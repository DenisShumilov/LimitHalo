from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


_DEPENDENCY_LICENSES_PATH = Path(__file__).resolve().with_name("dependency_licenses.py")
_DEPENDENCY_LICENSES_SPEC = importlib.util.spec_from_file_location(
    "limit_halo_build_dependency_licenses", _DEPENDENCY_LICENSES_PATH
)
if _DEPENDENCY_LICENSES_SPEC is None or _DEPENDENCY_LICENSES_SPEC.loader is None:
    raise RuntimeError("dependency license helper cannot be loaded")
dependency_licenses = importlib.util.module_from_spec(_DEPENDENCY_LICENSES_SPEC)
_DEPENDENCY_LICENSES_SPEC.loader.exec_module(dependency_licenses)


FIXED_ZIP_TIME = (2024, 1, 1, 0, 0, 0)
CANDIDATE_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_reparse(path: Path) -> bool:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def files_beneath(root: Path, excluded: set[Path] | None = None) -> list[Path]:
    excluded = {path.resolve() for path in (excluded or set())}
    result: list[Path] = []
    for current, directories, filenames in os.walk(root):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in directories:
            child = current_path / name
            if is_reparse(child):
                raise ValueError(f"reparse directory is forbidden: {child}")
            safe_directories.append(name)
        directories[:] = sorted(safe_directories)
        for name in sorted(filenames):
            child = current_path / name
            if child.resolve() in excluded:
                continue
            if is_reparse(child) or not child.is_file():
                raise ValueError(f"non-regular package file is forbidden: {child}")
            result.append(child)
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def product_identity(path: Path) -> dict[str, object]:
    product = read_json(path)
    required = {
        "schemaVersion", "displayName", "displayNameStatus", "internalProductId", "version",
        "architecture", "publisher", "repositorySlug", "executableName", "brokerExecutableName",
        "installerAppId", "settingsRelativePath", "installRelativePath", "copyright",
        "minimumWindows", "releaseChannel", "installerArchitecture", "aiAgentEntryPoint",
        "aiAgentProtocol",
    }
    if set(product) != required:
        raise ValueError("product identity has unexpected fields")
    if product["internalProductId"] != "AILimitsWidget" or product["displayNameStatus"] != "temporary-working-name":
        raise ValueError("stable product identity changed")
    if (
        product["installerArchitecture"] != "signing-ready-explicitly-unsigned-preview"
        or product["aiAgentEntryPoint"] != "LimitHalo-Agent.ps1"
        or product["aiAgentProtocol"] != "limit-halo-agent/1"
    ):
        raise ValueError("installer architecture identity changed")
    return product


def build_file_entries(root: Path, excluded: set[Path] | None = None) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in files_beneath(root, excluded)
    ]


def candidate_id(entries: Iterable[dict[str, object]]) -> str:
    ordered = sorted(entries, key=lambda entry: str(entry["path"]))
    lines = [f"{entry['path']}\t{entry['size']}\t{entry['sha256']}" for entry in ordered]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def command_payload_manifest(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    output = args.output.resolve()
    try:
        output.relative_to(root)
    except ValueError as error:
        raise ValueError("payload manifest output must be inside the payload root") from error
    if not SOURCE_RE.fullmatch(args.source_identity):
        raise ValueError("invalid source identity")
    product = product_identity(args.product.resolve(strict=True))
    entries = build_file_entries(root, {output})
    if not entries:
        raise ValueError("payload is empty")
    candidate = candidate_id(entries)
    broker_path = root / args.broker_relative
    broker_entry = next((entry for entry in entries if entry["path"] == args.broker_relative), None)
    if broker_entry is None or not broker_path.is_file():
        raise ValueError("native broker is not present in the payload")
    manifest: dict[str, object] = {
        "schemaVersion": "1.0.0",
        "kind": "installed-payload",
        "internalProductId": product["internalProductId"],
        "displayName": product["displayName"],
        "displayNameStatus": product["displayNameStatus"],
        "version": product["version"],
        "architecture": product["architecture"],
        "candidateId": candidate,
        "sourceIdentity": args.source_identity,
        "signatureStatus": "unsigned",
        "brokerSha256": broker_entry["sha256"],
        "files": entries,
    }
    write_json(output, manifest)
    print(candidate)


def command_zip(args: argparse.Namespace) -> None:
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    if output == source or source in output.parents:
        raise ValueError("archive output must not be inside its source")
    output.parent.mkdir(parents=True, exist_ok=True)
    members = files_beneath(source)
    if not members:
        raise ValueError("archive source is empty")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in members:
            relative = path.relative_to(source).as_posix()
            if args.prefix:
                relative = args.prefix.rstrip("/") + "/" + relative
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def command_normalize_stdlib_zip(args: argparse.Namespace) -> None:
    archive_path = args.archive.resolve(strict=True)
    if not archive_path.is_file() or is_reparse(archive_path):
        raise ValueError("stdlib archive must be a regular non-reparse file")
    temporary = archive_path.with_name(archive_path.name + ".normalized.tmp")
    if temporary.exists():
        if is_reparse(temporary) or not temporary.is_file():
            raise ValueError("stdlib normalization temporary path is unsafe")
        temporary.unlink()
    try:
        with zipfile.ZipFile(archive_path, "r") as source:
            infos = source.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError("stdlib archive contains duplicate members")
            members: list[tuple[str, bytes]] = []
            for info in infos:
                member = PurePosixPath(info.filename)
                if (
                    info.is_dir()
                    or not info.filename
                    or "\\" in info.filename
                    or member.is_absolute()
                    or any(part in {"", ".", ".."} for part in member.parts)
                ):
                    raise ValueError("stdlib archive contains an unsafe member")
                members.append((info.filename, source.read(info)))
        if not members:
            raise ValueError("stdlib archive is empty")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as target:
            for name, data in sorted(members, key=lambda item: item[0]):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = zipfile.ZIP_STORED
                target.writestr(info, data, compress_type=zipfile.ZIP_STORED)
        os.replace(temporary, archive_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(sha256(archive_path))


def command_release_manifest(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    product_path = args.product.resolve(strict=True)
    product = product_identity(product_path)
    payload_manifest_path = args.payload_manifest.resolve(strict=True)
    payload = read_json(payload_manifest_path)
    candidate = str(payload.get("candidateId", ""))
    if not CANDIDATE_RE.fullmatch(candidate):
        raise ValueError("invalid payload candidate identity")
    if payload.get("signatureStatus") != "unsigned":
        raise ValueError("this builder may only declare unsigned output")
    if not SOURCE_RE.fullmatch(args.source_identity) or payload.get("sourceIdentity") != args.source_identity:
        raise ValueError("source identity mismatch")
    dependency_manifest_path = args.dependency_license_manifest.resolve(strict=True)
    dependency_closure = dependency_licenses.validate_payload_closure(
        dependency_manifest_path,
        payload_manifest_path.parent,
    )
    payload_files = payload.get("files")
    dependency_entry = next(
        (
            entry for entry in payload_files
            if isinstance(entry, dict) and entry.get("path") == dependency_licenses.MANIFEST_RELATIVE_PATH
        ),
        None,
    ) if isinstance(payload_files, list) else None
    if (
        dependency_entry is None
        or dependency_entry.get("sha256") != sha256(dependency_manifest_path)
        or dependency_entry.get("size") != dependency_manifest_path.stat().st_size
    ):
        raise ValueError("dependency license manifest is not bound by the payload manifest")
    artifacts: list[dict[str, object]] = []
    for path_value in args.artifact:
        path = Path(path_value).resolve(strict=True)
        if path == output:
            raise ValueError("release manifest cannot hash itself")
        artifacts.append({"path": path.name, "size": path.stat().st_size, "sha256": sha256(path)})
    artifacts.sort(key=lambda item: str(item["path"]))
    if len({item["path"] for item in artifacts}) != len(artifacts):
        raise ValueError("duplicate release artifact name")
    expected_artifact_names = {
        f"LimitHalo-{product['version']}-Setup.exe",
        f"LimitHalo-{product['version']}-Windows-x64.zip",
        str(product["aiAgentEntryPoint"]),
    }
    if {str(item["path"]) for item in artifacts} != expected_artifact_names:
        raise ValueError("release must contain exact setup, portable, and AI agent artifacts")
    payload_agent = next(
        (
            entry for entry in payload_files
            if isinstance(entry, dict) and entry.get("path") == product["aiAgentEntryPoint"]
        ),
        None,
    ) if isinstance(payload_files, list) else None
    release_agent = next(
        (entry for entry in artifacts if entry["path"] == product["aiAgentEntryPoint"]),
        None,
    )
    if (
        payload_agent is None
        or release_agent is None
        or payload_agent.get("size") != release_agent.get("size")
        or payload_agent.get("sha256") != release_agent.get("sha256")
    ):
        raise ValueError("standalone and payload AI agent bytes differ")
    inputs = []
    for path_value in args.input:
        path = Path(path_value).resolve(strict=True)
        inputs.append({"path": path.name, "sha256": sha256(path)})
    inputs.sort(key=lambda item: str(item["path"]))
    manifest: dict[str, object] = {
        "schemaVersion": "1.0.0",
        "kind": "release-bundle",
        "internalProductId": product["internalProductId"],
        "displayName": product["displayName"],
        "displayNameStatus": product["displayNameStatus"],
        "version": product["version"],
        "architecture": product["architecture"],
        "candidateId": candidate,
        "sourceIdentity": args.source_identity,
        "signature": {
            "status": "unsigned",
            "verifiedSigner": None,
            "statement": "No Authenticode signature is claimed for this candidate.",
        },
        "automaticUpdater": False,
        "hostedGitHubActionsObserved": False,
        "publicationPerformed": False,
        "payloadManifestSha256": sha256(payload_manifest_path),
        "dependencyLicenseManifestSha256": sha256(dependency_manifest_path),
        "dependencyInventory": dependency_closure["inventory"],
        "buildInputs": inputs,
        "artifacts": artifacts,
    }
    write_json(output, manifest)


def command_checksums(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    paths = [Path(value).resolve(strict=True) for value in args.artifact]
    if output in paths:
        raise ValueError("checksum file cannot hash itself")
    if len({path.name for path in paths}) != len(paths):
        raise ValueError("duplicate checksum artifact name")
    lines = [f"{sha256(path)}  {path.name}" for path in sorted(paths, key=lambda value: value.name)]
    output.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")


def validate_payload_manifest(path: Path, root: Path, *, allow_portable_flag: bool) -> dict[str, object]:
    value = read_json(path)
    required = {
        "schemaVersion", "kind", "internalProductId", "displayName", "displayNameStatus", "version",
        "architecture", "candidateId", "sourceIdentity", "signatureStatus", "brokerSha256", "files",
    }
    if set(value) != required or value["schemaVersion"] != "1.0.0" or value["kind"] != "installed-payload":
        raise ValueError("invalid payload manifest schema")
    if value["internalProductId"] != "AILimitsWidget" or value["signatureStatus"] != "unsigned":
        raise ValueError("invalid payload identity or signature claim")
    entries = value["files"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("empty payload manifest")
    expected_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ValueError("invalid payload entry")
        relative = str(entry["path"])
        parts = Path(relative).parts
        if "\\" in relative or relative.startswith("/") or not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("unsafe payload path")
        if relative.casefold() in {item.casefold() for item in expected_paths}:
            raise ValueError("duplicate payload path")
        target = root / Path(*parts)
        if not target.is_file() or is_reparse(target):
            raise ValueError(f"missing or unsafe payload file: {relative}")
        if target.stat().st_size != entry["size"] or sha256(target) != entry["sha256"]:
            raise ValueError(f"payload hash mismatch: {relative}")
        expected_paths.add(relative)
    actual_paths = {
        item.relative_to(root).as_posix()
        for item in files_beneath(root, {path})
        if not (allow_portable_flag and item.relative_to(root).as_posix() == "portable.flag")
    }
    if actual_paths != expected_paths:
        raise ValueError("payload file-set mismatch")
    if candidate_id(entries) != value["candidateId"]:
        raise ValueError("payload candidate identity mismatch")
    broker = next((entry for entry in entries if entry["path"] == "ClaudeUsageBroker.exe"), None)
    if broker is None or broker["sha256"] != value["brokerSha256"]:
        raise ValueError("broker identity mismatch")
    return value


def command_verify(args: argparse.Namespace) -> None:
    release_root = args.release_root.resolve(strict=True)
    manifest_path = release_root / "release-manifest.json"
    sums_path = release_root / "SHA256SUMS.txt"
    manifest = read_json(manifest_path)
    required = {
        "schemaVersion", "kind", "internalProductId", "displayName", "displayNameStatus", "version",
        "architecture", "candidateId", "sourceIdentity", "signature", "automaticUpdater",
        "hostedGitHubActionsObserved", "publicationPerformed", "payloadManifestSha256",
        "dependencyLicenseManifestSha256", "dependencyInventory", "buildInputs", "artifacts",
    }
    if set(manifest) != required or manifest["schemaVersion"] != "1.0.0" or manifest["kind"] != "release-bundle":
        raise ValueError("invalid release manifest schema")
    if manifest["signature"] != {
        "status": "unsigned", "verifiedSigner": None,
        "statement": "No Authenticode signature is claimed for this candidate.",
    }:
        raise ValueError("release signature disclosure changed")
    if manifest["automaticUpdater"] is not False or manifest["publicationPerformed"] is not False:
        raise ValueError("forbidden release claim")
    if manifest["dependencyInventory"] != dependency_licenses.expected_inventory():
        raise ValueError("release dependency inventory mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest["dependencyLicenseManifestSha256"])):
        raise ValueError("invalid dependency license manifest identity")
    build_inputs = manifest["buildInputs"]
    if not isinstance(build_inputs, list) or not all(
        isinstance(entry, dict) and set(entry) == {"path", "sha256"}
        and re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"]))
        for entry in build_inputs
    ):
        raise ValueError("invalid build-input identities")
    input_hashes = {str(entry["path"]): str(entry["sha256"]) for entry in build_inputs}
    if input_hashes.get("python-build.lock.json") != "7fc014ce7148891c323f6244a0c0a09c762dc38d712176f8b1c407beb932b7b0":
        raise ValueError("Python build lock is not the frozen input")
    if input_hashes.get("inno-setup.lock.json") != "dcc97f618b56a106b866b3228ba71597ea4df21b88400558c04ac5d7e62708ae":
        raise ValueError("Inno Setup lock is not the frozen input")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 3:
        raise ValueError("release must contain setup, portable, and AI agent artifacts")
    names: set[str] = set()
    for entry in artifacts:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ValueError("invalid release artifact entry")
        name = str(entry["path"])
        if Path(name).name != name or name in names:
            raise ValueError("unsafe or duplicate release artifact name")
        path = release_root / name
        if not path.is_file() or path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"release artifact mismatch: {name}")
        names.add(name)
    version = str(manifest["version"])
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("invalid release version")
    expected_names = {
        f"LimitHalo-{version}-Setup.exe",
        f"LimitHalo-{version}-Windows-x64.zip",
        "LimitHalo-Agent.ps1",
    }
    if names != expected_names:
        raise ValueError("unexpected release artifact names")
    parsed_sums: dict[str, str] = {}
    for line in sums_path.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if not match or match.group(2) in parsed_sums:
            raise ValueError("invalid checksum line")
        parsed_sums[match.group(2)] = match.group(1)
    expected_sum_names = expected_names | {"release-manifest.json"}
    if set(parsed_sums) != expected_sum_names:
        raise ValueError("checksum coverage mismatch")
    for name, expected in parsed_sums.items():
        if sha256(release_root / name) != expected:
            raise ValueError(f"checksum mismatch: {name}")
    zip_path = release_root / f"LimitHalo-{version}-Windows-x64.zip"
    with zipfile.ZipFile(zip_path, "r") as archive:
        infos = archive.infolist()
        if not infos or [info.filename for info in infos] != sorted(info.filename for info in infos):
            raise ValueError("portable ZIP order is not deterministic")
        if any(info.date_time != FIXED_ZIP_TIME or "\\" in info.filename or info.filename.startswith("/") for info in infos):
            raise ValueError("portable ZIP metadata is not deterministic")
        names_in_zip = {info.filename for info in infos}
        if "LimitHalo/portable.flag" not in names_in_zip or "LimitHalo/package-manifest.json" not in names_in_zip:
            raise ValueError("portable ZIP markers are missing")
        agent_member = "LimitHalo/LimitHalo-Agent.ps1"
        if agent_member not in names_in_zip:
            raise ValueError("portable AI agent is missing")
        standalone_agent = release_root / "LimitHalo-Agent.ps1"
        if archive.read(agent_member) != standalone_agent.read_bytes():
            raise ValueError("standalone and portable AI agent bytes differ")
        dependency_member = "LimitHalo/" + dependency_licenses.MANIFEST_RELATIVE_PATH
        if dependency_member not in names_in_zip:
            raise ValueError("portable dependency license manifest is missing")
        dependency_bytes = archive.read(dependency_member)
        if hashlib.sha256(dependency_bytes).hexdigest() != manifest["dependencyLicenseManifestSha256"]:
            raise ValueError("portable dependency license manifest identity mismatch")
        dependency_value = json.loads(dependency_bytes.decode("ascii"))
        if not isinstance(dependency_value, dict):
            raise ValueError("portable dependency license manifest is invalid")
        dependency_licenses.validate_document(dependency_value)
        if dependency_value["inventory"] != manifest["dependencyInventory"]:
            raise ValueError("portable dependency inventory mismatch")
        declared_license_members: set[str] = set()
        for record in dependency_value["files"]:
            full_name = "LimitHalo/" + str(record["path"])
            if full_name not in names_in_zip:
                raise ValueError(f"portable dependency license file is missing: {record['path']}")
            content = archive.read(full_name)
            if len(content) != record["size"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ValueError(f"portable dependency license file mismatch: {record['path']}")
            declared_license_members.add(full_name)
        actual_license_members = {name for name in names_in_zip if name.startswith("LimitHalo/licenses/")}
        if actual_license_members != declared_license_members | {dependency_member}:
            raise ValueError("portable dependency license directory contains undeclared files")
        payload_manifest_bytes = archive.read("LimitHalo/package-manifest.json")
        if hashlib.sha256(payload_manifest_bytes).hexdigest() != manifest["payloadManifestSha256"]:
            raise ValueError("portable payload-manifest identity mismatch")
        payload = json.loads(payload_manifest_bytes.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("candidateId") != manifest["candidateId"]:
            raise ValueError("portable candidate identity mismatch")
        if payload.get("signatureStatus") != "unsigned" or payload.get("internalProductId") != "AILimitsWidget":
            raise ValueError("portable payload identity or signature claim mismatch")
        entries = payload.get("files")
        if not isinstance(entries, list) or not entries or candidate_id(entries) != payload.get("candidateId"):
            raise ValueError("portable payload manifest is invalid")
        payload_agent = next(
            (entry for entry in entries if isinstance(entry, dict) and entry.get("path") == "LimitHalo-Agent.ps1"),
            None,
        )
        if (
            payload_agent is None
            or payload_agent.get("size") != standalone_agent.stat().st_size
            or payload_agent.get("sha256") != sha256(standalone_agent)
        ):
            raise ValueError("standalone and payload AI agent identities differ")
        declared: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
                raise ValueError("portable payload entry is invalid")
            relative = str(entry["path"])
            full_name = "LimitHalo/" + relative
            if relative in declared or full_name not in names_in_zip:
                raise ValueError("portable payload file set is incomplete")
            content = archive.read(full_name)
            if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError(f"portable payload hash mismatch: {relative}")
            declared.add(relative)
        allowed_names = {"LimitHalo/" + relative for relative in declared} | {
            "LimitHalo/package-manifest.json", "LimitHalo/portable.flag"
        }
        if names_in_zip != allowed_names:
            raise ValueError("portable ZIP contains undeclared bytes")
    print(json.dumps({"status": "PASS", "candidateId": manifest["candidateId"]}, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    subcommands = root.add_subparsers(dest="command", required=True)

    payload = subcommands.add_parser("payload-manifest")
    payload.add_argument("--root", type=Path, required=True)
    payload.add_argument("--output", type=Path, required=True)
    payload.add_argument("--product", type=Path, required=True)
    payload.add_argument("--source-identity", required=True)
    payload.add_argument("--broker-relative", default="ClaudeUsageBroker.exe")
    payload.set_defaults(function=command_payload_manifest)

    archive = subcommands.add_parser("zip")
    archive.add_argument("--source", type=Path, required=True)
    archive.add_argument("--output", type=Path, required=True)
    archive.add_argument("--prefix", default="")
    archive.set_defaults(function=command_zip)

    stdlib = subcommands.add_parser("normalize-stdlib-zip")
    stdlib.add_argument("--archive", type=Path, required=True)
    stdlib.set_defaults(function=command_normalize_stdlib_zip)

    release = subcommands.add_parser("release-manifest")
    release.add_argument("--output", type=Path, required=True)
    release.add_argument("--product", type=Path, required=True)
    release.add_argument("--payload-manifest", type=Path, required=True)
    release.add_argument("--dependency-license-manifest", type=Path, required=True)
    release.add_argument("--source-identity", required=True)
    release.add_argument("--artifact", action="append", required=True)
    release.add_argument("--input", action="append", default=[])
    release.set_defaults(function=command_release_manifest)

    sums = subcommands.add_parser("checksums")
    sums.add_argument("--output", type=Path, required=True)
    sums.add_argument("--artifact", action="append", required=True)
    sums.set_defaults(function=command_checksums)

    verify = subcommands.add_parser("verify")
    verify.add_argument("--release-root", type=Path, required=True)
    verify.set_defaults(function=command_verify)
    return root


def main() -> int:
    try:
        args = parser().parse_args()
        args.function(args)
        return 0
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(f"release_tools: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
