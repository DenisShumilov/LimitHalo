from __future__ import annotations

import argparse
import copy
import email
import hashlib
import json
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable


SCHEMA_VERSION = "1.0.0"
MANIFEST_KIND = "dependency-license-closure"
MANIFEST_RELATIVE_PATH = "licenses/dependency-licenses.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

WHEEL_IDENTITIES: tuple[dict[str, str], ...] = (
    {
        "id": "python-wheel:altgraph",
        "kind": "python-wheel",
        "name": "altgraph",
        "version": "0.17.5",
        "source": "altgraph-0.17.5-py2.py3-none-any.whl",
        "sourceSha256": "f3a22400bce1b0c701683820ac4f3b159cd301acab067c51c653e06961600597",
    },
    {
        "id": "python-wheel:packaging",
        "kind": "python-wheel",
        "name": "packaging",
        "version": "26.3",
        "source": "packaging-26.3-py3-none-any.whl",
        "sourceSha256": "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c",
    },
    {
        "id": "python-wheel:pefile",
        "kind": "python-wheel",
        "name": "pefile",
        "version": "2024.8.26",
        "source": "pefile-2024.8.26-py3-none-any.whl",
        "sourceSha256": "76f8b485dcd3b1bb8166f1128d395fa3d87af26360c2358fb75b80019b957c6f",
    },
    {
        "id": "python-wheel:pillow",
        "kind": "python-wheel",
        "name": "pillow",
        "version": "12.3.0",
        "source": "pillow-12.3.0-cp312-cp312-win_amd64.whl",
        "sourceSha256": "a2b55dd6b2a4c4b7d87ffa56bdb33fdc5fdb9a462173861a7bc097f17d91cb09",
    },
    {
        "id": "python-wheel:pyinstaller",
        "kind": "python-wheel",
        "name": "pyinstaller",
        "version": "6.21.0",
        "source": "pyinstaller-6.21.0-py3-none-win_amd64.whl",
        "sourceSha256": "7fae06c494ce0ebfe6bd3055c0e409def884f63af2e3705d06bd431ad9237fc7",
    },
    {
        "id": "python-wheel:pyinstaller-hooks-contrib",
        "kind": "python-wheel",
        "name": "pyinstaller-hooks-contrib",
        "version": "2026.6",
        "source": "pyinstaller_hooks_contrib-2026.6-py3-none-any.whl",
        "sourceSha256": "fd13b8ac126b35361175edacd41a0d97080b75dd5f4b594ecefefff969509dd3",
    },
    {
        "id": "python-wheel:pywin32-ctypes",
        "kind": "python-wheel",
        "name": "pywin32-ctypes",
        "version": "0.2.3",
        "source": "pywin32_ctypes-0.2.3-py3-none-any.whl",
        "sourceSha256": "8a1513379d709975552d202d942d9837758905c8d01eb82b8bcc30918929e7b8",
    },
    {
        "id": "python-wheel:setuptools",
        "kind": "python-wheel",
        "name": "setuptools",
        "version": "83.0.0",
        "source": "setuptools-83.0.0-py3-none-any.whl",
        "sourceSha256": "29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3",
    },
)

RUNTIME_IDENTITIES: tuple[dict[str, str], ...] = (
    {
        "id": "runtime:python",
        "kind": "runtime",
        "name": "Python",
        "version": "3.12.13",
        "source": "python-build.lock.json#python",
        "sourceSha256": "5912d0884b23c0343983a864c6064242391e2265536f50b88624857e353882c9",
    },
    {
        "id": "runtime:tcl-tk",
        "kind": "runtime",
        "name": "Tcl/Tk",
        "version": "8.6",
        "source": "_internal/_tk_data/license.terms",
        "sourceSha256": "0d1e4405f6273f091732764ed89b57066be63ce64869be6c71ea337dc4f2f9b5",
    },
    {
        "id": "asset:lobehub-icons",
        "kind": "asset",
        "name": "@lobehub/icons-static-png",
        "version": "1.95.0",
        "source": "src/limit_halo/assets",
        "sourceSha256": "bcf61e272f21209483ce56646477564bac9639bb9990dfb9948ded09848215a9",
    },
)

EXPECTED_PYTHON_LICENSE_SHA256 = "886a0ead2d89030ee62dbff52b04e47ab91998341295bb9c56fb952b4e081c7a"
EXPECTED_TCL_TK_LICENSE_SHA256 = RUNTIME_IDENTITIES[1]["sourceSha256"]
EXPECTED_LOBEHUB_LICENSE_SHA256 = "add9d7531d1b21646317a8958e38fc727506fa39d24bdecb44154d943c82753a"
EXPECTED_LOBEHUB_ATTRIBUTION_SHA256 = "fcd90eee1401ce03ba1ed60bbd6bcb10f1e9677a7d09f10279b09e8e1ccda0ee"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_unique(path: Path) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("dependency closure root must be an object")
    return value


def expected_inventory() -> list[dict[str, str]]:
    return copy.deepcopy(sorted(WHEEL_IDENTITIES + RUNTIME_IDENTITIES, key=lambda item: item["id"]))


def validate_python_lock(path: Path) -> None:
    value = load_unique(path)
    if set(value) != {"schemaVersion", "python", "pyinstaller", "installMode", "wheels"} or value["schemaVersion"] != SCHEMA_VERSION:
        raise ValueError("invalid Python build lock schema")
    python = value["python"]
    if not isinstance(python, dict) or python != {
        "version": "3.12.13",
        "architecture": "x64",
        "buildInterpreterSha256": RUNTIME_IDENTITIES[0]["sourceSha256"],
    }:
        raise ValueError("unexpected Python runtime identity")
    records = value["wheels"]
    if not isinstance(records, list) or len(records) != len(WHEEL_IDENTITIES):
        raise ValueError("Python build lock must contain exactly eight wheels")
    actual = {(str(item.get("name")), str(item.get("sha256"))) for item in records if isinstance(item, dict)}
    expected = {(item["source"], item["sourceSha256"]) for item in WHEEL_IDENTITIES}
    if actual != expected or any(set(item) != {"name", "size", "sha256"} for item in records if isinstance(item, dict)):
        raise ValueError("Python build lock wheel inventory mismatch")


def safe_member_name(name: str) -> PurePosixPath:
    if "\\" in name or name.startswith("/"):
        raise ValueError(f"unsafe wheel member path: {name}")
    path = PurePosixPath(name)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe wheel member path: {name}")
    return path


def selected_role(name: str) -> str | None:
    if re.search(r"(?:^|/)[^/]+\.dist-info/METADATA$", name):
        return "metadata"
    leaf = PurePosixPath(name).name.casefold()
    for prefix, role in (
        ("license", "license"),
        ("licence", "license"),
        ("copying", "license"),
        ("notice", "notice"),
        ("copyright", "notice"),
        ("author", "notice"),
        ("authors", "notice"),
    ):
        if leaf == prefix or leaf.startswith(prefix + "."):
            return role
    return None


def write_closure_file(
    payload_root: Path,
    relative: str,
    data: bytes,
    component_id: str,
    role: str,
) -> dict[str, object]:
    relative_path = safe_member_name(relative)
    target = payload_root.joinpath(*relative_path.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {
        "componentId": component_id,
        "path": relative_path.as_posix(),
        "role": role,
        "size": len(data),
        "sha256": sha256_bytes(data),
    }


def collect_wheel(
    wheel: Path,
    identity: dict[str, str],
    payload_root: Path,
) -> list[dict[str, object]]:
    if wheel.name != identity["source"] or sha256(wheel) != identity["sourceSha256"]:
        raise ValueError(f"locked wheel identity mismatch: {wheel.name}")
    records: list[dict[str, object]] = []
    top_level_metadata = 0
    wheel_prefix = "licenses/python-wheels/" + wheel.name[:-4]
    with zipfile.ZipFile(wheel, "r") as archive:
        seen: set[str] = set()
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if info.is_dir():
                continue
            member = safe_member_name(info.filename)
            normalized = member.as_posix()
            if normalized in seen:
                raise ValueError(f"duplicate wheel member: {normalized}")
            seen.add(normalized)
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type not in {0, stat.S_IFREG}:
                raise ValueError(f"non-regular wheel member: {normalized}")
            role = selected_role(normalized)
            if role is None:
                continue
            data = archive.read(info)
            if role == "metadata" and len(member.parts) == 2:
                metadata = email.message_from_bytes(data)
                if metadata.get("Name", "").casefold() != identity["name"].casefold() or metadata.get("Version") != identity["version"]:
                    raise ValueError(f"wheel METADATA identity mismatch: {wheel.name}")
                top_level_metadata += 1
            records.append(write_closure_file(
                payload_root,
                f"{wheel_prefix}/{normalized}",
                data,
                identity["id"],
                role,
            ))
    if top_level_metadata != 1:
        raise ValueError(f"wheel must contain one top-level METADATA record: {wheel.name}")
    roles = {str(item["role"]) for item in records}
    if "metadata" not in roles or not roles.intersection({"license", "notice"}):
        raise ValueError(f"wheel license/METADATA closure is incomplete: {wheel.name}")
    return records


def copy_exact(
    source: Path,
    expected_sha256: str,
    payload_root: Path,
    relative: str,
    component_id: str,
    role: str,
) -> dict[str, object]:
    data = source.read_bytes()
    if sha256_bytes(data) != expected_sha256:
        raise ValueError(f"dependency notice identity mismatch: {source.name}")
    return write_closure_file(payload_root, relative, data, component_id, role)


def validate_document(value: dict[str, object]) -> None:
    if set(value) != {"schemaVersion", "kind", "inventory", "files"}:
        raise ValueError("invalid dependency license closure schema")
    if value["schemaVersion"] != SCHEMA_VERSION or value["kind"] != MANIFEST_KIND:
        raise ValueError("invalid dependency license closure identity")
    if value["inventory"] != expected_inventory():
        raise ValueError("dependency inventory mismatch")
    files = value["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("dependency license closure is empty")
    expected_ids = {item["id"] for item in expected_inventory()}
    roles_by_id: dict[str, set[str]] = {item: set() for item in expected_ids}
    paths: set[str] = set()
    canonical_order: list[str] = []
    for record in files:
        if not isinstance(record, dict) or set(record) != {"componentId", "path", "role", "size", "sha256"}:
            raise ValueError("invalid dependency license file record")
        component_id = str(record["componentId"])
        relative = str(record["path"])
        role = str(record["role"])
        if component_id not in expected_ids or role not in {"metadata", "license", "notice", "attribution"}:
            raise ValueError("unknown dependency license component or role")
        if not relative.startswith("licenses/") or safe_member_name(relative).as_posix() != relative:
            raise ValueError("unsafe dependency license path")
        if relative in paths:
            raise ValueError("duplicate dependency license path")
        if not isinstance(record["size"], int) or record["size"] < 1 or not SHA256_RE.fullmatch(str(record["sha256"])):
            raise ValueError("invalid dependency license file identity")
        paths.add(relative)
        canonical_order.append(relative)
        roles_by_id[component_id].add(role)
    if canonical_order != sorted(canonical_order):
        raise ValueError("dependency license files are not canonically ordered")
    for component in expected_inventory():
        roles = roles_by_id[component["id"]]
        if component["kind"] == "python-wheel" and ("metadata" not in roles or not roles.intersection({"license", "notice"})):
            raise ValueError(f"missing license or METADATA closure: {component['id']}")
        if component["id"] in {"runtime:python", "runtime:tcl-tk"} and "license" not in roles:
            raise ValueError(f"missing runtime license: {component['id']}")
        if component["id"] == "asset:lobehub-icons" and not {"license", "attribution"}.issubset(roles):
            raise ValueError("missing LobeHub license or attribution")


def validate_payload_closure(manifest_path: Path, payload_root: Path) -> dict[str, object]:
    manifest_path = manifest_path.resolve(strict=True)
    payload_root = payload_root.resolve(strict=True)
    expected_manifest = payload_root / Path(*PurePosixPath(MANIFEST_RELATIVE_PATH).parts)
    if manifest_path != expected_manifest:
        raise ValueError("dependency license manifest is outside its canonical payload path")
    value = load_unique(manifest_path)
    validate_document(value)
    declared: set[str] = set()
    for record in value["files"]:
        relative = str(record["path"])
        target = payload_root / Path(*PurePosixPath(relative).parts)
        if not target.is_file() or target.is_symlink():
            raise ValueError(f"missing dependency license file: {relative}")
        if target.stat().st_size != record["size"] or sha256(target) != record["sha256"]:
            raise ValueError(f"dependency license file mismatch: {relative}")
        declared.add(relative)
    license_root = payload_root / "licenses"
    actual = {
        path.relative_to(payload_root).as_posix()
        for path in license_root.rglob("*")
        if path.is_file()
    }
    if actual != declared | {MANIFEST_RELATIVE_PATH}:
        raise ValueError("dependency license directory contains undeclared files")
    return value


def collect(args: argparse.Namespace) -> None:
    wheelhouse = args.wheelhouse.resolve(strict=True)
    lock = args.lock.resolve(strict=True)
    payload_root = args.payload_root.resolve(strict=True)
    license_root = payload_root / "licenses"
    if license_root.exists() and any(license_root.iterdir()):
        raise ValueError("dependency license output root must be absent or empty")
    validate_python_lock(lock)
    wheel_names = {path.name for path in wheelhouse.iterdir() if path.is_file()}
    expected_names = {item["source"] for item in WHEEL_IDENTITIES}
    if wheel_names != expected_names:
        raise ValueError("wheelhouse does not contain the exact eight locked wheels")
    files: list[dict[str, object]] = []
    for identity in WHEEL_IDENTITIES:
        files.extend(collect_wheel(wheelhouse / identity["source"], identity, payload_root))
    files.extend((
        copy_exact(args.python_license.resolve(strict=True), EXPECTED_PYTHON_LICENSE_SHA256, payload_root, "licenses/PYTHON-LICENSE.txt", "runtime:python", "license"),
        copy_exact(args.tcl_tk_license.resolve(strict=True), EXPECTED_TCL_TK_LICENSE_SHA256, payload_root, "licenses/TCL-TK-LICENSE.terms", "runtime:tcl-tk", "license"),
        copy_exact(args.lobehub_license.resolve(strict=True), EXPECTED_LOBEHUB_LICENSE_SHA256, payload_root, "licenses/LICENSE-lobehub.txt", "asset:lobehub-icons", "license"),
        copy_exact(args.lobehub_attribution.resolve(strict=True), EXPECTED_LOBEHUB_ATTRIBUTION_SHA256, payload_root, "licenses/ATTRIBUTIONS-lobehub.md", "asset:lobehub-icons", "attribution"),
    ))
    files.sort(key=lambda item: str(item["path"]))
    value: dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "inventory": expected_inventory(),
        "files": files,
    }
    validate_document(value)
    manifest = payload_root / Path(*PurePosixPath(MANIFEST_RELATIVE_PATH).parts)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="ascii", newline="\n")
    validate_payload_closure(manifest, payload_root)
    print(json.dumps({"status": "PASS", "manifest": str(manifest), "sha256": sha256(manifest)}, sort_keys=True))


def verify(args: argparse.Namespace) -> None:
    value = validate_payload_closure(args.manifest, args.payload_root)
    print(json.dumps({"status": "PASS", "components": len(value["inventory"]), "files": len(value["files"])}, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect")
    collect_parser.add_argument("--wheelhouse", type=Path, required=True)
    collect_parser.add_argument("--lock", type=Path, required=True)
    collect_parser.add_argument("--payload-root", type=Path, required=True)
    collect_parser.add_argument("--python-license", type=Path, required=True)
    collect_parser.add_argument("--tcl-tk-license", type=Path, required=True)
    collect_parser.add_argument("--lobehub-license", type=Path, required=True)
    collect_parser.add_argument("--lobehub-attribution", type=Path, required=True)
    collect_parser.set_defaults(function=collect)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--payload-root", type=Path, required=True)
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.set_defaults(function=verify)
    return root


def main() -> int:
    try:
        args = parser().parse_args()
        args.function(args)
        return 0
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(f"dependency_licenses: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
