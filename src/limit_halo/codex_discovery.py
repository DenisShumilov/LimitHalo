from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
from ctypes import wintypes
from dataclasses import dataclass, replace
from functools import cmp_to_key, total_ordering
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol


_HRESULT = wintypes.LONG


PACKAGE_NAME = "OpenAI.Codex"
PACKAGE_PUBLISHER = 'CN="OpenAI OpCo, LLC", O="OpenAI OpCo, LLC", L=San Francisco, S=California, C=US'
PACKAGE_PUBLISHER_ID = "3k8sg7r9htsxt"
STORE_PACKAGE_PUBLISHER = "CN=50BDFD77-8903-4850-9FFE-6E8522F64D5B"
STORE_PACKAGE_PUBLISHER_ID = "2p2nqsd0c76g0"
PACKAGE_PUBLISHER_IDS = {
    PACKAGE_PUBLISHER: PACKAGE_PUBLISHER_ID,
    STORE_PACKAGE_PUBLISHER: STORE_PACKAGE_PUBLISHER_ID,
}
NPM_PACKAGE_NAME = "@openai/codex"
NPM_NATIVE_NAME = "@openai/codex-win32-x64"
NPM_NATIVE_SUFFIX = "-win32-x64"
NPM_EXE_RELATIVE = Path("vendor/x86_64-pc-windows-msvc/bin/codex.exe")
APPX_EXE_RELATIVE = Path("app/resources/codex.exe")
EXPECTED_SUBJECT = {
    "2.5.4.3": "OpenAI OpCo, LLC",
    "2.5.4.10": "OpenAI OpCo, LLC",
    "2.5.4.7": "San Francisco",
    "2.5.4.8": "California",
    "2.5.4.6": "US",
}
CODE_SIGNING_EKU = "1.3.6.1.5.5.7.3.3"
ERROR_RESOURCE_TYPE_NOT_FOUND = 1813
MAX_METADATA_BYTES = 1_048_576
MAX_EXECUTABLE_BYTES = 1_073_741_824
MAX_SEMVER_TEXT = 256
MAX_SEMVER_COMPONENT = 65_535
MAX_SEMVER_PRERELEASE_IDENTIFIERS = 16
MAX_SEMVER_PRERELEASE_IDENTIFIER = 64
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:\.(0|[1-9][0-9]*))?(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class DiscoveryFailure(RuntimeError):
    pass


@total_ordering
@dataclass(frozen=True)
class Version:
    numbers: tuple[int, int, int, int]
    prerelease: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.numbers, tuple)
            or len(self.numbers) != 4
            or any(isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= MAX_SEMVER_COMPONENT for item in self.numbers)
        ):
            raise DiscoveryFailure("semantic version component out of range")
        if not isinstance(self.prerelease, tuple) or len(self.prerelease) > MAX_SEMVER_PRERELEASE_IDENTIFIERS:
            raise DiscoveryFailure("semantic prerelease is too large")
        for item in self.prerelease:
            if (
                not isinstance(item, str)
                or not 1 <= len(item) <= MAX_SEMVER_PRERELEASE_IDENTIFIER
                or re.fullmatch(r"[0-9A-Za-z-]+", item) is None
                or (item.isdecimal() and len(item) > 1 and item.startswith("0"))
            ):
                raise DiscoveryFailure("invalid semantic prerelease")

    @classmethod
    def parse(cls, text: str) -> "Version":
        if not isinstance(text, str):
            raise DiscoveryFailure("version is not text")
        if not 1 <= len(text) <= MAX_SEMVER_TEXT:
            raise DiscoveryFailure("semantic version text is too large")
        match = SEMVER.fullmatch(text)
        if not match:
            raise DiscoveryFailure("invalid semantic version")
        number_text = tuple(item or "0" for item in match.groups()[:4])
        if any(len(item) > 5 or (len(item) == 5 and item > str(MAX_SEMVER_COMPONENT)) for item in number_text):
            raise DiscoveryFailure("semantic version component out of range")
        try:
            numbers = tuple(int(item) for item in number_text)
        except (ValueError, OverflowError) as exc:
            raise DiscoveryFailure("invalid semantic version component") from exc
        prerelease = tuple((match.group(5) or "").split(".")) if match.group(5) else ()
        return cls(numbers, prerelease)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        if self.numbers != other.numbers:
            return self.numbers < other.numbers
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_numeric, right_numeric = left.isdecimal(), right.isdecimal()
            if left_numeric and right_numeric:
                try:
                    return int(left) < int(right)
                except (ValueError, OverflowError) as exc:
                    raise DiscoveryFailure("invalid semantic prerelease") from exc
            if left_numeric != right_numeric:
                return left_numeric
            return left < right
        return len(self.prerelease) < len(other.prerelease)


@dataclass(frozen=True)
class TrustRecord:
    status_valid: bool
    revocation_cache_available: bool
    subject: Mapping[str, str]
    enhanced_key_usages: frozenset[str]

    def satisfies_policy(self) -> bool:
        return (
            self.status_valid
            and self.revocation_cache_available
            and dict(self.subject) == EXPECTED_SUBJECT
            and CODE_SIGNING_EKU in self.enhanced_key_usages
        )


@dataclass(frozen=True)
class AppxRecord:
    name: str
    publisher: str
    publisher_id: str
    family_name: str
    full_name: str
    version: str
    install_location: Path


@dataclass(frozen=True)
class CodexCandidate:
    source: str
    version: Version
    executable: Path
    package_root: Path
    publisher_id: str | None = None
    executable_sha256: str = ""


class TrustVerifier(Protocol):
    def __call__(self, executable: Path) -> TrustRecord: ...


class PeVersionReader(Protocol):
    def __call__(self, executable: Path) -> tuple[int, int, int, int] | None: ...


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DiscoveryFailure("duplicate metadata key")
        result[key] = value
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size <= 0 or path.stat().st_size > MAX_METADATA_BYTES:
            raise DiscoveryFailure("metadata size rejected")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_no_duplicate_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DiscoveryFailure("metadata rejected") from exc
    if not isinstance(value, dict):
        raise DiscoveryFailure("metadata is not an object")
    return value


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DiscoveryFailure("path is unavailable") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _reject_existing_reparse_chain(path: Path, *, trusted_root: Path | None = None) -> None:
    absolute = Path(os.path.abspath(path))
    stop = None if trusted_root is None else Path(os.path.abspath(trusted_root))
    if stop is not None:
        try:
            if os.path.normcase(os.path.commonpath((str(stop), str(absolute)))) != os.path.normcase(str(stop)):
                raise DiscoveryFailure("path escapes trusted anchor")
        except ValueError as exc:
            raise DiscoveryFailure("path escapes trusted anchor") from exc
    cursor = absolute
    components: list[Path] = []
    while True:
        components.append(cursor)
        if stop is not None and os.path.normcase(str(cursor)) == os.path.normcase(str(stop)):
            break
        parent = cursor.parent
        if parent == cursor:
            if stop is not None:
                raise DiscoveryFailure("trusted anchor not reached")
            break
        cursor = parent
    for component in reversed(components):
        if (component.exists() or component.is_symlink()) and _is_reparse(component):
            raise DiscoveryFailure("reparse point rejected")


def _canonical_regular_beneath(
    root: Path,
    relative: Path,
    *,
    trusted_root: Path | None = None,
) -> tuple[Path, Path]:
    if relative.is_absolute() or ".." in relative.parts:
        raise DiscoveryFailure("unsafe relative path")
    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise DiscoveryFailure("package root unavailable") from exc
    _reject_existing_reparse_chain(root, trusted_root=trusted_root)
    if not canonical_root.is_dir():
        raise DiscoveryFailure("unsafe package root")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if _is_reparse(cursor):
            raise DiscoveryFailure("reparse point rejected")
    try:
        candidate = (root / relative).resolve(strict=True)
        common = os.path.commonpath((str(canonical_root), str(candidate)))
    except (OSError, ValueError) as exc:
        raise DiscoveryFailure("candidate path rejected") from exc
    if os.path.normcase(common) != os.path.normcase(str(canonical_root)):
        raise DiscoveryFailure("candidate escapes package root")
    if candidate.name.casefold() != "codex.exe" or not candidate.is_file():
        raise DiscoveryFailure("Codex executable rejected")
    return canonical_root, candidate


def _require_pe_version(executable: Path, version: Version, reader: PeVersionReader) -> None:
    try:
        file_version = reader(executable)
    except (OSError, TypeError, ValueError) as exc:
        raise DiscoveryFailure("PE version unavailable") from exc
    if file_version is None:
        return
    try:
        normalized = tuple(file_version)
    except TypeError as exc:
        raise DiscoveryFailure("PE version unavailable") from exc
    if normalized != version.numbers:
        raise DiscoveryFailure("PE/package version mismatch")


def executable_sha256(executable: Path) -> str:
    """Hash one bounded regular executable and reject a changing read."""

    try:
        before = executable.stat()
        if not executable.is_file() or not 1 <= before.st_size <= MAX_EXECUTABLE_BYTES:
            raise DiscoveryFailure("Codex executable size rejected")
        digest = hashlib.sha256()
        with executable.open("rb") as stream:
            while chunk := stream.read(1_048_576):
                digest.update(chunk)
        after = executable.stat()
    except OSError as exc:
        raise DiscoveryFailure("Codex executable hash unavailable") from exc
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise DiscoveryFailure("Codex executable changed while hashing")
    return digest.hexdigest()


def npm_candidate(
    npm_codex_root: Path,
    *,
    trust_verifier: TrustVerifier,
    pe_version_reader: PeVersionReader,
    trusted_root: Path | None = None,
) -> CodexCandidate:
    _reject_existing_reparse_chain(npm_codex_root, trusted_root=trusted_root)
    package_path = npm_codex_root / "package.json"
    if _is_reparse(package_path):
        raise DiscoveryFailure("npm metadata reparse rejected")
    package = _read_json_object(package_path)
    if package.get("name") != NPM_PACKAGE_NAME:
        raise DiscoveryFailure("wrong npm package")
    version = Version.parse(package.get("version"))
    declarations: list[str] = []
    for field in ("dependencies", "optionalDependencies"):
        value = package.get(field)
        if value is not None:
            if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
                raise DiscoveryFailure("malformed npm dependency metadata")
            if NPM_NATIVE_NAME in value:
                declarations.append(value[NPM_NATIVE_NAME])
    if len(declarations) != 1:
        raise DiscoveryFailure("native npm dependency is ambiguous")
    dependency_root = npm_codex_root / "node_modules" / "@openai" / "codex-win32-x64"
    dependency_metadata = dependency_root / "package.json"
    if _is_reparse(dependency_metadata):
        raise DiscoveryFailure("native metadata reparse rejected")
    native = _read_json_object(dependency_metadata)
    declared = declarations[0]
    package_version = package["version"]
    legacy_profile = (
        native.get("name") == NPM_NATIVE_NAME
        and native.get("version") == package_version
        and declared in {package_version, f"={package_version}"}
    )
    alias_version = f"{package_version}{NPM_NATIVE_SUFFIX}"
    alias_profile = (
        native.get("name") == NPM_PACKAGE_NAME
        and native.get("version") == alias_version
        and native.get("os") == ["win32"]
        and native.get("cpu") == ["x64"]
        and declared == f"npm:{NPM_PACKAGE_NAME}@{alias_version}"
    )
    if not (legacy_profile or alias_profile):
        raise DiscoveryFailure("native npm metadata mismatch")
    canonical_root, executable = _canonical_regular_beneath(
        dependency_root, NPM_EXE_RELATIVE, trusted_root=trusted_root
    )
    _require_pe_version(executable, version, pe_version_reader)
    if not trust_verifier(executable).satisfies_policy():
        raise DiscoveryFailure("npm Codex signer rejected")
    return CodexCandidate(
        "npm", version, executable, canonical_root,
        executable_sha256=executable_sha256(executable),
    )


def appx_candidate(
    record: AppxRecord,
    *,
    trust_verifier: TrustVerifier,
    pe_version_reader: PeVersionReader,
    trusted_root: Path | None = None,
) -> CodexCandidate:
    expected_publisher_id = PACKAGE_PUBLISHER_IDS.get(record.publisher)
    if record.name != PACKAGE_NAME or expected_publisher_id is None:
        raise DiscoveryFailure("wrong AppX identity")
    if record.publisher_id != expected_publisher_id:
        raise DiscoveryFailure("wrong AppX publisher ID")
    if record.family_name != f"{PACKAGE_NAME}_{record.publisher_id}":
        raise DiscoveryFailure("AppX family mismatch")
    version = Version.parse(record.version)
    if record.full_name != f"{PACKAGE_NAME}_{record.version}_x64__{record.publisher_id}":
        raise DiscoveryFailure("AppX full name mismatch")
    canonical_root, executable = _canonical_regular_beneath(
        record.install_location, APPX_EXE_RELATIVE, trusted_root=trusted_root
    )
    _require_pe_version(executable, version, pe_version_reader)
    if not trust_verifier(executable).satisfies_policy():
        raise DiscoveryFailure("AppX Codex signer rejected")
    return CodexCandidate(
        "appx", version, executable, canonical_root, record.publisher_id,
        executable_sha256(executable),
    )


def appx_runnable_mirror(
    candidate: CodexCandidate,
    mirror_root: Path,
    *,
    trust_verifier: TrustVerifier,
    pe_version_reader: PeVersionReader,
    trusted_root: Path | None = None,
) -> CodexCandidate:
    """Select an executable LocalAppData mirror byte-identical to AppX."""

    if candidate.source != "appx" or re.fullmatch(r"[0-9a-f]{64}", candidate.executable_sha256) is None:
        raise DiscoveryFailure("AppX reference identity unavailable")
    _reject_existing_reparse_chain(mirror_root, trusted_root=trusted_root)
    try:
        child_names = sorted(
            entry.name for entry in mirror_root.iterdir()
            if re.fullmatch(r"[0-9a-f]{16}", entry.name) is not None
        )
    except OSError as exc:
        raise DiscoveryFailure("Codex mirror root unavailable") from exc
    relative_candidates = (Path("codex.exe"),) + tuple(Path(name) / "codex.exe" for name in child_names)
    mirrors: list[CodexCandidate] = []
    for relative in relative_candidates:
        try:
            canonical_root, executable = _canonical_regular_beneath(
                mirror_root, relative, trusted_root=trusted_root
            )
            if executable.stat().st_size != candidate.executable.stat().st_size:
                continue
            if executable_sha256(executable) != candidate.executable_sha256:
                continue
            _require_pe_version(executable, candidate.version, pe_version_reader)
            if not trust_verifier(executable).satisfies_policy():
                continue
            mirrors.append(
                replace(
                    candidate,
                    source="appx-mirror",
                    executable=executable,
                    package_root=canonical_root,
                )
            )
        except (DiscoveryFailure, OSError):
            continue
    if not mirrors:
        raise DiscoveryFailure("no byte-identical runnable AppX mirror")
    return select_candidate(mirrors)


def _compare_string_ordinal_ignore_case(left: str, right: str) -> int:
    if os.name != "nt":
        raise DiscoveryFailure("Windows ordinal comparison unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    compare = kernel32.CompareStringOrdinal
    compare.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int, wintypes.BOOL]
    compare.restype = ctypes.c_int
    result = compare(left, len(left), right, len(right), True)
    if result == 0:
        raise DiscoveryFailure("Windows ordinal comparison failed")
    return result - 2


def _compare_candidates(left: CodexCandidate, right: CodexCandidate) -> int:
    source_priority = {"appx-mirror": 0, "npm": 1}
    if left.source not in source_priority or right.source not in source_priority:
        raise DiscoveryFailure("unknown Codex candidate source")
    if source_priority[left.source] != source_priority[right.source]:
        return -1 if source_priority[left.source] < source_priority[right.source] else 1
    if left.version != right.version:
        return -1 if left.version > right.version else 1
    return _compare_string_ordinal_ignore_case(str(left.executable), str(right.executable))


def select_candidate(candidates: Iterable[CodexCandidate]) -> CodexCandidate:
    values = tuple(candidates)
    if not values:
        raise DiscoveryFailure("no trusted Codex candidate")
    return sorted(values, key=cmp_to_key(_compare_candidates))[0]


def discover_from_sources(
    *,
    npm_codex_root: Path | None,
    appx_records: Iterable[AppxRecord],
    trust_verifier: TrustVerifier,
    pe_version_reader: PeVersionReader,
    npm_trusted_root: Path | None = None,
    appx_trusted_root: Path | None = None,
    appx_mirror_root: Path | None = None,
    appx_mirror_trusted_root: Path | None = None,
) -> CodexCandidate:
    candidates: list[CodexCandidate] = []
    if npm_codex_root is not None:
        try:
            candidates.append(
                npm_candidate(
                    npm_codex_root,
                    trust_verifier=trust_verifier,
                    pe_version_reader=pe_version_reader,
                    trusted_root=npm_trusted_root,
                )
            )
        except DiscoveryFailure:
            pass
    for record in appx_records:
        try:
            appx = appx_candidate(
                record,
                trust_verifier=trust_verifier,
                pe_version_reader=pe_version_reader,
                trusted_root=appx_trusted_root,
            )
            if appx_mirror_root is not None:
                candidates.append(
                    appx_runnable_mirror(
                        appx,
                        appx_mirror_root,
                        trust_verifier=trust_verifier,
                        pe_version_reader=pe_version_reader,
                        trusted_root=appx_mirror_trusted_root,
                    )
                )
        except DiscoveryFailure:
            continue
    return select_candidate(candidates)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _PACKAGE_VERSION(ctypes.Structure):
    _fields_ = [("Major", wintypes.WORD), ("Minor", wintypes.WORD), ("Build", wintypes.WORD), ("Revision", wintypes.WORD)]


def _guid(text: str) -> _GUID:
    import uuid

    value = uuid.UUID(text)
    data = value.bytes_le
    result = _GUID()
    ctypes.memmove(ctypes.byref(result), data, 16)
    return result


def _com_call(pointer: ctypes.c_void_p, index: int, restype: Any, *argtypes: Any) -> Any:
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    address = vtable[index]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(address)


def _release(pointer: ctypes.c_void_p) -> None:
    if pointer and pointer.value:
        _com_call(pointer, 2, wintypes.ULONG)(pointer)


class _HString:
    def __init__(self, value: str) -> None:
        combase = ctypes.WinDLL("combase", use_last_error=True)
        combase.WindowsCreateString.argtypes = [wintypes.LPCWSTR, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)]
        combase.WindowsCreateString.restype = _HRESULT
        self._combase = combase
        self.handle = ctypes.c_void_p()
        if combase.WindowsCreateString(value, len(value), ctypes.byref(self.handle)) < 0:
            raise DiscoveryFailure("HSTRING creation failed")

    def close(self) -> None:
        if self.handle.value:
            self._combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
            self._combase.WindowsDeleteString(self.handle)
            self.handle = ctypes.c_void_p()

    def __enter__(self) -> ctypes.c_void_p:
        return self.handle

    def __exit__(self, *_: object) -> None:
        self.close()


def _consume_hstring(handle: ctypes.c_void_p) -> str:
    if not handle.value:
        return ""
    combase = ctypes.WinDLL("combase", use_last_error=True)
    length = wintypes.UINT()
    combase.WindowsGetStringRawBuffer.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)]
    combase.WindowsGetStringRawBuffer.restype = wintypes.LPCWSTR
    combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
    try:
        pointer = combase.WindowsGetStringRawBuffer(handle, ctypes.byref(length))
        return ctypes.wstring_at(pointer, length.value)
    finally:
        combase.WindowsDeleteString(handle)


def _package_path(full_name: str) -> Path:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetPackagePathByFullName
    function.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
    function.restype = wintypes.LONG
    length = wintypes.UINT()
    status = function(full_name, ctypes.byref(length), None)
    if status != 122 or length.value < 2 or length.value > 32_768:
        raise DiscoveryFailure("AppX path unavailable")
    buffer = ctypes.create_unicode_buffer(length.value)
    if function(full_name, ctypes.byref(length), buffer) != 0:
        raise DiscoveryFailure("AppX path unavailable")
    return Path(buffer.value)


def enumerate_current_user_appx() -> tuple[AppxRecord, ...]:
    """Use WinRT PackageManager public metadata; never invokes a helper process."""

    if os.name != "nt":
        return ()
    combase = ctypes.WinDLL("combase", use_last_error=True)
    combase.RoInitialize.argtypes = [wintypes.UINT]
    combase.RoInitialize.restype = _HRESULT
    initialized = combase.RoInitialize(1)
    if initialized < 0 and ctypes.c_uint32(initialized).value != 0x80010106:
        return ()
    package_manager = ctypes.c_void_p()
    try:
        combase.RoActivateInstance.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        combase.RoActivateInstance.restype = _HRESULT
        with _HString("Windows.Management.Deployment.PackageManager") as runtime_class:
            if combase.RoActivateInstance(runtime_class, ctypes.byref(package_manager)) < 0:
                return ()
        records: list[AppxRecord] = []
        seen_full_names: set[str] = set()
        for publisher_text in PACKAGE_PUBLISHER_IDS:
            collection = ctypes.c_void_p()
            iterator = ctypes.c_void_p()
            try:
                with _HString("") as current_user, _HString(PACKAGE_NAME) as name, _HString(publisher_text) as publisher:
                    find = _com_call(
                        package_manager,
                        14,
                        _HRESULT,
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p),
                    )
                    if find(package_manager, current_user, name, publisher, ctypes.byref(collection)) < 0:
                        continue
                first = _com_call(collection, 6, _HRESULT, ctypes.POINTER(ctypes.c_void_p))
                if first(collection, ctypes.byref(iterator)) < 0:
                    continue
                while True:
                    has_current = ctypes.c_ubyte()
                    if _com_call(iterator, 7, _HRESULT, ctypes.POINTER(ctypes.c_ubyte))(
                        iterator, ctypes.byref(has_current)
                    ) < 0 or not has_current.value:
                        break
                    package = ctypes.c_void_p()
                    package_id = ctypes.c_void_p()
                    try:
                        if _com_call(iterator, 6, _HRESULT, ctypes.POINTER(ctypes.c_void_p))(
                            iterator, ctypes.byref(package)
                        ) < 0:
                            break
                        if _com_call(package, 6, _HRESULT, ctypes.POINTER(ctypes.c_void_p))(
                            package, ctypes.byref(package_id)
                        ) < 0:
                            continue

                        def text_property(index: int) -> str:
                            value = ctypes.c_void_p()
                            if _com_call(package_id, index, _HRESULT, ctypes.POINTER(ctypes.c_void_p))(
                                package_id, ctypes.byref(value)
                            ) < 0:
                                raise DiscoveryFailure("AppX identity unavailable")
                            return _consume_hstring(value)

                        version = _PACKAGE_VERSION()
                        if _com_call(package_id, 7, _HRESULT, ctypes.POINTER(_PACKAGE_VERSION))(
                            package_id, ctypes.byref(version)
                        ) < 0:
                            continue
                        name_value = text_property(6)
                        publisher_value = text_property(10)
                        publisher_id = text_property(11)
                        full_name = text_property(12)
                        family = text_property(13)
                        if full_name not in seen_full_names:
                            records.append(
                                AppxRecord(
                                    name_value,
                                    publisher_value,
                                    publisher_id,
                                    family,
                                    full_name,
                                    f"{version.Major}.{version.Minor}.{version.Build}.{version.Revision}",
                                    _package_path(full_name),
                                )
                            )
                            seen_full_names.add(full_name)
                    except DiscoveryFailure:
                        pass
                    finally:
                        _release(package_id)
                        _release(package)
                    moved = ctypes.c_ubyte()
                    if _com_call(iterator, 8, _HRESULT, ctypes.POINTER(ctypes.c_ubyte))(
                        iterator, ctypes.byref(moved)
                    ) < 0 or not moved.value:
                        break
            finally:
                _release(iterator)
                _release(collection)
        return tuple(records)
    finally:
        _release(package_manager)
        if initialized in (0, 1):
            combase.RoUninitialize()


class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in (
        "dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS",
        "dwProductVersionMS", "dwProductVersionLS", "dwFileFlagsMask", "dwFileFlags",
        "dwFileOS", "dwFileType", "dwFileSubtype", "dwFileDateMS", "dwFileDateLS",
    )]


def read_pe_file_version(executable: Path) -> tuple[int, int, int, int] | None:
    if os.name != "nt":
        raise OSError("Windows required")
    version = ctypes.WinDLL("version", use_last_error=True)
    ignored = wintypes.DWORD()
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    size = version.GetFileVersionInfoSizeW(str(executable), ctypes.byref(ignored))
    if not size:
        if ctypes.get_last_error() == ERROR_RESOURCE_TYPE_NOT_FOUND:
            return None
        raise OSError("version metadata unavailable")
    if size > 16 * 1024 * 1024:
        raise OSError("version metadata unavailable")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(executable), 0, size, buffer):
        raise OSError("version metadata unavailable")
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise OSError("version metadata unavailable")
    fixed = ctypes.cast(pointer, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
    if fixed.dwSignature != 0xFEEF04BD:
        raise OSError("version metadata malformed")
    return (
        fixed.dwFileVersionMS >> 16,
        fixed.dwFileVersionMS & 0xFFFF,
        fixed.dwFileVersionLS >> 16,
        fixed.dwFileVersionLS & 0xFFFF,
    )


class _WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pcwszFilePath", wintypes.LPCWSTR),
        ("hFile", wintypes.HANDLE),
        ("pgKnownSubject", ctypes.c_void_p),
    ]


class _WINTRUST_DATA(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p),
        ("dwUIChoice", wintypes.DWORD),
        ("fdwRevocationChecks", wintypes.DWORD),
        ("dwUnionChoice", wintypes.DWORD),
        ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),
        ("dwStateAction", wintypes.DWORD),
        ("hWVTStateData", wintypes.HANDLE),
        ("pwszURLReference", wintypes.LPCWSTR),
        ("dwProvFlags", wintypes.DWORD),
        ("dwUIContext", wintypes.DWORD),
        ("pSignatureSettings", ctypes.c_void_p),
    ]


class _CERT_CONTEXT(ctypes.Structure):
    _fields_ = [
        ("dwCertEncodingType", wintypes.DWORD),
        ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbCertEncoded", wintypes.DWORD),
        ("pCertInfo", ctypes.c_void_p),
        ("hCertStore", wintypes.HANDLE),
    ]


class _CRYPT_PROVIDER_CERT(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pCert", ctypes.POINTER(_CERT_CONTEXT)),
    ]


class _CERT_ENHKEY_USAGE(ctypes.Structure):
    _fields_ = [("cUsageIdentifier", wintypes.DWORD), ("rgpszUsageIdentifier", ctypes.POINTER(ctypes.c_char_p))]


def verify_authenticode_cache_only(executable: Path) -> TrustRecord:
    """Require a cache-only, whole-chain revocation-valid Authenticode result."""

    if os.name != "nt":
        return TrustRecord(False, False, {}, frozenset())
    wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    action = _guid("00AAC56B-CD44-11D0-8CC2-00C04FC295EE")
    file_info = _WINTRUST_FILE_INFO(ctypes.sizeof(_WINTRUST_FILE_INFO), str(executable), None, None)
    data = _WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(data)
    data.dwUIChoice = 2
    data.fdwRevocationChecks = 1
    data.dwUnionChoice = 1
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = 1
    data.dwProvFlags = 0x00001000 | 0x00000040 | 0x00002000
    wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(_GUID), ctypes.c_void_p]
    wintrust.WinVerifyTrust.restype = wintypes.LONG
    status = wintrust.WinVerifyTrust(wintypes.HWND(-1), ctypes.byref(action), ctypes.byref(data))
    subject: dict[str, str] = {}
    usages: set[str] = set()
    try:
        if status != 0 or not data.hWVTStateData:
            return TrustRecord(False, False, {}, frozenset())
        wintrust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
        wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
        provider = wintrust.WTHelperProvDataFromStateData(data.hWVTStateData)
        wintrust.WTHelperGetProvSignerFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.c_void_p
        signer = wintrust.WTHelperGetProvSignerFromChain(provider, 0, False, 0)
        wintrust.WTHelperGetProvCertFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        wintrust.WTHelperGetProvCertFromChain.restype = ctypes.POINTER(_CRYPT_PROVIDER_CERT)
        provider_cert = wintrust.WTHelperGetProvCertFromChain(signer, 0)
        if not provider_cert or not provider_cert.contents.pCert:
            return TrustRecord(False, False, {}, frozenset())
        cert = provider_cert.contents.pCert
        crypt32.CertGetNameStringW.argtypes = [ctypes.POINTER(_CERT_CONTEXT), wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
        crypt32.CertGetNameStringW.restype = wintypes.DWORD
        for oid in EXPECTED_SUBJECT:
            encoded = oid.encode("ascii") + b"\0"
            oid_buffer = ctypes.create_string_buffer(encoded)
            required = crypt32.CertGetNameStringW(cert, 3, 0, oid_buffer, None, 0)
            if required < 2 or required > 1024:
                return TrustRecord(False, True, {}, frozenset())
            output = ctypes.create_unicode_buffer(required)
            if crypt32.CertGetNameStringW(cert, 3, 0, oid_buffer, output, required) != required:
                return TrustRecord(False, True, {}, frozenset())
            subject[oid] = output.value
        crypt32.CertGetEnhancedKeyUsage.argtypes = [ctypes.POINTER(_CERT_CONTEXT), wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        crypt32.CertGetEnhancedKeyUsage.restype = wintypes.BOOL
        bytes_needed = wintypes.DWORD()
        if not crypt32.CertGetEnhancedKeyUsage(cert, 0, None, ctypes.byref(bytes_needed)) or bytes_needed.value > 65_536:
            return TrustRecord(False, True, subject, frozenset())
        usage_buffer = ctypes.create_string_buffer(bytes_needed.value)
        if not crypt32.CertGetEnhancedKeyUsage(cert, 0, usage_buffer, ctypes.byref(bytes_needed)):
            return TrustRecord(False, True, subject, frozenset())
        usage = ctypes.cast(usage_buffer, ctypes.POINTER(_CERT_ENHKEY_USAGE)).contents
        if usage.cUsageIdentifier > 128:
            return TrustRecord(False, True, subject, frozenset())
        for index in range(usage.cUsageIdentifier):
            item = usage.rgpszUsageIdentifier[index]
            if item:
                usages.add(item.decode("ascii", "strict"))
        return TrustRecord(True, True, subject, frozenset(usages))
    except (OSError, UnicodeError, ValueError):
        return TrustRecord(False, True, subject, frozenset())
    finally:
        data.dwStateAction = 2
        wintrust.WinVerifyTrust(wintypes.HWND(-1), ctypes.byref(action), ctypes.byref(data))


def verify_candidate_unchanged(candidate: CodexCandidate) -> Path:
    """Revalidate the exact selected bytes immediately before CreateProcess."""

    if candidate.source not in {"appx-mirror", "npm"}:
        raise DiscoveryFailure("selected Codex source is not runnable")
    if re.fullmatch(r"[0-9a-f]{64}", candidate.executable_sha256) is None:
        raise DiscoveryFailure("selected Codex hash unavailable")
    try:
        _reject_existing_reparse_chain(candidate.executable, trusted_root=candidate.package_root)
        package_root = candidate.package_root.resolve(strict=True)
        executable = candidate.executable.resolve(strict=True)
        common = os.path.commonpath((str(package_root), str(executable)))
    except (OSError, ValueError) as exc:
        raise DiscoveryFailure("selected Codex path changed") from exc
    if (
        os.path.normcase(common) != os.path.normcase(str(package_root))
        or os.path.normcase(str(executable)) != os.path.normcase(str(candidate.executable))
        or executable.name.casefold() != "codex.exe"
    ):
        raise DiscoveryFailure("selected Codex path changed")
    _require_pe_version(executable, candidate.version, read_pe_file_version)
    if executable_sha256(executable) != candidate.executable_sha256:
        raise DiscoveryFailure("selected Codex bytes changed")
    if not verify_authenticode_cache_only(executable).satisfies_policy():
        raise DiscoveryFailure("selected Codex trust changed")
    return executable


def discover_public_codex() -> CodexCandidate:
    """Discover public package metadata only; never executes a candidate."""

    if os.name != "nt":
        raise DiscoveryFailure("Windows required")
    appdata = os.environ.get("APPDATA")
    npm_root = None
    if appdata:
        appdata_root = Path(appdata)
        npm_root = appdata_root / "npm" / "node_modules" / "@openai" / "codex"
    else:
        appdata_root = None
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        localappdata_root = Path(localappdata)
        mirror_root = localappdata_root / "OpenAI" / "Codex" / "bin"
    else:
        localappdata_root = None
        mirror_root = None
    return discover_from_sources(
        npm_codex_root=npm_root,
        appx_records=enumerate_current_user_appx(),
        trust_verifier=verify_authenticode_cache_only,
        pe_version_reader=read_pe_file_version,
        npm_trusted_root=appdata_root,
        appx_mirror_root=mirror_root,
        appx_mirror_trusted_root=localappdata_root,
    )
