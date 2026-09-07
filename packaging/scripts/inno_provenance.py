from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping


CONTRACT_SHA256 = "0d01b424176f389cc9bb1e602dc0574ed1f2e16a5d6c0da62cd4fd2473e66962"
INSTALLER_SHA256 = "0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f"
COMPILER_SHA256 = "d06ebd38f38e3cee60a3c50cc45bd449d77e0bc6a5cabc607ea9886808e4de1a"
SIGNER_SUBJECT = "CN=Pyrsys B.V., O=Pyrsys B.V., S=Noord-Holland, C=NL"

LOCAL_FIELDS = {
    "status",
    "installerSha256",
    "installerSignature",
    "compilerIdentitySource",
    "compilerSha256",
    "networkEnabled",
    "clipboardEnabled",
    "hostInstallPerformed",
}
HOSTED_FIELDS = {
    "schemaVersion",
    "contractSha256",
    "status",
    "provenanceMode",
    "installerSha256",
    "installerSignature",
    "compilerIdentitySource",
    "compilerSha256",
    "networkEnabled",
    "hostInstallPerformed",
    "disposableRunner",
    "runnerEnvironment",
    "runnerOs",
    "imageOs",
    "githubRunId",
    "createdUtc",
}


def load_unique(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("receipt root must be an object")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_lock(path: Path) -> None:
    lock = load_unique(path)
    required = {
        "schemaVersion", "product", "version", "edition", "source", "inputPath",
        "size", "sha256", "authenticode", "installation",
    }
    if set(lock) != required or lock["schemaVersion"] != "1.0.0":
        raise ValueError("invalid Inno Setup lock schema")
    signature = lock["authenticode"]
    if not isinstance(signature, dict) or set(signature) != {"requiredStatus", "requiredLeafSubject"}:
        raise ValueError("invalid Inno Setup signature lock")
    if (
        lock["product"] != "Inno Setup"
        or lock["version"] != "7.1.0"
        or lock["edition"] != "x64"
        or lock["sha256"] != INSTALLER_SHA256
        or signature["requiredStatus"] != "Valid"
        or signature["requiredLeafSubject"] != SIGNER_SUBJECT
    ):
        raise ValueError("unexpected Inno Setup lock identity")


def validate_receipt(
    receipt_path: Path,
    compiler_path: Path,
    lock_path: Path,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment = os.environ if environment is None else environment
    validate_lock(lock_path)
    receipt = load_unique(receipt_path)
    actual_compiler = sha256(compiler_path)
    if actual_compiler != COMPILER_SHA256:
        raise ValueError("Inno Setup compiler does not match the frozen compiler")

    common = (
        receipt.get("status") == "PASS"
        and receipt.get("installerSha256") == INSTALLER_SHA256
        and receipt.get("installerSignature") == "Valid Pyrsys B.V."
        and receipt.get("compilerIdentitySource") == "pinned-signed-installer"
        and receipt.get("compilerSha256") == COMPILER_SHA256
    )
    if not common:
        raise ValueError("Inno Setup receipt identity mismatch")

    if set(receipt) == LOCAL_FIELDS:
        if environment.get("GITHUB_ACTIONS", "").casefold() == "true":
            raise ValueError("GitHub Actions must use hosted acquisition provenance")
        if (
            receipt["networkEnabled"] is not False
            or receipt["clipboardEnabled"] is not False
            or receipt["hostInstallPerformed"] is not False
        ):
            raise ValueError("local compiler receipt is not a pinned offline export")
        if receipt_path.name != "local-compiler-receipt.json" or compiler_path.name.casefold() != "iscc.exe":
            raise ValueError("local compiler receipt is not an intact pinned bundle")
        if receipt_path.parent.resolve() != compiler_path.parent.resolve():
            raise ValueError("local compiler and receipt must share the pinned toolchain root")
        return {
            "status": "PASS",
            "provenanceMode": "local-pinned-offline-compiler",
            "networkEnabled": False,
            "hostInstallPerformed": False,
            "compilerSha256": actual_compiler,
        }

    if set(receipt) != HOSTED_FIELDS:
        raise ValueError("unknown Inno Setup receipt schema")
    if (
        receipt["schemaVersion"] != "1.0.0"
        or receipt["contractSha256"] != CONTRACT_SHA256
        or receipt["provenanceMode"] != "github-hosted-disposable-runner-acquisition"
        or receipt["networkEnabled"] is not True
        or receipt["hostInstallPerformed"] is not True
        or receipt["disposableRunner"] is not True
        or receipt["runnerEnvironment"] != "github-hosted"
        or receipt["runnerOs"] != "Windows"
        or receipt["imageOs"] != "win25"
        or not re.fullmatch(r"[1-9][0-9]*", str(receipt["githubRunId"]))
        or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,7})?Z", str(receipt["createdUtc"]))
    ):
        raise ValueError("hosted acquisition receipt is not truthful and exact")
    expected_environment = {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Windows",
        "ImageOS": "win25",
    }
    for name, expected in expected_environment.items():
        if environment.get(name) != expected:
            raise ValueError(f"hosted acquisition environment mismatch: {name}")
    if environment.get("GITHUB_RUN_ID") != str(receipt["githubRunId"]):
        raise ValueError("hosted acquisition receipt is bound to another workflow run")
    return {
        "status": "PASS",
        "provenanceMode": receipt["provenanceMode"],
        "networkEnabled": True,
        "hostInstallPerformed": True,
        "disposableRunner": True,
        "compilerSha256": actual_compiler,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--installer-lock", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.contract_sha256 != CONTRACT_SHA256:
            raise ValueError("unexpected frozen contract identity")
        result = validate_receipt(
            args.receipt.resolve(strict=True),
            args.compiler.resolve(strict=True),
            args.installer_lock.resolve(strict=True),
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"inno_provenance: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
