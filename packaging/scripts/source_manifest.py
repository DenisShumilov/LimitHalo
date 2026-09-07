from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


SCHEMA = "1.0.0"
FORBIDDEN_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".git", ".svn", ".hg"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo"}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_unique(path: Path) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


def records(root: Path) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    case_insensitive_paths: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in FORBIDDEN_NAMES for part in relative.parts) or path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise ValueError(f"generated cache is forbidden: {relative.as_posix()}")
        stat = path.lstat()
        if path.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"reparse point is forbidden: {relative.as_posix()}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"unsupported source entry: {relative.as_posix()}")
        relative_text = relative.as_posix()
        folded = relative_text.casefold()
        if folded in case_insensitive_paths:
            raise ValueError(f"case-colliding source path is forbidden: {relative_text}")
        case_insensitive_paths.add(folded)
        data = path.read_bytes()
        output.append({"path": relative_text, "size": len(data), "sha256": sha256_bytes(data)})
    output.sort(key=lambda item: str(item["path"]))
    return output


def tree_sha256(items: list[dict[str, object]]) -> str:
    canonical = "".join(f'{item["path"]}\0{item["size"]}\0{item["sha256"]}\n' for item in items)
    return sha256_bytes(canonical.encode("utf-8"))


def create(root: Path, output: Path, contract_sha256: str) -> str:
    if output == root or root in output.parents:
        raise ValueError("source manifest output must be outside the governed source root")
    items = records(root)
    tree = tree_sha256(items)
    manifest = {
        "schemaVersion": SCHEMA,
        "contractSha256": contract_sha256,
        "treeSha256": tree,
        "recordCount": len(items),
        "records": items,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="ascii", newline="\n")
    return tree


def verify(root: Path, manifest_path: Path, contract_sha256: str) -> str:
    manifest = load_unique(manifest_path)
    if not isinstance(manifest, dict) or set(manifest) != {"schemaVersion", "contractSha256", "treeSha256", "recordCount", "records"}:
        raise ValueError("invalid source manifest schema")
    if manifest["schemaVersion"] != SCHEMA or manifest["contractSha256"] != contract_sha256:
        raise ValueError("source manifest identity mismatch")
    actual = records(root)
    actual_tree = tree_sha256(actual)
    if manifest["recordCount"] != len(actual) or manifest["records"] != actual or manifest["treeSha256"] != actual_tree:
        raise ValueError("source manifest closure mismatch")
    return actual_tree


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("create", "verify"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    manifest = args.manifest.resolve(strict=args.mode == "verify")
    if args.contract_sha256 != "0d01b424176f389cc9bb1e602dc0574ed1f2e16a5d6c0da62cd4fd2473e66962":
        raise ValueError("unexpected frozen contract identity")
    tree = create(root, manifest, args.contract_sha256) if args.mode == "create" else verify(root, manifest, args.contract_sha256)
    print(tree)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
