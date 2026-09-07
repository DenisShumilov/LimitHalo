from __future__ import annotations

import argparse
import contextlib
import io
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY / "packaging" / "scripts"
VERSION = json.loads((REPOSITORY / "product.json").read_text(encoding="utf-8"))["version"]
sys.path.insert(0, str(SCRIPTS))

import release_policy  # noqa: E402
import release_tools  # noqa: E402
import dependency_licenses  # noqa: E402
import generate_version_info  # noqa: E402
import inno_provenance  # noqa: E402
import source_manifest  # noqa: E402


ROOTED_RELEASE_SCRIPTS = (
    "packaging/scripts/Test-BuildLocks.ps1",
    "packaging/scripts/Test-OwnedCleanup.ps1",
    "packaging/scripts/Test-OwnedShortcuts.ps1",
    "packaging/scripts/Build-NativeBroker.ps1",
    "packaging/scripts/Invoke-OwnedCleanupFixture.ps1",
)


def correction_policy_errors(root: Path) -> list[str]:
    errors: list[str] = []
    for relative in ROOTED_RELEASE_SCRIPTS:
        text = (root / relative).read_text(encoding="utf-8")
        if not re.search(r"\[Parameter\(Mandatory = \$true\)\]\[string\]\$AuthorizedStateRoot", text):
            errors.append(f"{relative} does not require AuthorizedStateRoot")
        if "strict descendant of AuthorizedStateRoot" not in text:
            errors.append(f"{relative} does not confine writable roots beneath AuthorizedStateRoot")
        for phrase in ("ReparsePoint", "[System.IO.DirectoryInfo]", "[System.IO.FileInfo]"):
            if phrase not in text:
                errors.append(f"{relative} missing reparse control: {phrase}")

    build_script = (root / "packaging/scripts/Build-Release.ps1").read_text(encoding="utf-8")
    if not re.search(r"(?m)^\s*AuthorizedStateRoot = \$authorizedState\s*$", build_script):
        errors.append("Build-Release.ps1 omits Build-NativeBroker AuthorizedStateRoot")
    for phrase in ("dependency_licenses.py", "--dependency-license-manifest"):
        if phrase not in build_script:
            errors.append(f"Build-Release.ps1 omits dependency closure control: {phrase}")

    fragment = (root / "packaging/scripts/Test-ReleaseFragment.ps1").read_text(encoding="utf-8")
    cleanup_call = re.search(r"Test-OwnedCleanup\.ps1'\)\s*`(?P<body>.*?)\| Out-Null", fragment, re.DOTALL)
    if cleanup_call is None or not re.search(r"(?m)^\s*-AuthorizedStateRoot \$state\s+`\s*$", cleanup_call.group("body")):
        errors.append("Test-ReleaseFragment.ps1 omits Test-OwnedCleanup AuthorizedStateRoot")

    for workflow_name in ("validate.yml", "release.yml"):
        workflow = (root / ".github/workflows" / workflow_name).read_text(encoding="utf-8")
        lock_call = re.search(r"Test-BuildLocks\.ps1'\)\s*`(?P<body>.*?)\| Out-Null", workflow, re.DOTALL)
        if lock_call is None or not re.search(r"(?m)^\s*-AuthorizedStateRoot \$env:LIMIT_HALO_STATE_ROOT\s*$", lock_call.group("body")):
            errors.append(f"{workflow_name} omits Test-BuildLocks AuthorizedStateRoot")

    helper_path = root / "packaging/scripts/dependency_licenses.py"
    if not helper_path.is_file():
        errors.append("dependency license helper is missing")
    else:
        helper = helper_path.read_text(encoding="utf-8")
        for phrase in (
            "altgraph-0.17.5-py2.py3-none-any.whl",
            "packaging-26.3-py3-none-any.whl",
            "pefile-2024.8.26-py3-none-any.whl",
            "pillow-12.3.0-cp312-cp312-win_amd64.whl",
            "pyinstaller-6.21.0-py3-none-win_amd64.whl",
            "pyinstaller_hooks_contrib-2026.6-py3-none-any.whl",
            "pywin32_ctypes-0.2.3-py3-none-any.whl",
            "setuptools-83.0.0-py3-none-any.whl",
            "licenses/dependency-licenses.json",
        ):
            if phrase not in helper:
                errors.append(f"dependency license closure missing identity: {phrase}")
    release_tools_text = (root / "packaging/scripts/release_tools.py").read_text(encoding="utf-8")
    for phrase in ("dependencyInventory", "dependencyLicenseManifestSha256", "validate_document"):
        if phrase not in release_tools_text:
            errors.append(f"release verifier missing dependency inventory control: {phrase}")
    return errors


class RedirectedCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configured = os.environ.get("LIMIT_HALO_TEST_ROOT")
        if not configured:
            raise RuntimeError("LIMIT_HALO_TEST_ROOT must name an explicit redirected fixture root")
        cls.shared_root = Path(configured).resolve()
        cls.shared_root.mkdir(parents=True, exist_ok=True)

    def fixture_dir(self, name: str) -> Path:
        path = self.shared_root / f"{self.__class__.__name__}-{self._testMethodName}-{name}"
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        return path


class PolicyControls(RedirectedCase):
    def test_positive_repository_passes(self) -> None:
        self.assertEqual(release_policy.validate_repository(REPOSITORY), [])

    def mutated_repository(self, name: str) -> Path:
        root = self.fixture_dir(name) / "repo"
        shutil.copytree(REPOSITORY, root)
        return root

    def assert_mutant_fails(self, root: Path, expected_fragment: str) -> None:
        errors = release_policy.validate_repository(root)
        self.assertTrue(errors, "mutant unexpectedly passed")
        self.assertTrue(any(expected_fragment in error for error in errors), errors)

    def assert_correction_mutant_fails(self, root: Path, expected_fragment: str) -> None:
        errors = correction_policy_errors(root)
        self.assertTrue(errors, "correction mutant unexpectedly passed")
        self.assertTrue(any(expected_fragment in error for error in errors), errors)

    def test_correction_policy_positive_control(self) -> None:
        self.assertEqual(correction_policy_errors(REPOSITORY), [])

    def test_missing_integration_disclosure_mutant_fails(self) -> None:
        root = self.mutated_repository("disclosure")
        path = root / "INTEGRATIONS.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "unsupported private Anthropic OAuth usage/refresh interface",
            "Claude compatibility interface",
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "missing disclosure")

    def test_softened_private_disclosure_mutant_fails(self) -> None:
        root = self.mutated_repository("softened")
        path = root / "README.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "unsupported private Anthropic OAuth usage/refresh interface",
            "supported Anthropic interface",
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "missing disclosure")

    def test_movable_action_tag_mutant_fails(self) -> None:
        root = self.mutated_repository("action")
        path = root / ".github" / "workflows" / "validate.yml"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/checkout@v4",
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "not pinned")

    def test_workflows_use_setup_python_output(self) -> None:
        for workflow_name in ("validate.yml", "release.yml"):
            workflow = (REPOSITORY / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
            self.assertIn("id: setup-python", workflow)
            self.assertIn("steps.setup-python.outputs.python-path", workflow)
            self.assertNotRegex(workflow, r"(?im)(?:^|[& (])python(?:\.exe)?\s+-|Get-Command\s+python")

    def test_bare_python_workflow_mutant_fails(self) -> None:
        root = self.mutated_repository("bare-python")
        path = root / ".github" / "workflows" / "validate.yml"
        text = path.read_text(encoding="utf-8")
        self.assertIn("& $python -I -B", text)
        path.write_text(text.replace("& $python -I -B", "python -I -B", 1), encoding="utf-8")
        self.assert_mutant_fails(root, "PATH-selected Python")

    def test_missing_isolated_python_flag_mutant_fails(self) -> None:
        root = self.mutated_repository("missing-isolated-python")
        path = root / "packaging" / "scripts" / "Build-Release.ps1"
        text = path.read_text(encoding="utf-8")
        self.assertIn("& $buildPython -I -B", text)
        path.write_text(text.replace("& $buildPython -I -B", "& $buildPython -B", 1), encoding="utf-8")
        self.assert_mutant_fails(root, "Python invocation missing exact -I -B")

    def test_nonexistent_workflow_command_mutant_fails(self) -> None:
        root = self.mutated_repository("missing-command")
        path = root / ".github" / "workflows" / "validate.yml"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "packaging/scripts/Test-ReleaseFragment.ps1",
            "packaging/scripts/Does-Not-Exist.ps1",
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "workflow references missing path")

    def test_missing_source_manifest_argument_mutant_fails(self) -> None:
        root = self.mutated_repository("missing-source-manifest")
        path = root / ".github" / "workflows" / "release.yml"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "-SourceManifestPath $env:LIMIT_HALO_SOURCE_MANIFEST",
            "-OmittedSourceManifestPath $env:LIMIT_HALO_SOURCE_MANIFEST",
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "missing clean build control")

    def test_raw_commit_source_identity_mutant_fails(self) -> None:
        root = self.mutated_repository("raw-source-identity")
        path = root / ".github" / "workflows" / "release.yml"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "-SourceIdentity $env:LIMIT_HALO_SOURCE_IDENTITY",
            "-SourceIdentity $env:SOURCE_REF",
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "raw checkout identity")

    def test_source_export_omission_mutant_fails(self) -> None:
        root = self.mutated_repository("source-export")
        path = root / ".github" / "workflows" / "release.yml"
        path.write_text(path.read_text(encoding="utf-8").replace("git archive", "git checkout-index"), encoding="utf-8")
        self.assert_mutant_fails(root, "missing clean build control")

    def test_acquisition_in_offline_phase_mutant_fails(self) -> None:
        root = self.mutated_repository("offline-acquisition")
        path = root / ".github" / "workflows" / "release.yml"
        text = path.read_text(encoding="utf-8")
        phase_marker = "      - name: Validate and build without acquisition"
        prefix, phase = text.split(phase_marker, 1)
        contract_marker = "          $contract = '0d01b424176f389cc9bb1e602dc0574ed1f2e16a5d6c0da62cd4fd2473e66962'\n"
        self.assertIn(contract_marker, phase)
        phase = phase.replace(
            contract_marker,
            "          Invoke-WebRequest -Uri 'https://invalid.example/' -OutFile acquired.bin\n" + contract_marker,
            1,
        )
        path.write_text(prefix + phase_marker + phase, encoding="utf-8")
        self.assert_mutant_fails(root, "offline build phase contains acquisition route")

    def test_fabricated_hosted_receipt_mutant_fails(self) -> None:
        root = self.mutated_repository("fabricated-receipt")
        path = root / ".github" / "workflows" / "release.yml"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "Acquire-HostedInno.ps1",
            "Write-FabricatedInnoReceipt.ps1",
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "missing clean build control")

    def test_installer_url_placeholder_mutant_fails(self) -> None:
        root = self.mutated_repository("installer-url")
        path = root / "packaging" / "installer" / "LimitHalo.iss"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "AppPublisher={#ProductPublisher}",
            "AppPublisher={#ProductPublisher}\nAppPublisherURL=https://github.com/OWNER/ai-limits-widget",
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "unverifiable URL metadata")

    def test_embedded_installer_source_without_notimestamp_mutant_fails(self) -> None:
        root = self.mutated_repository("embedded-source-timestamp")
        path = root / "packaging" / "installer" / "LimitHalo.iss"
        text = path.read_text(encoding="utf-8")
        source = 'Source: "{#SourceDir}\\*"; DestDir: "{app}\\versions\\{#CandidateId}"; Flags: ignoreversion recursesubdirs createallsubdirs notimestamp;'
        mutant = 'Source: "{#SourceDir}\\*"; DestDir: "{app}\\versions\\{#CandidateId}"; Flags: ignoreversion recursesubdirs createallsubdirs;'
        self.assertIn(source, text)
        path.write_text(text.replace(source, mutant, 1), encoding="utf-8")
        self.assert_mutant_fails(root, "embedded installer source missing notimestamp")

    def test_optional_outside_test_root_mutant_fails(self) -> None:
        root = self.mutated_repository("optional-test-root")
        path = root / "scripts" / "run_offline_tests.ps1"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "[Parameter(Mandatory = $true)][string]$TestOutputRoot",
            "[Parameter()][string]$TestOutputRoot",
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "does not require TestOutputRoot")

    def test_portable_beside_executable_doc_mutant_fails(self) -> None:
        root = self.mutated_repository("portable-doc")
        path = root / "README.md"
        text = path.read_text(encoding="utf-8").replace(
            "Both modes keep preferences in `%LOCALAPPDATA%\\AILimitsWidget\\config.json`, not beside the executable.",
            "Portable mode stores settings beside the executable.",
        )
        path.write_text(text, encoding="utf-8")
        self.assert_mutant_fails(root, "portable LocalAppData boundary")

    def test_wrong_file_parent_reparse_mutant_fails(self) -> None:
        root = self.mutated_repository("file-parent")
        path = root / "packaging" / "scripts" / "Build-Release.ps1"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "$item = $item.Directory",
            "$item = $item.Parent",
            1,
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "source-build/release closure")

    def test_outside_root_shortcut_mutant_fails(self) -> None:
        root = self.mutated_repository("shortcut")
        path = root / "packaging" / "installer" / "LimitHalo.iss"
        path.write_text(path.read_text(encoding="utf-8").replace(
            'Filename: "{app}\\versions\\{#CandidateId}\\{#ProductExe}"',
            'Filename: "{tmp}\\outside.exe"',
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "escapes active manifest-owned version")

    def test_unsigned_setup_has_no_execution_route(self) -> None:
        root = self.mutated_repository("unsigned-execution")
        path = root / "packaging" / "scripts" / "Build-Release.ps1"
        text = path.read_text(encoding="utf-8")
        self.assertIn("installerExecutionPerformed = $false", text)
        path.write_text(text.replace(
            "$setup = Join-Path $releaseRoot ('LimitHalo-' + $productVersion + '-Setup.exe')",
            "$setup = Join-Path $releaseRoot ('LimitHalo-' + $productVersion + '-Setup.exe')\nStart-Process -FilePath $setup -Wait",
            1,
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "unsigned installer execution route")

    def test_direct_shortcut_bypass_mutant_fails(self) -> None:
        root = self.mutated_repository("shortcut-bypass")
        path = root / "packaging" / "installer" / "LimitHalo.iss"
        text = path.read_text(encoding="utf-8")
        self.assertIn("RunOwnedShortcut('Ensure', 'Hud', Candidate)", text)
        path.write_text(text.replace(
            "RunOwnedShortcut('Ensure', 'Hud', Candidate)",
            "CreateShellLink('foreign')",
            1,
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "bypasses ownership-aware shortcut helper")

    def test_name_change_without_identity_mutant_fails(self) -> None:
        root = self.mutated_repository("name")
        path = root / "product.json"
        product = json.loads(path.read_text(encoding="utf-8"))
        product["displayName"] = "RenamedAfterFreeze"
        path.write_text(json.dumps(product, indent=2) + "\n", encoding="utf-8")
        self.assert_mutant_fails(root, "product identity mismatch")

    def test_license_change_without_identity_mutant_fails(self) -> None:
        root = self.mutated_repository("license")
        path = root / "LICENSE"
        path.write_text(path.read_text(encoding="utf-8").replace("MIT License", "Apache License", 1), encoding="utf-8")
        self.assert_mutant_fails(root, "license draft identity changed")

    def test_prebuilt_only_broker_mutant_fails(self) -> None:
        root = self.mutated_repository("prebuilt-broker")
        path = root / "packaging" / "scripts" / "Build-Release.ps1"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "Build-NativeBroker.ps1",
            "Use-PrebuiltBroker.ps1",
        ), encoding="utf-8")
        self.assert_mutant_fails(root, "source-build/release closure")

    def test_mandatory_authorized_root_omission_mutants_fail(self) -> None:
        scripts = (
            "packaging/scripts/Test-BuildLocks.ps1",
            "packaging/scripts/Test-OwnedCleanup.ps1",
            "packaging/scripts/Build-NativeBroker.ps1",
            "packaging/scripts/Invoke-OwnedCleanupFixture.ps1",
        )
        mandatory = "[Parameter(Mandatory = $true)][string]$AuthorizedStateRoot"
        for index, relative in enumerate(scripts):
            with self.subTest(script=relative):
                root = self.mutated_repository(f"root-omission-{index}")
                path = root / Path(relative)
                text = path.read_text(encoding="utf-8")
                self.assertIn(mandatory, text)
                path.write_text(text.replace(mandatory, "[Parameter()][string]$AuthorizedStateRoot", 1), encoding="utf-8")
                self.assert_correction_mutant_fails(root, f"{relative} does not require AuthorizedStateRoot")

    def test_native_caller_authorized_root_omission_mutant_fails(self) -> None:
        root = self.mutated_repository("native-caller-root-omission")
        path = root / "packaging" / "scripts" / "Build-Release.ps1"
        text = path.read_text(encoding="utf-8")
        self.assertIn("AuthorizedStateRoot = $authorizedState", text)
        path.write_text(text.replace("AuthorizedStateRoot = $authorizedState", "OmittedAuthorizedStateRoot = $authorizedState", 1), encoding="utf-8")
        self.assert_correction_mutant_fails(root, "omits Build-NativeBroker AuthorizedStateRoot")

    def test_cleanup_caller_authorized_root_omission_mutant_fails(self) -> None:
        root = self.mutated_repository("cleanup-caller-root-omission")
        path = root / "packaging" / "scripts" / "Test-ReleaseFragment.ps1"
        text = path.read_text(encoding="utf-8")
        marker = "-AuthorizedStateRoot $state `\n    -PythonExe $pythonRuntime"
        self.assertIn(marker, text)
        path.write_text(text.replace(marker, "-OmittedAuthorizedStateRoot $state `\n    -PythonExe $pythonRuntime", 1), encoding="utf-8")
        self.assert_correction_mutant_fails(root, "omits Test-OwnedCleanup AuthorizedStateRoot")

    def test_workflow_build_lock_root_omission_mutants_fail(self) -> None:
        marker = "-AuthorizedStateRoot $env:LIMIT_HALO_STATE_ROOT | Out-Null"
        for workflow_name in ("validate.yml", "release.yml"):
            with self.subTest(workflow=workflow_name):
                root = self.mutated_repository("workflow-root-" + workflow_name[:-4])
                path = root / ".github" / "workflows" / workflow_name
                text = path.read_text(encoding="utf-8")
                lock_call = text.index("Test-BuildLocks.ps1")
                marker_index = text.index(marker, lock_call)
                path.write_text(text[:marker_index] + "-Omitted" + text[marker_index:], encoding="utf-8")
                self.assert_correction_mutant_fails(root, "omits Test-BuildLocks AuthorizedStateRoot")


class SourceManifestControls(RedirectedCase):
    CONTRACT = "0d01b424176f389cc9bb1e602dc0574ed1f2e16a5d6c0da62cd4fd2473e66962"

    def test_clean_source_manifest_positive_control(self) -> None:
        fixture = self.fixture_dir("source-positive")
        source = fixture / "source"
        source.mkdir()
        (source / "alpha.txt").write_text("alpha\n", encoding="utf-8")
        (source / "nested").mkdir()
        (source / "nested" / "beta.bin").write_bytes(b"beta")
        manifest = fixture / "source-manifest.json"
        tree = source_manifest.create(source, manifest, self.CONTRACT)
        self.assertRegex(tree, r"^[0-9a-f]{64}$")
        self.assertEqual(source_manifest.verify(source, manifest, self.CONTRACT), tree)

    def test_undeclared_source_file_mutant_fails(self) -> None:
        fixture = self.fixture_dir("source-extra")
        source = fixture / "source"
        source.mkdir()
        (source / "declared.txt").write_text("declared\n", encoding="utf-8")
        manifest = fixture / "source-manifest.json"
        source_manifest.create(source, manifest, self.CONTRACT)
        (source / "undeclared.txt").write_text("mutant\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "closure mismatch"):
            source_manifest.verify(source, manifest, self.CONTRACT)

    def test_vcs_metadata_mutant_fails(self) -> None:
        fixture = self.fixture_dir("source-vcs")
        source = fixture / "source"
        (source / ".git").mkdir(parents=True)
        (source / ".git" / "config").write_text("fixture\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "generated cache is forbidden"):
            source_manifest.records(source)


class RootConfinementControls(RedirectedCase):
    def test_build_reparse_helper_file_directory_and_in_memory_mutants(self) -> None:
        fixture = self.fixture_dir("file-input")
        regular_file = fixture / "regular-input.bin"
        regular_file.write_bytes(b"regular fixture")
        regular_directory = fixture / "regular-directory"
        regular_directory.mkdir()
        build_script = REPOSITORY / "packaging" / "scripts" / "Build-Release.ps1"
        powershell = r"""
param([string]$BuildScript, [string]$FixtureFile, [string]$FixtureDirectory)
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($BuildScript, [ref]$tokens, [ref]$errors)
if (@($errors).Count -ne 0) { throw 'Build script did not parse' }
$function = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Assert-NotReparse'
}, $true)
if ($null -eq $function) { throw 'Assert-NotReparse function was not found' }
$source = $function.Extent.Text
if ($source -notmatch '\[System\.IO\.DirectoryInfo\]' -or $source -notmatch '\[System\.IO\.FileInfo\]' -or $source -match 'PSIsContainer') {
    throw 'Assert-NotReparse source oracle failed'
}
& ([ScriptBlock]::Create($source))
Assert-NotReparse -PathValue $FixtureFile -Label 'Regular fixture file'
Assert-NotReparse -PathValue $FixtureDirectory -Label 'Regular fixture directory'
$fileMutant = $source.Replace('elseif ($item -is [System.IO.FileInfo])', 'elseif ($item -is [System.IO.DirectoryInfo])')
if ($fileMutant -ceq $source) { throw 'File mutant was not applied' }
& ([ScriptBlock]::Create($fileMutant))
$fileDetected = $false
try { Assert-NotReparse -PathValue $FixtureFile -Label 'File mutant' } catch { $fileDetected = $true }
if (-not $fileDetected) { throw 'In-memory file-type mutant was not detected' }
$directoryMutant = $source.Replace('if ($item -is [System.IO.DirectoryInfo])', 'if ($item -is [System.IO.FileInfo])')
if ($directoryMutant -ceq $source) { throw 'Directory mutant was not applied' }
& ([ScriptBlock]::Create($directoryMutant))
$directoryDetected = $false
try { Assert-NotReparse -PathValue $FixtureDirectory -Label 'Directory mutant' } catch { $directoryDetected = $true }
if (-not $directoryDetected) { throw 'In-memory directory-type mutant was not detected' }
"PASS"
"""
        helper = fixture / "invoke-helper.ps1"
        helper.write_text(powershell, encoding="utf-8")
        result = subprocess.run(
            [
                "pwsh", "-NoProfile", "-NonInteractive", "-File", str(helper),
                "-BuildScript", str(build_script), "-FixtureFile", str(regular_file),
                "-FixtureDirectory", str(regular_directory),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_offline_runner_rejects_outside_output_root(self) -> None:
        fixture = self.fixture_dir("offline-outside")
        state = fixture / "state"
        outside = fixture / "outside"
        state.mkdir()
        candidate = state / "candidate"
        scripts = candidate / "scripts"
        scripts.mkdir(parents=True)
        runner = scripts / "run_offline_tests.ps1"
        shutil.copy2(REPOSITORY / "scripts" / "run_offline_tests.ps1", runner)
        result = subprocess.run(
            [
                "pwsh", "-NoProfile", "-NonInteractive", "-File",
                str(runner),
                "-AuthorizedStateRoot", str(state),
                "-TestOutputRoot", str(outside),
                "-PythonExe", sys.executable,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("strict descendant of AuthorizedStateRoot", result.stdout + result.stderr)

    def test_offline_runner_rejects_windowsapps_alias(self) -> None:
        fixture = self.fixture_dir("offline-alias")
        state = fixture / "state"
        candidate = state / "candidate"
        scripts = candidate / "scripts"
        scripts.mkdir(parents=True)
        runner = scripts / "run_offline_tests.ps1"
        shutil.copy2(REPOSITORY / "scripts" / "run_offline_tests.ps1", runner)
        alias = state / "Microsoft" / "WindowsApps" / "python.exe"
        alias.parent.mkdir(parents=True)
        shutil.copy2(sys.executable, alias)
        result = subprocess.run(
            [
                "pwsh", "-NoProfile", "-NonInteractive", "-File", str(runner),
                "-AuthorizedStateRoot", str(state),
                "-TestOutputRoot", str(state / "output"),
                "-PythonExe", str(alias),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an app-execution alias", result.stdout + result.stderr)

    def test_direct_writer_outside_root_mutants_fail_before_work(self) -> None:
        fixture = self.fixture_dir("direct-writer-outside")
        state = fixture / "state"
        repository = state / "repository"
        wheelhouse = state / "wheelhouse"
        source = state / "source"
        output = state / "output"
        resources = state / "resources"
        for path in (repository, wheelhouse, source, output, resources):
            path.mkdir(parents=True, exist_ok=True)
        lock = state / "native-build.lock.json"
        lock.write_text("{}\n", encoding="utf-8")
        outside = fixture / "outside"
        outside.mkdir()
        product = outside / "product"
        product.mkdir()
        invocations = (
            (
                "Test-BuildLocks.ps1",
                ["-RepositoryRoot", str(repository), "-WheelhouseRoot", str(wheelhouse), "-TestOutputRoot", str(outside)],
            ),
            (
                "Test-OwnedCleanup.ps1",
                ["-RepositoryRoot", str(repository), "-TestOutputRoot", str(outside), "-PythonExe", sys.executable],
            ),
            (
                "Build-NativeBroker.ps1",
                [
                    "-SourceRoot", str(source), "-OutputRoot", str(outside), "-VsWherePath", sys.executable,
                    "-GeneratedResourceRoot", str(resources), "-NativeBuildLockPath", str(lock),
                ],
            ),
            (
                "Invoke-OwnedCleanupFixture.ps1",
                ["-ProductRoot", str(product)],
            ),
        )
        for script_name, arguments in invocations:
            with self.subTest(script=script_name):
                result = subprocess.run(
                    [
                        "pwsh", "-NoProfile", "-NonInteractive", "-File",
                        str(REPOSITORY / "packaging" / "scripts" / script_name),
                        *arguments,
                        "-AuthorizedStateRoot", str(state),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("strict descendant of AuthorizedStateRoot", result.stdout + result.stderr)


class DependencyLicenseControls(RedirectedCase):
    def write_fixture_closure(self, payload: Path) -> Path:
        records: list[dict[str, object]] = []
        for component in dependency_licenses.expected_inventory():
            component_id = component["id"]
            safe_id = component_id.replace(":", "-")
            roles = ["metadata", "license"] if component["kind"] == "python-wheel" else ["license"]
            if component_id == "asset:lobehub-icons":
                roles = ["license", "attribution"]
            for role in roles:
                relative = f"licenses/fixture/{safe_id}/{role}.txt"
                data = f"{component_id} {role}\n".encode("utf-8")
                target = payload / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                records.append({
                    "componentId": component_id,
                    "path": relative,
                    "role": role,
                    "size": len(data),
                    "sha256": dependency_licenses.sha256_bytes(data),
                })
        records.sort(key=lambda item: str(item["path"]))
        manifest = payload / Path(dependency_licenses.MANIFEST_RELATIVE_PATH)
        value = {
            "schemaVersion": dependency_licenses.SCHEMA_VERSION,
            "kind": dependency_licenses.MANIFEST_KIND,
            "inventory": dependency_licenses.expected_inventory(),
            "files": records,
        }
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="ascii")
        dependency_licenses.validate_payload_closure(manifest, payload)
        return manifest

    def test_exact_python_lock_inventory_positive_control(self) -> None:
        dependency_licenses.validate_python_lock(REPOSITORY / "requirements" / "python-build.lock.json")
        wheel_components = [item for item in dependency_licenses.expected_inventory() if item["kind"] == "python-wheel"]
        self.assertEqual(len(wheel_components), 8)

    def test_wheel_collector_preserves_top_level_and_vendored_records(self) -> None:
        fixture = self.fixture_dir("wheel-records")
        wheel = fixture / "fixture-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("fixture-1.0.dist-info/METADATA", "Name: fixture\nVersion: 1.0\n")
            archive.writestr("fixture-1.0.dist-info/LICENSE", "fixture license\n")
            archive.writestr("fixture/_vendor/vendor-2.0.dist-info/METADATA", "Name: vendor\nVersion: 2.0\n")
            archive.writestr("fixture/_vendor/vendor-2.0.dist-info/licenses/LICENSE", "vendor license\n")
        identity = {
            "id": "python-wheel:fixture",
            "kind": "python-wheel",
            "name": "fixture",
            "version": "1.0",
            "source": wheel.name,
            "sourceSha256": dependency_licenses.sha256(wheel),
        }
        payload = fixture / "payload"
        payload.mkdir()
        records = dependency_licenses.collect_wheel(wheel, identity, payload)
        self.assertEqual(sum(item["role"] == "metadata" for item in records), 2)
        self.assertEqual(sum(item["role"] == "license" for item in records), 2)

    def test_missing_license_mutant_fails(self) -> None:
        payload = self.fixture_dir("missing-license") / "payload"
        payload.mkdir()
        manifest = self.write_fixture_closure(payload)
        value = json.loads(manifest.read_text(encoding="ascii"))
        target_id = "python-wheel:altgraph"
        value["files"] = [
            item for item in value["files"]
            if item["componentId"] != target_id or item["role"] == "metadata"
        ]
        with self.assertRaisesRegex(ValueError, "missing license or METADATA closure"):
            dependency_licenses.validate_document(value)

    def test_deleted_license_file_mutant_fails(self) -> None:
        payload = self.fixture_dir("deleted-license") / "payload"
        payload.mkdir()
        manifest = self.write_fixture_closure(payload)
        value = json.loads(manifest.read_text(encoding="ascii"))
        deleted = next(item for item in value["files"] if item["role"] == "license")
        (payload / Path(deleted["path"])).unlink()
        with self.assertRaisesRegex(ValueError, "missing dependency license file"):
            dependency_licenses.validate_payload_closure(manifest, payload)


class InnoProvenanceControls(RedirectedCase):
    def compiler_and_hash(self, name: str) -> tuple[Path, str]:
        compiler = self.fixture_dir(name) / "ISCC.exe"
        compiler.write_bytes(b"fixture Inno compiler")
        return compiler, inno_provenance.sha256(compiler)

    def local_receipt(self, path: Path, compiler_hash: str) -> None:
        path.write_text(json.dumps({
            "status": "PASS",
            "installerSha256": inno_provenance.INSTALLER_SHA256,
            "installerSignature": "Valid Pyrsys B.V.",
            "compilerIdentitySource": "pinned-signed-installer",
            "compilerSha256": compiler_hash,
            "networkEnabled": False,
            "clipboardEnabled": False,
            "hostInstallPerformed": False,
        }, indent=2) + "\n", encoding="utf-8")

    def hosted_value(self, compiler_hash: str) -> dict[str, object]:
        return {
            "schemaVersion": "1.0.0",
            "contractSha256": inno_provenance.CONTRACT_SHA256,
            "status": "PASS",
            "provenanceMode": "github-hosted-disposable-runner-acquisition",
            "installerSha256": inno_provenance.INSTALLER_SHA256,
            "installerSignature": "Valid Pyrsys B.V.",
            "compilerIdentitySource": "pinned-signed-installer",
            "compilerSha256": compiler_hash,
            "networkEnabled": True,
            "hostInstallPerformed": True,
            "disposableRunner": True,
            "runnerEnvironment": "github-hosted",
            "runnerOs": "Windows",
            "imageOs": "win25",
            "githubRunId": "12345",
            "createdUtc": "2026-08-13T14:00:00Z",
        }

    @staticmethod
    def hosted_environment() -> dict[str, str]:
        return {
            "GITHUB_ACTIONS": "true",
            "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_OS": "Windows",
            "ImageOS": "win25",
            "GITHUB_RUN_ID": "12345",
        }

    def test_local_pinned_receipt_positive_control(self) -> None:
        compiler, compiler_hash = self.compiler_and_hash("local-positive")
        receipt = compiler.parent / "local-compiler-receipt.json"
        self.local_receipt(receipt, compiler_hash)
        with mock.patch.object(inno_provenance, "COMPILER_SHA256", compiler_hash):
            result = inno_provenance.validate_receipt(
                receipt, compiler, REPOSITORY / "requirements" / "inno-setup.lock.json", {}
            )
        self.assertEqual(result["provenanceMode"], "local-pinned-offline-compiler")
        self.assertFalse(result["networkEnabled"])

    def test_local_network_enabled_mutant_fails(self) -> None:
        compiler, compiler_hash = self.compiler_and_hash("local-network-mutant")
        receipt = compiler.parent / "local-compiler-receipt.json"
        self.local_receipt(receipt, compiler_hash)
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["networkEnabled"] = True
        receipt.write_text(json.dumps(value) + "\n", encoding="utf-8")
        with mock.patch.object(inno_provenance, "COMPILER_SHA256", compiler_hash):
            with self.assertRaisesRegex(ValueError, "pinned offline export"):
                inno_provenance.validate_receipt(
                    receipt, compiler, REPOSITORY / "requirements" / "inno-setup.lock.json", {}
                )

    def test_hosted_receipt_positive_control(self) -> None:
        compiler, compiler_hash = self.compiler_and_hash("hosted-positive")
        receipt = compiler.parent / "hosted-acquisition-receipt.json"
        receipt.write_text(json.dumps(self.hosted_value(compiler_hash)) + "\n", encoding="utf-8")
        with mock.patch.object(inno_provenance, "COMPILER_SHA256", compiler_hash):
            result = inno_provenance.validate_receipt(
                receipt,
                compiler,
                REPOSITORY / "requirements" / "inno-setup.lock.json",
                self.hosted_environment(),
            )
        self.assertTrue(result["networkEnabled"])
        self.assertTrue(result["hostInstallPerformed"])
        self.assertTrue(result["disposableRunner"])

    def test_fabricated_host_install_mutant_fails(self) -> None:
        compiler, compiler_hash = self.compiler_and_hash("hosted-install-mutant")
        value = self.hosted_value(compiler_hash)
        value["hostInstallPerformed"] = False
        receipt = compiler.parent / "hosted-acquisition-receipt.json"
        receipt.write_text(json.dumps(value) + "\n", encoding="utf-8")
        with mock.patch.object(inno_provenance, "COMPILER_SHA256", compiler_hash):
            with self.assertRaisesRegex(ValueError, "not truthful and exact"):
                inno_provenance.validate_receipt(
                    receipt,
                    compiler,
                    REPOSITORY / "requirements" / "inno-setup.lock.json",
                    self.hosted_environment(),
                )

    def test_wrong_hosted_run_binding_mutant_fails(self) -> None:
        compiler, compiler_hash = self.compiler_and_hash("hosted-run-mutant")
        receipt = compiler.parent / "hosted-acquisition-receipt.json"
        receipt.write_text(json.dumps(self.hosted_value(compiler_hash)) + "\n", encoding="utf-8")
        environment = self.hosted_environment()
        environment["GITHUB_RUN_ID"] = "99999"
        with mock.patch.object(inno_provenance, "COMPILER_SHA256", compiler_hash):
            with self.assertRaisesRegex(ValueError, "another workflow run"):
                inno_provenance.validate_receipt(
                    receipt,
                    compiler,
                    REPOSITORY / "requirements" / "inno-setup.lock.json",
                    environment,
                )


class ManifestControls(RedirectedCase):
    def build_payload(self, name: str) -> tuple[Path, Path]:
        root = self.fixture_dir(name) / "payload"
        root.mkdir()
        (root / "AILimitsWidget.exe").write_bytes(b"fixture-widget")
        (root / "ClaudeUsageBroker.exe").write_bytes(b"fixture-broker")
        shutil.copyfile(REPOSITORY / "LimitHalo-Agent.ps1", root / "LimitHalo-Agent.ps1")
        (root / "README.md").write_text("fixture only\n", encoding="utf-8")
        manifest = root / "package-manifest.json"
        args = argparse.Namespace(
            root=root,
            output=manifest,
            product=REPOSITORY / "product.json",
            source_identity="fixture-source-v1",
            broker_relative="ClaudeUsageBroker.exe",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            release_tools.command_payload_manifest(args)
        return root, manifest

    def test_payload_manifest_positive_control(self) -> None:
        root, manifest = self.build_payload("positive")
        value = release_tools.validate_payload_manifest(manifest, root, allow_portable_flag=False)
        self.assertRegex(str(value["candidateId"]), r"^[0-9a-f]{64}$")

    def test_hash_substitution_mutant_fails(self) -> None:
        root, manifest = self.build_payload("hash")
        (root / "ClaudeUsageBroker.exe").write_bytes(b"substituted-broker")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            release_tools.validate_payload_manifest(manifest, root, allow_portable_flag=False)

    def test_unowned_extra_file_mutant_fails(self) -> None:
        root, manifest = self.build_payload("extra")
        (root / "stale-after-upgrade.dll").write_bytes(b"stale")
        with self.assertRaisesRegex(ValueError, "file-set mismatch"):
            release_tools.validate_payload_manifest(manifest, root, allow_portable_flag=False)

    def test_unowned_manifest_path_mutant_fails(self) -> None:
        root, manifest = self.build_payload("escape")
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["files"][0]["path"] = "../outside.txt"
        manifest.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsafe payload path"):
            release_tools.validate_payload_manifest(manifest, root, allow_portable_flag=False)

    def test_deterministic_portable_zip(self) -> None:
        root, _ = self.build_payload("zip")
        (root / "portable.flag").write_text("fixture portable\n", encoding="utf-8")
        output = self.fixture_dir("zip-output")
        first = output / "first.zip"
        second = output / "second.zip"
        for target in (first, second):
            release_tools.command_zip(argparse.Namespace(source=root, output=target, prefix="LimitHalo"))
        self.assertEqual(release_tools.sha256(first), release_tools.sha256(second))

    def test_stdlib_zip_normalization_removes_member_order_variance(self) -> None:
        output = self.fixture_dir("stdlib-order")
        first = output / "first.zip"
        second = output / "second.zip"
        names = ("encodings/utf_8.pyc", "stat.pyc", "ntpath.pyc")
        for target, order in ((first, names), (second, tuple(reversed(names)))):
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
                for index, name in enumerate(order):
                    info = zipfile.ZipInfo(name, (2025, 1, index + 1, 0, 0, 0))
                    archive.writestr(info, ("fixture:" + name).encode("ascii"))
            release_tools.command_normalize_stdlib_zip(argparse.Namespace(archive=target))
        self.assertEqual(release_tools.sha256(first), release_tools.sha256(second))
        with zipfile.ZipFile(first, "r") as archive:
            self.assertEqual(archive.namelist(), sorted(names))
            self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))

    def test_stdlib_zip_unsafe_member_mutant_fails(self) -> None:
        archive_path = self.fixture_dir("stdlib-escape") / "base_library.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../escape.pyc", b"mutant")
        with self.assertRaisesRegex(ValueError, "unsafe member"):
            release_tools.command_normalize_stdlib_zip(argparse.Namespace(archive=archive_path))


class MetadataControls(RedirectedCase):
    def test_product_schema_consumers_accept_installer_fields(self) -> None:
        product = generate_version_info.load_product(REPOSITORY / "product.json")
        self.assertEqual(product["installerArchitecture"], "signing-ready-explicitly-unsigned-preview")
        self.assertEqual(product["aiAgentEntryPoint"], "LimitHalo-Agent.ps1")
        self.assertEqual(product["aiAgentProtocol"], "limit-halo-agent/1")
        self.assertEqual(release_tools.product_identity(REPOSITORY / "product.json"), product)

    def test_product_installer_field_mutants_fail(self) -> None:
        original = json.loads((REPOSITORY / "product.json").read_text(encoding="utf-8"))
        for field, value in (
            ("installerArchitecture", "signed"),
            ("aiAgentEntryPoint", "Other-Agent.ps1"),
            ("aiAgentProtocol", "open-agent/99"),
        ):
            with self.subTest(field=field):
                mutant = dict(original)
                mutant[field] = value
                path = self.fixture_dir("product-" + field) / "product.json"
                path.write_text(json.dumps(mutant, indent=2) + "\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    generate_version_info.load_product(path)

    def test_version_metadata_is_consistent(self) -> None:
        output = self.fixture_dir("metadata")
        product = generate_version_info.load_product(REPOSITORY / "product.json")
        widget_manifest = output / "AILimitsWidget.manifest"
        broker_manifest = output / "ClaudeUsageBroker.manifest"
        version_file = output / "AILimitsWidget.version.txt"
        broker_rc = output / "ClaudeUsageBroker.version.rc"
        generate_version_info.write_application_manifest(widget_manifest, dpi_aware=True)
        generate_version_info.write_application_manifest(broker_manifest, dpi_aware=False)
        generate_version_info.write_pyinstaller_version(product, version_file)
        generate_version_info.write_broker_rc(product, broker_rc)
        self.assertIn("PerMonitorV2", widget_manifest.read_text(encoding="utf-8"))
        self.assertNotIn("PerMonitorV2", broker_manifest.read_text(encoding="utf-8"))
        self.assertIn("OriginalFilename', 'AILimitsWidget.exe", version_file.read_text(encoding="utf-8"))
        self.assertIn('VALUE "OriginalFilename", "ClaudeUsageBroker.exe"', broker_rc.read_text(encoding="utf-8"))
        self.assertIn(",".join(str(part) for part in generate_version_info.version_tuple(VERSION)), broker_rc.read_text(encoding="utf-8"))


class ReleaseBundleControls(ManifestControls):
    def build_release(self, name: str) -> Path:
        payload, manifest = self.build_payload(name + "-payload")
        dependency_manifest = DependencyLicenseControls.write_fixture_closure(self, payload)
        with contextlib.redirect_stdout(io.StringIO()):
            release_tools.command_payload_manifest(argparse.Namespace(
                root=payload,
                output=manifest,
                product=REPOSITORY / "product.json",
                source_identity="fixture-source-v1",
                broker_relative="ClaudeUsageBroker.exe",
            ))
        (payload / "portable.flag").write_text("AILimitsWidget portable mode v1\n", encoding="utf-8")
        release_root = self.fixture_dir(name + "-release")
        setup = release_root / f"LimitHalo-{VERSION}-Setup.exe"
        portable = release_root / f"LimitHalo-{VERSION}-Windows-x64.zip"
        agent = release_root / "LimitHalo-Agent.ps1"
        release_manifest = release_root / "release-manifest.json"
        sums = release_root / "SHA256SUMS.txt"
        setup.write_bytes(b"fixture setup executable")
        shutil.copyfile(REPOSITORY / "LimitHalo-Agent.ps1", agent)
        release_tools.command_zip(argparse.Namespace(source=payload, output=portable, prefix="LimitHalo"))
        release_tools.command_release_manifest(argparse.Namespace(
            output=release_manifest,
            product=REPOSITORY / "product.json",
            payload_manifest=manifest,
            dependency_license_manifest=dependency_manifest,
            source_identity="fixture-source-v1",
            artifact=[str(setup), str(portable), str(agent)],
            input=[
                str(REPOSITORY / "product.json"),
                str(REPOSITORY / "requirements" / "python-build.lock.json"),
                str(REPOSITORY / "requirements" / "native-build.lock.json"),
                str(REPOSITORY / "requirements" / "inno-setup.lock.json"),
            ],
        ))
        release_tools.command_checksums(argparse.Namespace(
            output=sums,
            artifact=[str(setup), str(portable), str(agent), str(release_manifest)],
        ))
        return release_root

    def test_release_bundle_positive_control(self) -> None:
        root = self.build_release("positive")
        with contextlib.redirect_stdout(io.StringIO()):
            release_tools.command_verify(argparse.Namespace(release_root=root))

        manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {entry["path"] for entry in manifest["artifacts"]},
            {
                f"LimitHalo-{VERSION}-Setup.exe",
                f"LimitHalo-{VERSION}-Windows-x64.zip",
                "LimitHalo-Agent.ps1",
            },
        )
        checksum_names = {
            line.split("  ", 1)[1]
            for line in (root / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines()
        }
        self.assertEqual(checksum_names, {
            f"LimitHalo-{VERSION}-Setup.exe",
            f"LimitHalo-{VERSION}-Windows-x64.zip",
            "LimitHalo-Agent.ps1",
            "release-manifest.json",
        })
        with zipfile.ZipFile(root / f"LimitHalo-{VERSION}-Windows-x64.zip", "r") as archive:
            self.assertEqual(
                archive.read("LimitHalo/LimitHalo-Agent.ps1"),
                (root / "LimitHalo-Agent.ps1").read_bytes(),
            )

    def test_standalone_payload_agent_mismatch_fails_manifest_generation(self) -> None:
        payload, manifest = self.build_payload("agent-mismatch-payload")
        dependency_manifest = DependencyLicenseControls.write_fixture_closure(self, payload)
        with contextlib.redirect_stdout(io.StringIO()):
            release_tools.command_payload_manifest(argparse.Namespace(
                root=payload,
                output=manifest,
                product=REPOSITORY / "product.json",
                source_identity="fixture-source-v1",
                broker_relative="ClaudeUsageBroker.exe",
            ))
        release_root = self.fixture_dir("agent-mismatch-release")
        setup = release_root / f"LimitHalo-{VERSION}-Setup.exe"
        portable = release_root / f"LimitHalo-{VERSION}-Windows-x64.zip"
        agent = release_root / "LimitHalo-Agent.ps1"
        setup.write_bytes(b"fixture setup executable")
        agent.write_bytes(b"mutated helper")
        release_tools.command_zip(argparse.Namespace(source=payload, output=portable, prefix="LimitHalo"))
        with self.assertRaisesRegex(ValueError, "standalone and payload AI agent bytes differ"):
            release_tools.command_release_manifest(argparse.Namespace(
                output=release_root / "release-manifest.json",
                product=REPOSITORY / "product.json",
                payload_manifest=manifest,
                dependency_license_manifest=dependency_manifest,
                source_identity="fixture-source-v1",
                artifact=[str(setup), str(portable), str(agent)],
                input=[
                    str(REPOSITORY / "product.json"),
                    str(REPOSITORY / "requirements" / "python-build.lock.json"),
                    str(REPOSITORY / "requirements" / "native-build.lock.json"),
                    str(REPOSITORY / "requirements" / "inno-setup.lock.json"),
                ],
            ))

    def test_false_signed_wording_mutant_fails(self) -> None:
        root = self.build_release("signed-mutant")
        path = root / "release-manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["signature"]["status"] = "signed"
        value["signature"]["verifiedSigner"] = "Unverified Publisher"
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "signature disclosure"):
            release_tools.command_verify(argparse.Namespace(release_root=root))

    def test_missing_checksum_coverage_mutant_fails(self) -> None:
        root = self.build_release("checksum-mutant")
        path = root / "SHA256SUMS.txt"
        lines = [line for line in path.read_text(encoding="ascii").splitlines() if "Setup.exe" not in line]
        path.write_text("\n".join(lines) + "\n", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "checksum coverage"):
            release_tools.command_verify(argparse.Namespace(release_root=root))

    def test_dependency_inventory_mutant_fails(self) -> None:
        root = self.build_release("dependency-inventory-mutant")
        path = root / "release-manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["dependencyInventory"] = value["dependencyInventory"][:-1]
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "release dependency inventory mismatch"):
            release_tools.command_verify(argparse.Namespace(release_root=root))


if __name__ == "__main__":
    unittest.main()
