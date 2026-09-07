from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
AGENT = REPOSITORY / "LimitHalo-Agent.ps1"
MUTANT_CATALOG = REPOSITORY / "tests/mutants/mutant-catalog.json"
POWERSHELL = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
CANDIDATE = "a" * 64
WORKER_TEMP = REPOSITORY.parents[1] / "test-output/agent-worker"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_forced_stop_policy(source: str) -> None:
    required_once = (
        ".CloseMainWindow()",
        "WaitForExit(2000)",
        "[Diagnostics.Process]::GetProcessById($pidValue)",
        "$same.Id-ne$pidValue",
        "$sameStarted-ne$started",
        "GetFullPath($samePath).Equals([IO.Path]::GetFullPath($I.Executable)",
        "$same.Kill()",
        "WaitForExit(5000)",
    )
    for token in required_once:
        if source.count(token) != 1:
            raise AssertionError(f"forced-stop policy requires exactly one {token!r}")
    for forbidden in ("Stop-Process", "taskkill", "TerminateProcess", "Win32_Process"):
        if forbidden.lower() in source.lower():
            raise AssertionError(f"alternate termination route found: {forbidden}")


def assert_completion_policy(source: str) -> None:
    if source.count("'/OPENAFTERINSTALL=0'") != 1 or "/OPENAFTERINSTALL=1" in source:
        raise AssertionError("Setup must disable installer-owned launch exactly once")
    setup_source = source.split("function Setup-Once", 1)[1].split(
        "function Launch-Once", 1
    )[0]
    if setup_source.count("Start-Process -FilePath $B.Setup") != 1 or " -Wait " not in setup_source:
        raise AssertionError("helper must wait for exactly one Setup process")
    launch_source = source.split("function Launch-Once", 1)[1].split(
        "if(@($args).Count", 1
    )[0]
    if launch_source.count("Start-Process -FilePath $I.Executable") != 1:
        raise AssertionError("Launch-Once must start exactly one verified executable")
    if re.search(r"Start-Process -FilePath \$I\.Executable[^\r\n]*\s-Wait(?:\s|$)", launch_source):
        raise AssertionError("widget lifetime must not keep the helper open")
    required = (
        "for($attempt=0;$attempt-lt50;$attempt++)",
        "Start-Sleep -Milliseconds 100",
        "[Diagnostics.Process]::GetProcessById($launched.Id)",
        "GetFullPath($path).Equals([IO.Path]::GetFullPath($I.Executable)",
        "  throw 'LAUNCH_NOT_OBSERVED'\n}",
    )
    for token in required:
        if launch_source.count(token) != 1:
            raise AssertionError(f"completion policy requires exactly one {token!r}")
    if source.count("Launch-Once $c $after") != 1:
        raise AssertionError("verified installed candidate must be launched exactly once after Setup")
    ordered = (
        source.find("Setup-Once $c $b $i"),
        source.find("$after=Installed $c"),
        source.find("Launch-Once $c $after"),
        source.find("$running=Installed $c"),
    )
    if -1 in ordered or tuple(sorted(ordered)) != ordered:
        raise AssertionError("Setup, validation, launch, and observation ordering changed")


class LimitHaloAgentFixture(unittest.TestCase):
    def setUp(self) -> None:
        WORKER_TEMP.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=WORKER_TEMP)
        self.test_root = Path(self.temporary.name).resolve()
        self.root = self.test_root / "fixture"
        self.release = self.root / "release"
        self.bundle_candidate = CANDIDATE
        self.release.mkdir(parents=True)
        (self.root / "limit-halo-agent-fixture-v1.json").write_text(
            '{"schemaVersion":"1.0.0","kind":"LimitHaloAgentFixture"}\n', encoding="ascii"
        )
        shutil.copyfile(AGENT, self.release / "LimitHalo-Agent.ps1")
        (self.release / "LimitHalo-1.0.0-Setup.exe").write_bytes(b"fixture setup\n")
        (self.release / "LimitHalo-1.0.0-Windows-x64.zip").write_bytes(b"fixture zip\n")
        self.candidate_root = self.root / "fixture-candidate"
        self.candidate_root.mkdir()
        (self.candidate_root / "AILimitsWidget.exe").write_bytes(b"fixture widget\n")
        payload = {
            "schemaVersion": "1.0.0",
            "kind": "installed-payload",
            "internalProductId": "AILimitsWidget",
            "displayName": "LimitHalo",
            "displayNameStatus": "temporary-working-name",
            "version": "1.0.0",
            "architecture": "x64",
            "candidateId": self.bundle_candidate,
            "sourceIdentity": "fixture",
            "signatureStatus": "unsigned",
            "brokerSha256": "b" * 64,
            "files": [{
                "path": "AILimitsWidget.exe",
                "size": (self.candidate_root / "AILimitsWidget.exe").stat().st_size,
                "sha256": digest(self.candidate_root / "AILimitsWidget.exe"),
            }],
        }
        (self.candidate_root / "package-manifest.json").write_text(
            json.dumps(payload, separators=(",", ":")) + "\n", encoding="ascii"
        )
        self.write_release()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_release(self) -> None:
        artifacts = []
        for name in (
            "LimitHalo-1.0.0-Setup.exe",
            "LimitHalo-1.0.0-Windows-x64.zip",
            "LimitHalo-Agent.ps1",
        ):
            path = self.release / name
            artifacts.append({"path": name, "size": path.stat().st_size, "sha256": digest(path)})
        manifest = {
            "schemaVersion": "1.0.0",
            "kind": "release-bundle",
            "internalProductId": "AILimitsWidget",
            "displayName": "LimitHalo",
            "displayNameStatus": "temporary-working-name",
            "version": "1.0.0",
            "architecture": "x64",
            "candidateId": self.bundle_candidate,
            "sourceIdentity": "fixture",
            "signature": {"status": "unsigned", "verifiedSigner": None, "statement": "fixture"},
            "automaticUpdater": False,
            "hostedGitHubActionsObserved": False,
            "publicationPerformed": False,
            "payloadManifestSha256": "c" * 64,
            "dependencyLicenseManifestSha256": "d" * 64,
            "dependencyInventory": [],
            "buildInputs": [],
            "artifacts": artifacts,
        }
        manifest_path = self.release / "release-manifest.json"
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="ascii")
        names = [item["path"] for item in artifacts] + ["release-manifest.json"]
        (self.release / "SHA256SUMS.txt").write_text(
            "".join(f"{digest(self.release / name)}  {name}\n" for name in sorted(names)), encoding="ascii"
        )

    def set_bundle_candidate(self, candidate: str) -> None:
        manifest_path = self.candidate_root / "package-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
        manifest["candidateId"] = candidate
        manifest_path.write_text(
            json.dumps(manifest, separators=(",", ":")) + "\n", encoding="ascii"
        )
        self.bundle_candidate = candidate
        self.write_release()

    def run_agent(self, *arguments: str) -> tuple[int, dict[str, object], str]:
        environment = os.environ.copy()
        environment["LIMIT_HALO_TEST_ROOT"] = str(self.test_root)
        environment["LIMIT_HALO_AGENT_FIXTURE_ROOT"] = str(self.root)
        environment["PATH"] = ""
        completed = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(self.release / "LimitHalo-Agent.ps1"), *arguments],
            text=True,
            capture_output=True,
            env=environment,
            timeout=20,
            check=False,
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, completed.stdout + completed.stderr)
        value = json.loads(lines[0])
        self.assertEqual(set(value), {
            "schemaVersion", "kind", "action", "status", "code", "trust", "recommended", "performed",
            "candidateId", "setupSha256", "helperSha256", "acceptance", "installedState",
            "rollbackState", "changed", "launched", "settingsAccess", "authenticationPerformed",
        })
        self.assertLessEqual(len(lines[0].encode("utf-8")), 16_384)
        self.assertEqual(completed.stderr, "")
        self.assertNotRegex(
            lines[0],
            re.compile(r"(?i)(?:[a-z]:\\\\|\\\\\\\\|/users/|https?://|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})"),
        )
        self.assertNotRegex(lines[0], re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]"))
        self.assertIn(value["action"], {"Unknown", "Auto", "Status", "Install", "Repair"})
        self.assertIn(value["status"], {"OK", "ACTION_REQUIRED", "INVALID_OR_UNSAFE", "FAILED_SAFE"})
        self.assertIn(value["trust"], {"NOT_EVALUATED", "UNSIGNED_UNTRUSTED"})
        self.assertIn(value["recommended"], {"NONE", "INSTALL", "LAUNCH", "REPAIR"})
        self.assertIn(value["performed"], {"NONE", "INSTALL", "LAUNCH", "REPAIR"})
        self.assertIn(value["installedState"], {"ABSENT", "HEALTHY_RUNNING", "HEALTHY_STOPPED", "DAMAGED_OWNED", "UNSAFE"})
        self.assertIn(value["rollbackState"], {"ABSENT", "HEALTHY", "UNSAFE", "NOT_CHECKED"})
        self.assertEqual(value["settingsAccess"], "NOT_READ")
        self.assertIs(value["authenticationPerformed"], False)

        def shape(item: object, depth: int = 1) -> tuple[int, int]:
            if isinstance(item, dict):
                children = [shape(child, depth + 1) for child in item.values()]
            elif isinstance(item, list):
                children = [shape(child, depth + 1) for child in item]
            else:
                children = []
            return 1 + sum(nodes for nodes, _level in children), max(
                [depth, *(level for _nodes, level in children)]
            )

        nodes, depth = shape(value)
        self.assertLessEqual(nodes, 128)
        self.assertLessEqual(depth, 5)
        return completed.returncode, value, completed.stderr

    def accept(self, action: str = "Auto") -> dict[str, object]:
        code, preview, _ = self.run_agent(action)
        self.assertEqual(code, 10)
        self.assertEqual(preview["code"], "EXACT_ACCEPTANCE_REQUIRED")
        code, result, _ = self.run_agent(action, "-AcceptUnsignedExact", str(preview["acceptance"]))
        self.assertEqual(code, 0)
        return result

    def test_status_is_closed_read_only_and_python_free(self) -> None:
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        code, result, stderr = self.run_agent("Status")
        after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.assertEqual(code, 0)
        self.assertEqual((result["installedState"], result["recommended"]), ("ABSENT", "NONE"))
        self.assertEqual(before, after)
        self.assertEqual(stderr, "")

    def test_auto_installs_once_then_is_no_change(self) -> None:
        result = self.accept()
        self.assertEqual((result["performed"], result["installedState"]), ("INSTALL", "HEALTHY_RUNNING"))
        self.assertEqual((self.root / "setup-invocations.txt").read_text().strip(), "1")
        self.assertEqual((self.root / "launch-invocations.txt").read_text().strip(), "1")
        code, status, _ = self.run_agent("Auto")
        self.assertEqual(code, 0)
        self.assertEqual((status["performed"], status["recommended"]), ("NONE", "NONE"))
        self.assertEqual((self.root / "setup-invocations.txt").read_text().strip(), "1")
        self.assertEqual((self.root / "launch-invocations.txt").read_text().strip(), "1")

    def assert_launch_failure_is_bounded(self, mode: str, expected_code: int, expected: str) -> None:
        (self.root / "launch-mode.txt").write_text(mode + "\n", encoding="ascii")
        code, preview, _ = self.run_agent("Install")
        self.assertEqual(code, 10)
        code, result, _ = self.run_agent(
            "Install", "-AcceptUnsignedExact", str(preview["acceptance"])
        )
        self.assertEqual((code, result["code"]), (expected_code, expected))
        self.assertEqual((self.root / "setup-invocations.txt").read_text().strip(), "1")
        self.assertEqual((self.root / "launch-invocations.txt").read_text().strip(), "1")

    def test_launch_failure_does_not_retry_setup_or_launch(self) -> None:
        self.assert_launch_failure_is_bounded("FAIL", 30, "LAUNCH_FAILED")

    def test_unobserved_launch_fails_bounded_without_retry(self) -> None:
        self.assert_launch_failure_is_bounded("UNOBSERVED", 30, "LAUNCH_NOT_OBSERVED")

    def test_foreign_launched_process_fails_closed_without_retry(self) -> None:
        self.assert_launch_failure_is_bounded("FOREIGN", 10, "FOREIGN_PROCESS")

    def test_auto_launches_stopped_healthy_candidate(self) -> None:
        self.accept()
        (self.root / "process-state.txt").write_text("NONE\n", encoding="ascii")
        code, preview, _ = self.run_agent("Auto")
        self.assertEqual((code, preview["recommended"]), (10, "LAUNCH"))
        code, result, _ = self.run_agent("Auto", "-AcceptUnsignedExact", str(preview["acceptance"]))
        self.assertEqual(code, 0)
        self.assertEqual(result["performed"], "LAUNCH")

    def test_repair_owned_damage_once(self) -> None:
        self.accept()
        installed = self.root / "installed/AILimitsWidget/versions" / CANDIDATE / "AILimitsWidget.exe"
        installed.write_bytes(b"damaged")
        code, preview, _ = self.run_agent("Repair")
        self.assertEqual((code, preview["recommended"]), (10, "REPAIR"))
        code, result, _ = self.run_agent("Repair", "-AcceptUnsignedExact", str(preview["acceptance"]))
        self.assertEqual(code, 0)
        self.assertEqual(result["performed"], "REPAIR")
        self.assertEqual((self.root / "setup-invocations.txt").read_text().strip(), "2")

    def test_install_and_repair_no_change_and_prior_valid_upgrade(self) -> None:
        self.accept("Install")
        code, same, _ = self.run_agent("Install")
        self.assertEqual((code, same["recommended"], same["performed"]), (0, "NONE", "NONE"))
        code, healthy_repair, _ = self.run_agent("Repair")
        self.assertEqual(
            (code, healthy_repair["recommended"], healthy_repair["performed"]),
            (0, "NONE", "NONE"),
        )

        replacement = "b" * 64
        self.set_bundle_candidate(replacement)
        code, preview, _ = self.run_agent("Install")
        self.assertEqual((code, preview["recommended"]), (10, "INSTALL"))
        code, upgraded, _ = self.run_agent(
            "Install", "-AcceptUnsignedExact", str(preview["acceptance"])
        )
        self.assertEqual((code, upgraded["performed"]), (0, "INSTALL"))
        product = self.root / "installed/AILimitsWidget"
        self.assertEqual((product / "active-candidate.txt").read_text().strip(), replacement)
        self.assertEqual((product / "rollback-candidate.txt").read_text().strip(), CANDIDATE)

    def test_unknown_argument_and_foreign_state_fail_closed(self) -> None:
        code, result, _ = self.run_agent("Launch")
        self.assertEqual((code, result["code"]), (20, "INVALID_ACTION"))
        code, result, _ = self.run_agent("Auto", "Auto")
        self.assertEqual((code, result["code"]), (20, "INVALID_ARGUMENTS"))
        code, result, _ = self.run_agent()
        self.assertEqual((code, result["code"]), (20, "INVALID_ARGUMENTS"))
        code, result, _ = self.run_agent("Auto", "-AcceptUnsignedExact", "not-a-hash")
        self.assertEqual((code, result["code"]), (20, "INVALID_ACCEPTANCE"))
        self.accept()
        (self.root / "process-state.txt").write_text("FOREIGN\n", encoding="ascii")
        code, result, _ = self.run_agent("Auto")
        self.assertEqual((code, result["code"]), (10, "FOREIGN_PROCESS"))

    def test_tampered_bundle_invalidates_prior_acceptance(self) -> None:
        code, preview, _ = self.run_agent("Auto")
        self.assertEqual(code, 10)
        (self.release / "LimitHalo-1.0.0-Setup.exe").write_bytes(b"tampered")
        code, result, _ = self.run_agent("Auto", "-AcceptUnsignedExact", str(preview["acceptance"]))
        self.assertEqual((code, result["code"]), (20, "ARTIFACT_MISMATCH"))

    def test_fully_rehashed_bundle_still_invalidates_stale_acceptance(self) -> None:
        code, preview, _ = self.run_agent("Auto")
        self.assertEqual(code, 10)
        (self.release / "LimitHalo-1.0.0-Setup.exe").write_bytes(b"different fixture setup\n")
        self.write_release()
        code, result, _ = self.run_agent("Auto", "-AcceptUnsignedExact", str(preview["acceptance"]))
        self.assertEqual((code, result["code"]), (10, "EXACT_ACCEPTANCE_REQUIRED"))
        self.assertNotEqual(result["acceptance"], preview["acceptance"])

    def test_failed_setup_is_not_retried(self) -> None:
        (self.root / "fixture-operation.txt").write_text("FAIL_BEFORE_MARKERS\n", encoding="ascii")
        code, preview, _ = self.run_agent("Auto")
        code, result, _ = self.run_agent("Auto", "-AcceptUnsignedExact", str(preview["acceptance"]))
        self.assertEqual((code, result["code"]), (30, "INSTALLER_FAILED"))
        self.assertEqual((self.root / "setup-invocations.txt").read_text().strip(), "1")
        self.assertFalse((self.root / "installed/AILimitsWidget/active-candidate.txt").exists())

    def test_partial_stage_fails_once_without_activating(self) -> None:
        (self.root / "fixture-operation.txt").write_text("FAIL_AFTER_STAGE\n", encoding="ascii")
        code, preview, _ = self.run_agent("Auto")
        self.assertEqual(code, 10)
        code, result, _ = self.run_agent(
            "Auto", "-AcceptUnsignedExact", str(preview["acceptance"])
        )
        self.assertEqual((code, result["code"]), (30, "INSTALLER_FAILED"))
        self.assertEqual((self.root / "setup-invocations.txt").read_text().strip(), "1")
        self.assertFalse((self.root / "installed/AILimitsWidget/active-candidate.txt").exists())

    def test_failure_after_marker_restores_absent_marker_state(self) -> None:
        (self.root / "fixture-operation.txt").write_text("FAIL_AFTER_MARKER\n", encoding="ascii")
        code, preview, _ = self.run_agent("Install")
        self.assertEqual(code, 10)
        code, result, _ = self.run_agent("Install", "-AcceptUnsignedExact", str(preview["acceptance"]))
        self.assertEqual((code, result["code"]), (30, "INSTALLER_FAILED"))
        self.assertFalse((self.root / "installed/AILimitsWidget/active-candidate.txt").exists())
        self.assertEqual((self.root / "setup-invocations.txt").read_text().strip(), "1")

    def test_locked_owned_process_requires_manual_action_without_setup(self) -> None:
        self.accept()
        installed = self.root / "installed/AILimitsWidget/versions" / CANDIDATE / "AILimitsWidget.exe"
        installed.write_bytes(b"damaged")
        (self.root / "stop-mode.txt").write_text("LOCKED\n", encoding="ascii")
        code, preview, _ = self.run_agent("Repair")
        code, result, _ = self.run_agent("Repair", "-AcceptUnsignedExact", str(preview["acceptance"]))
        self.assertEqual((code, result["code"]), (10, "OWNED_PROCESS_BUSY"))
        self.assertEqual((self.root / "setup-invocations.txt").read_text().strip(), "1")

    def test_graceful_owned_stop_precedes_setup(self) -> None:
        self.accept()
        installed = self.root / "installed/AILimitsWidget/versions" / CANDIDATE / "AILimitsWidget.exe"
        installed.write_bytes(b"damaged")
        code, preview, _ = self.run_agent("Repair")
        code, result, _ = self.run_agent("Repair", "-AcceptUnsignedExact", str(preview["acceptance"]))
        self.assertEqual((code, result["performed"]), (0, "REPAIR"))
        self.assertEqual((self.root / "stop-events.txt").read_text().splitlines(), ["CLOSE"])
        self.assertEqual((self.root / "setup-invocations.txt").read_text().strip(), "2")

    def test_forced_exact_owned_stop_is_single_and_precedes_setup(self) -> None:
        self.accept()
        installed = self.root / "installed/AILimitsWidget/versions" / CANDIDATE / "AILimitsWidget.exe"
        installed.write_bytes(b"damaged")
        (self.root / "stop-mode.txt").write_text("FORCE\n", encoding="ascii")
        code, preview, _ = self.run_agent("Repair")
        code, result, _ = self.run_agent("Repair", "-AcceptUnsignedExact", str(preview["acceptance"]))
        self.assertEqual((code, result["performed"]), (0, "REPAIR"))
        self.assertEqual(
            (self.root / "stop-events.txt").read_text().splitlines(),
            ["CLOSE", "REVALIDATE", "KILL"],
        )
        self.assertEqual((self.root / "setup-invocations.txt").read_text().strip(), "2")

    def assert_stop_failure_blocks_setup(self, mode: str, expected: str) -> None:
        self.accept()
        installed = self.root / "installed/AILimitsWidget/versions" / CANDIDATE / "AILimitsWidget.exe"
        installed.write_bytes(b"damaged")
        (self.root / "stop-mode.txt").write_text(mode + "\n", encoding="ascii")
        code, preview, _ = self.run_agent("Repair")
        code, result, _ = self.run_agent("Repair", "-AcceptUnsignedExact", str(preview["acceptance"]))
        self.assertEqual((code, result["code"]), (10, expected))
        self.assertEqual((self.root / "setup-invocations.txt").read_text().strip(), "1")

    def test_replaced_pid_blocks_setup(self) -> None:
        self.assert_stop_failure_blocks_setup("REPLACED", "OWNED_PROCESS_BUSY")

    def test_foreign_revalidated_process_blocks_setup(self) -> None:
        self.assert_stop_failure_blocks_setup("FOREIGN", "FOREIGN_PROCESS")

    def test_stop_failure_blocks_setup(self) -> None:
        self.assert_stop_failure_blocks_setup("FAILURE", "OWNED_PROCESS_BUSY")

    def test_multiple_process_fails_before_close_and_setup(self) -> None:
        self.assert_stop_failure_blocks_setup("MULTIPLE_PROCESS", "OWNED_PROCESS_BUSY")
        self.assertFalse((self.root / "stop-events.txt").exists())

    def test_inaccessible_module_fails_before_close_and_setup(self) -> None:
        self.assert_stop_failure_blocks_setup("INACCESSIBLE_MODULE", "OWNED_PROCESS_BUSY")
        self.assertFalse((self.root / "stop-events.txt").exists())

    def test_kill_failure_records_one_termination_attempt_and_blocks_setup(self) -> None:
        self.assert_stop_failure_blocks_setup("KILL_FAIL", "OWNED_PROCESS_BUSY")
        self.assertEqual(
            (self.root / "stop-events.txt").read_text().splitlines(),
            ["CLOSE", "REVALIDATE", "KILL"],
        )

    def test_second_kill_request_is_rejected_without_retry_or_setup(self) -> None:
        self.assert_stop_failure_blocks_setup("SECOND_KILL", "OWNED_PROCESS_BUSY")
        self.assertEqual(
            (self.root / "stop-events.txt").read_text().splitlines(),
            ["CLOSE", "REVALIDATE", "KILL"],
        )

    def test_extra_candidate_file_and_foreign_shortcut_fail_closed(self) -> None:
        self.accept()
        candidate = self.root / "installed/AILimitsWidget/versions" / CANDIDATE
        (candidate / "unexpected.bin").write_bytes(b"foreign")
        code, result, _ = self.run_agent("Status")
        self.assertEqual((code, result["code"]), (20, "INSTALLED_STATE_UNSAFE"))
        (candidate / "unexpected.bin").unlink()
        (self.root / "shortcut-state.txt").write_text("FOREIGN\n", encoding="ascii")
        code, result, _ = self.run_agent("Status")
        self.assertEqual((code, result["code"]), (10, "FOREIGN_SHORTCUT"))

    def test_corrupt_active_marker_fails_closed_without_raw_exception(self) -> None:
        self.accept()
        (self.root / "installed/AILimitsWidget/active-candidate.txt").write_text("bad\n", encoding="ascii")
        code, result, stderr = self.run_agent("Status")
        self.assertEqual((code, result["code"]), (20, "MARKER_INVALID"))
        self.assertEqual(stderr, "")

    def test_corrupt_rollback_marker_requires_manual_action(self) -> None:
        self.accept()
        product = self.root / "installed/AILimitsWidget"
        (product / "rollback-candidate.txt").write_text("bad\n", encoding="ascii")
        code, result, _ = self.run_agent("Status")
        self.assertEqual((code, result["code"], result["rollbackState"]), (10, "ROLLBACK_UNSAFE", "UNSAFE"))

    def test_agent_source_contains_bounded_reparse_and_single_setup_controls(self) -> None:
        source = AGENT.read_text(encoding="utf-8")
        self.assertIn("Get-ChildItem -LiteralPath $root -Recurse -Directory -Force", source)
        self.assertIn("[IO.FileAttributes]::ReparsePoint", source)
        self.assertEqual(source.count("Setup-Once $c $b $i"), 1)
        self.assertEqual(source.count(".CloseMainWindow()"), 1)
        assert_forced_stop_policy(source)
        assert_completion_policy(source)
        self.assertNotIn("while($true)", source.replace(" ", ""))
        for forbidden in ("Invoke-WebRequest", "Start-BitsTransfer", "pip ", "python.exe"):
            self.assertNotIn(forbidden, source)

    def test_completion_policy_rejects_lifetime_and_launch_mutants(self) -> None:
        source = AGENT.read_text(encoding="utf-8")
        mutants = {
            "installer-owned post-install launch restored": source.replace(
                "'/OPENAFTERINSTALL=0'", "'/OPENAFTERINSTALL=1'", 1
            ),
            "widget lifetime wait added to helper launch": source.replace(
                " -PassThru\n  for($attempt", " -Wait -PassThru\n  for($attempt", 1
            ),
            "launch observation loop made unbounded": source.replace(
                "for($attempt=0;$attempt-lt50;$attempt++)",
                "for($attempt=0;$true;$attempt++)",
                1,
            ),
            "launched PID observation removed": source.replace(
                "[Diagnostics.Process]::GetProcessById($launched.Id)", "", 1
            ),
            "exact launched executable-path comparison weakened": "GetFullPath($path).Equals([IO.Path]::GetFullPath($path)".join(
                source.rsplit(
                    "GetFullPath($path).Equals([IO.Path]::GetFullPath($I.Executable)", 1
                )
            ),
            "duplicate explicit launch added": source.replace(
                "Start-Process -FilePath $I.Executable",
                "Start-Process -FilePath $I.Executable;Start-Process -FilePath $I.Executable",
                1,
            ),
        }
        catalog = json.loads(MUTANT_CATALOG.read_text(encoding="utf-8"))["G03"]
        self.assertEqual(set(mutants), {
            "installer-owned post-install launch restored",
            "widget lifetime wait added to helper launch",
            "launch observation loop made unbounded",
            "launched PID observation removed",
            "exact launched executable-path comparison weakened",
            "duplicate explicit launch added",
        })
        for name, mutant in mutants.items():
            with self.subTest(mutant=name):
                self.assertEqual(catalog.count(name), 1)
                self.assertNotEqual(mutant, source)
                with self.assertRaises(AssertionError):
                    assert_completion_policy(mutant)

    def test_forced_stop_policy_rejects_revalidation_and_termination_mutants(self) -> None:
        source = AGENT.read_text(encoding="utf-8")
        removals = (
            "[Diagnostics.Process]::GetProcessById($pidValue)",
            "$same.Id-ne$pidValue",
            "$sameStarted-ne$started",
            "GetFullPath($samePath).Equals([IO.Path]::GetFullPath($I.Executable)",
        )
        additions = (
            ".CloseMainWindow()",
            "$same.Kill()",
            "WaitForExit(2000)",
            "WaitForExit(5000)",
            " Stop-Process -Id $pidValue",
        )
        for token in removals:
            with self.subTest(mutant=f"remove:{token}"):
                with self.assertRaises(AssertionError):
                    assert_forced_stop_policy(source.replace(token, "", 1))
        for token in additions:
            with self.subTest(mutant=f"add:{token}"):
                with self.assertRaises(AssertionError):
                    assert_forced_stop_policy(source + token)

    def test_forced_stop_mutants_are_declared_in_catalog(self) -> None:
        catalog = json.loads(MUTANT_CATALOG.read_text(encoding="utf-8"))
        required = {
            "forced stop PID revalidation removed",
            "forced stop start-time revalidation removed",
            "forced stop executable-path revalidation removed",
            "second CloseMainWindow path added",
            "second process Kill path added",
            "second graceful or post-kill wait added",
            "alternate Stop-Process termination path added",
            "multiple process allowed before close",
            "inaccessible process module allowed before close",
            "kill failure retries or starts Setup",
            "second kill request accepted",
            "installer-owned post-install launch restored",
            "widget lifetime wait added to helper launch",
            "launch observation loop made unbounded",
            "launched PID observation removed",
            "exact launched executable-path comparison weakened",
            "duplicate explicit launch added",
        }
        self.assertTrue(required.issubset(set(catalog["G03"])))


if __name__ == "__main__":
    unittest.main()
