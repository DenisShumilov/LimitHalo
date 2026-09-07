"""Bounded, opt-in GitHub release discovery and inert update-bundle staging.

This module never launches downloaded code. GitHub's HTTPS asset digest binds a
download to a release in the pinned repository; it is not a publisher signature.
The caller must obtain exact unsigned-bundle consent and reverify immediately
before invoking the existing local maintenance helper.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPOSITORY = "DenisShumilov/LimitHalo"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
MAX_API_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 131072
MAX_CHECKSUM_BYTES = 4096
MAX_SETUP_BYTES = 64 * 1024 * 1024
MAX_ZIP_BYTES = 96 * 1024 * 1024
MAX_HELPER_BYTES = 1024 * 1024
NETWORK_TIMEOUT = 15
DISCOVERY_SECONDS = 30
STAGING_SECONDS = 240
MAX_REDIRECTS = 3
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(
    r"v?((?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\."
    r"(?:0|[1-9][0-9]{0,5}))(?:-beta\.([1-9][0-9]{0,5}))?\Z"
)
_MANIFEST_KEYS = {
    "schemaVersion", "kind", "internalProductId", "displayName", "displayNameStatus",
    "version", "architecture", "candidateId", "sourceIdentity", "signature",
    "automaticUpdater", "hostedGitHubActionsObserved", "publicationPerformed",
    "payloadManifestSha256", "dependencyLicenseManifestSha256", "dependencyInventory",
    "buildInputs", "artifacts",
}


class UpdateError(RuntimeError):
    """An unavailable, incompatible, or unsafe release; no install was performed."""


@dataclass(frozen=True)
class Asset:
    name: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    assets: tuple[Asset, ...]
    page_url: str


@dataclass(frozen=True)
class StagedUpdate:
    directory: Path
    acceptance: str
    release: Release
    update_root: Path


class Transport(Protocol):
    def get(self, url: str, *, max_bytes: int, expected_size: int | None = None,
            deadline: float | None = None) -> bytes: ...


def _version(value: object) -> tuple[str, tuple[int, int, int, int, int], bool]:
    if not isinstance(value, str) or len(value) > 40:
        raise UpdateError("Unsupported release version")
    match = _VERSION.fullmatch(value)
    if not match:
        raise UpdateError("Unsupported release version")
    numeric, beta = match.groups()
    major, minor, patch = (int(part) for part in numeric.split("."))
    return numeric, (major, minor, patch, int(beta is None), int(beta or 0)), beta is not None


def _asset_limits(version: str) -> dict[str, int]:
    return {
        f"LimitHalo-{version}-Setup.exe": MAX_SETUP_BYTES,
        f"LimitHalo-{version}-Windows-x64.zip": MAX_ZIP_BYTES,
        "LimitHalo-Agent.ps1": MAX_HELPER_BYTES,
        "release-manifest.json": MAX_MANIFEST_BYTES,
        "SHA256SUMS.txt": MAX_CHECKSUM_BYTES,
    }


def _safe_url(url: str) -> str:
    """Allow only HTTPS GitHub repository endpoints and exact asset CDN hosts."""
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 for c in url):
        raise UpdateError("Unsafe update URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UpdateError("Unsafe update URL") from exc
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or port is not None or parsed.fragment or "\\" in url):
        raise UpdateError("Unsafe update URL")
    if parsed.netloc not in {"api.github.com", "github.com",
                            "release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
        raise UpdateError("Update host is not allowed")
    lower_path = parsed.path.lower()
    if any(part in lower_path for part in ("%2e", "%2f", "%5c", "/../", "/./")):
        raise UpdateError("Unsafe update URL path")
    if parsed.netloc == "api.github.com":
        prefix = f"/repos/{REPOSITORY}/releases"
        if not (parsed.path == prefix or parsed.path.startswith(prefix + "/")):
            raise UpdateError("Update API is outside the pinned repository")
    elif parsed.netloc == "github.com":
        if not parsed.path.startswith(f"/{REPOSITORY}/releases/download/"):
            raise UpdateError("Update asset is outside the pinned repository")
    return url


class _SafeRedirects(HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS
    max_repeats = 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _safe_url(newurl)
        # Carry an explicit overall count: changing the URL cannot reset it.
        count = getattr(req, "_limithalo_redirects", 0) + 1
        if count > MAX_REDIRECTS:
            raise UpdateError("Too many update redirects")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            raise UpdateError("Unsupported update redirect")
        redirected._limithalo_redirects = count
        return redirected


class HttpsTransport:
    """No cookies, credentials, retry loop, or unbounded response buffering."""

    def get(self, url: str, *, max_bytes: int, expected_size: int | None = None,
            deadline: float | None = None) -> bytes:
        _safe_url(url)
        if deadline is None:
            deadline = time.monotonic() + DISCOVERY_SECONDS
        if time.monotonic() >= deadline:
            raise UpdateError("Update request timed out")
        request = Request(url, headers={
            "User-Agent": "LimitHalo-Updater/1",
            "Accept": "application/vnd.github+json" if urlsplit(url).netloc == "api.github.com"
                      else "application/octet-stream",
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept-Encoding": "identity",
        })
        try:
            with build_opener(_SafeRedirects()).open(
                request, timeout=min(NETWORK_TIMEOUT, max(0.1, deadline - time.monotonic()))
            ) as response:
                _safe_url(response.geturl())
                if response.status != 200:
                    raise UpdateError("Unexpected update response")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise UpdateError("Encoded update response rejected")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    if not re.fullmatch(r"[0-9]{1,12}", declared):
                        raise UpdateError("Invalid update response length")
                    length = int(declared)
                    if length > max_bytes or (expected_size is not None and length != expected_size):
                        raise UpdateError("Update response size mismatch")
                result = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise UpdateError("Update request timed out")
                    chunk = response.read(min(65536, max_bytes + 1 - len(result)))
                    if not chunk:
                        break
                    result.extend(chunk)
                    if len(result) > max_bytes:
                        raise UpdateError("Update response is too large")
                if declared is not None and len(result) != int(declared):
                    raise UpdateError("Truncated update response")
                if expected_size is not None and len(result) != expected_size:
                    raise UpdateError("Update response size mismatch")
                return bytes(result)
        except UpdateError:
            raise
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise UpdateError("Could not securely retrieve GitHub update data") from exc


def _json(data: bytes, maximum: int):
    if not isinstance(data, bytes) or not 2 <= len(data) <= maximum:
        raise UpdateError("Invalid update JSON size")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise UpdateError("Duplicate update JSON key")
            result[key] = value
        return result

    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(UpdateError("Invalid JSON number")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise UpdateError("Invalid update JSON") from exc


def _validate_release(release: Release) -> None:
    if not isinstance(release, Release):
        raise UpdateError("Invalid release")
    numeric, _, _ = _version(release.tag)
    if release.version != numeric or release.tag != "v" + release.tag.removeprefix("v"):
        raise UpdateError("Mismatched release version")
    if release.page_url != f"{REPOSITORY_URL}/releases/tag/{quote(release.tag, safe='')}":
        raise UpdateError("Release is outside the pinned repository")
    limits = _asset_limits(numeric)
    if not isinstance(release.assets, tuple) or len(release.assets) != len(limits):
        raise UpdateError("Release must contain exactly five assets")
    seen = set()
    for asset in release.assets:
        if not isinstance(asset, Asset) or not isinstance(asset.name, str) or asset.name not in limits or asset.name in seen:
            raise UpdateError("Unknown, missing, or duplicate release asset")
        seen.add(asset.name)
        if type(asset.size) is not int or not 1 <= asset.size <= limits[asset.name]:
            raise UpdateError("Invalid release asset size")
        if not isinstance(asset.sha256, str) or not _HASH.fullmatch(asset.sha256):
            raise UpdateError("GitHub SHA-256 asset digest is required")
        expected_url = f"{REPOSITORY_URL}/releases/download/{quote(release.tag, safe='')}/{quote(asset.name, safe='')}"
        if asset.url != expected_url:
            raise UpdateError("Mismatched release asset URL")
        _safe_url(asset.url)
    if seen != set(limits):
        raise UpdateError("Release assets are incomplete")


def discover(current_tag: str, *, transport: Transport | None = None) -> Release | None:
    """Read one bounded public release page; this beta channel also accepts stable.

    Drafts and unsupported channels are ignored. Any eligible newer release with
    malformed identity/assets fails closed, rather than offering an older one.
    Neither metadata discovery nor startup checks download executable assets.
    """
    _, current, _ = _version(current_tag)
    client = transport or HttpsTransport()
    raw = client.get(RELEASES_URL, max_bytes=MAX_API_BYTES,
                     deadline=time.monotonic() + DISCOVERY_SECONDS)
    items = _json(raw, MAX_API_BYTES)
    if not isinstance(items, list) or len(items) > 100:
        raise UpdateError("Invalid GitHub release list")
    candidates: list[tuple[tuple[int, int, int, int, int], Release]] = []
    seen_tags = set()
    for item in items:
        if not isinstance(item, dict) or type(item.get("draft")) is not bool:
            raise UpdateError("Invalid GitHub release entry")
        if item["draft"]:
            continue
        try:
            numeric, order, beta = _version(item.get("tag_name"))
        except UpdateError:
            continue
        if order <= current:
            continue
        tag = item["tag_name"]
        if tag in seen_tags:
            raise UpdateError("Duplicate release tag")
        seen_tags.add(tag)
        if type(item.get("prerelease")) is not bool or item["prerelease"] != beta:
            raise UpdateError("Release channel does not match its tag")
        if not isinstance(item.get("assets"), list) or len(item["assets"]) != 5:
            raise UpdateError("Release must contain exactly five assets")
        release_id = item.get("id")
        if type(release_id) is not int or release_id <= 0 or item.get("url") != f"https://api.github.com/repos/{REPOSITORY}/releases/{release_id}":
            raise UpdateError("Release is outside the pinned repository")
        assets = []
        asset_ids = set()
        for entry in item["assets"]:
            if not isinstance(entry, dict):
                raise UpdateError("Invalid GitHub asset entry")
            digest = entry.get("digest")
            asset_id = entry.get("id")
            if (not isinstance(digest, str) or not digest.startswith("sha256:")
                    or type(asset_id) is not int or asset_id <= 0 or asset_id in asset_ids
                    or entry.get("state") != "uploaded"
                    or entry.get("url") != f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset_id}"):
                raise UpdateError("Invalid GitHub asset identity or digest")
            asset_ids.add(asset_id)
            assets.append(Asset(entry.get("name"), entry.get("size"), digest[7:], entry.get("browser_download_url")))
        release = Release(numeric, tag, tuple(assets), item.get("html_url"))
        _validate_release(release)
        candidates.append((order, release))
    return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


def _safe_chain(path: Path, *, directory: bool = True) -> Path:
    """Check every existing component without resolving through a reparse point."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or str(path).startswith("\\\\"):
        raise UpdateError("Update staging requires a local absolute path")
    chain = [*reversed(path.parents), path]
    for component in chain:
        try:
            info = component.lstat()
        except OSError as exc:
            raise UpdateError("Update staging path is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise UpdateError("Reparse points are not allowed in update staging")
        if component != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise UpdateError("Update staging directory is invalid")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UpdateError("Update staging file is not a private regular file")
    return path


def _read_asset(directory: Path, asset: Asset) -> bytes:
    path = _safe_chain(directory / asset.name, directory=False)
    try:
        if path.stat().st_size != asset.size:
            raise UpdateError("Staged update asset size changed")
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != asset.size:
                raise UpdateError("Staged update file changed")
            data = stream.read(asset.size + 1)
        _safe_chain(path, directory=False)
        if len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
            raise UpdateError("Staged update digest changed")
        return data
    except OSError as exc:
        raise UpdateError("Could not verify staged update") from exc


def _acceptance(directory: Path, release: Release) -> str:
    _validate_release(release)
    _safe_chain(directory)
    assets = {asset.name: asset for asset in release.assets}
    try:
        if {entry.name for entry in directory.iterdir()} != set(assets):
            raise UpdateError("Staged bundle contains unexpected files")
    except OSError as exc:
        raise UpdateError("Staged bundle is unavailable") from exc
    # Always rehash all five files against the original HTTPS GitHub API digests.
    metadata = {}
    for name, asset in assets.items():
        data = _read_asset(directory, asset)
        if name in {"release-manifest.json", "SHA256SUMS.txt"}:
            metadata[name] = data
    manifest = _json(metadata["release-manifest.json"], MAX_MANIFEST_BYTES)
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise UpdateError("Incompatible release manifest")
    exact = {"schemaVersion": "1.0.0", "kind": "release-bundle", "internalProductId": "AILimitsWidget",
             "displayName": "LimitHalo", "version": release.version, "architecture": "x64"}
    if any(manifest.get(key) != value for key, value in exact.items()):
        raise UpdateError("Release manifest identity mismatch")
    if manifest["automaticUpdater"] is not False or manifest["publicationPerformed"] is not False:
        raise UpdateError("Unsupported release build provenance")
    signature = manifest["signature"]
    if (not isinstance(signature, dict) or set(signature) != {"status", "verifiedSigner", "statement"}
            or signature["status"] != "unsigned" or signature["verifiedSigner"] is not None
            or not isinstance(signature["statement"], str)):
        raise UpdateError("Only explicit-consent unsigned bundles are supported")
    for key in ("candidateId", "payloadManifestSha256", "dependencyLicenseManifestSha256"):
        if not isinstance(manifest[key], str) or not _HASH.fullmatch(manifest[key]):
            raise UpdateError("Invalid release manifest identity hash")
    if not isinstance(manifest["sourceIdentity"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["sourceIdentity"]):
        raise UpdateError("Invalid source identity")
    declarations = manifest["artifacts"]
    artifact_names = set(assets) - {"release-manifest.json", "SHA256SUMS.txt"}
    if not isinstance(declarations, list) or len(declarations) != 3:
        raise UpdateError("Invalid release artifact declarations")
    seen = set()
    for entry in declarations:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise UpdateError("Invalid release artifact declaration")
        name = entry["path"]
        if not isinstance(name, str) or name not in artifact_names or name in seen:
            raise UpdateError("Unknown or duplicate release artifact")
        seen.add(name)
        if type(entry["size"]) is not int or entry["size"] != assets[name].size or entry["sha256"] != assets[name].sha256:
            raise UpdateError("Manifest and GitHub artifact digest disagree")
    try:
        lines = metadata["SHA256SUMS.txt"].decode("ascii").splitlines()
    except UnicodeError as exc:
        raise UpdateError("Invalid release checksums") from exc
    if len(lines) != 4:
        raise UpdateError("Release must declare four checksums")
    seen = set()
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if not match:
            raise UpdateError("Invalid release checksum line")
        digest, name = match.groups()
        if name not in assets or name == "SHA256SUMS.txt" or name in seen or digest != assets[name].sha256:
            raise UpdateError("Release checksums and GitHub digests disagree")
        seen.add(name)
    setup = assets[f"LimitHalo-{release.version}-Setup.exe"]
    helper = assets["LimitHalo-Agent.ps1"]
    return f"{manifest['candidateId']}:{setup.sha256}:{helper.sha256}"


def stage_release(release: Release, update_root: Path, *, transport: Transport | None = None) -> StagedUpdate:
    """Download only on an explicit user request into one new private directory.

    The caller supplies an authorized update root with existing parents. Failed
    or interrupted staging is inert and retained; no recursive cleanup occurs.
    """
    _validate_release(release)
    root = Path(update_root)
    _safe_chain(root.parent)
    if not root.is_absolute() or ".." in root.parts:
        raise UpdateError("Update staging requires an absolute path")
    try:
        root.mkdir(exist_ok=True)
        _safe_chain(root)
        directory = Path(tempfile.mkdtemp(prefix="LimitHalo-", dir=str(root)))
        _safe_chain(directory)
        client = transport or HttpsTransport()
        deadline = time.monotonic() + STAGING_SECONDS
        limits = _asset_limits(release.version)
        # Verify small metadata first; every executable remains inert bytes.
        ordered = sorted(release.assets, key=lambda asset: (asset.name not in {"release-manifest.json", "SHA256SUMS.txt"}, asset.size))
        for asset in ordered:
            if time.monotonic() >= deadline:
                raise UpdateError("Update download timed out")
            _safe_chain(directory)
            data = client.get(asset.url, max_bytes=limits[asset.name], expected_size=asset.size, deadline=deadline)
            if not isinstance(data, bytes) or len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
                raise UpdateError("Downloaded update does not match GitHub digest")
            with (directory / asset.name).open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _safe_chain(directory / asset.name, directory=False)
        acceptance = _acceptance(directory, release)
        return StagedUpdate(directory, acceptance, release, root)
    except UpdateError:
        raise
    except OSError as exc:
        raise UpdateError("Could not safely stage update files") from exc


def verify_staged(staged: StagedUpdate) -> str:
    """Reverify the complete inert bundle and return its exact consent identity."""
    if not isinstance(staged, StagedUpdate):
        raise UpdateError("Invalid staged update")
    root = _safe_chain(Path(staged.update_root))
    directory = _safe_chain(Path(staged.directory))
    if directory.parent != root or not directory.name.startswith("LimitHalo-"):
        raise UpdateError("Staged update escaped its authorized root")
    acceptance = _acceptance(directory, staged.release)
    if acceptance != staged.acceptance:
        raise UpdateError("Staged update acceptance changed")
    return acceptance
