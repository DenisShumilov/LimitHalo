from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


REQUIRED_FILES = {
    "product.json",
    "README.md",
    "PRIVACY.md",
    "SECURITY.md",
    "INTEGRATIONS.md",
    "INSTALL_WITH_AI.md",
    "AGENTS.md",
    "LimitHalo-Agent.ps1",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "LICENSE",
    "THIRD-PARTY-NOTICES.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/workflows/validate.yml",
    ".github/workflows/release.yml",
    "packaging/README.md",
    "packaging/pyinstaller/AILimitsWidget.spec",
    "packaging/installer/LimitHalo.iss",
    "packaging/installer/Invoke-ManifestOwnedCleanup.ps1",
    "packaging/installer/Invoke-OwnedShortcut.ps1",
    "packaging/scripts/Acquire-HostedInno.ps1",
    "packaging/scripts/Build-Release.ps1",
    "packaging/scripts/Build-NativeBroker.ps1",
    "packaging/scripts/Assert-PythonBuildLock.ps1",
    "packaging/scripts/generate_version_info.py",
    "packaging/scripts/inno_provenance.py",
    "packaging/scripts/release_tools.py",
    "packaging/scripts/source_manifest.py",
    "packaging/scripts/Verify-Release.ps1",
    "packaging/scripts/Sign-Artifacts.ps1",
    "packaging/scripts/Invoke-OwnedCleanupFixture.ps1",
    "packaging/scripts/Test-OwnedCleanup.ps1",
    "packaging/scripts/Test-OwnedShortcuts.ps1",
    "packaging/scripts/Test-BuildLocks.ps1",
    "requirements/python-build.lock.json",
    "requirements/inno-setup.lock.json",
    "requirements/native-build.lock.json",
    "requirements/build-requirements.txt",
    "scripts/run_offline_tests.ps1",
    "tests/release/test_release_fragment.py",
    "tests/release/test_limit_halo_agent.py",
}

DOCS_WITH_INTEGRATION_DISCLOSURE = ("README.md", "PRIVACY.md", "SECURITY.md", "INTEGRATIONS.md")
DISCLOSURES = (
    "unsupported private Anthropic OAuth usage/refresh interface",
    "official Codex app-server",
    "not affiliated",
    "fail closed",
)

EXPECTED_PYTHON_LOCK = "7fc014ce7148891c323f6244a0c0a09c762dc38d712176f8b1c407beb932b7b0"
EXPECTED_INNO_LOCK = "dcc97f618b56a106b866b3228ba71597ea4df21b88400558c04ac5d7e62708ae"

PYTHON_EXECUTION_SURFACES = (
    "README.md",
    ".github/workflows/validate.yml",
    ".github/workflows/release.yml",
    "packaging/scripts/Acquire-HostedInno.ps1",
    "packaging/scripts/Build-Release.ps1",
    "packaging/scripts/Test-OwnedCleanup.ps1",
    "packaging/scripts/Test-ReleaseFragment.ps1",
    "packaging/scripts/Verify-Release.ps1",
    "scripts/run_offline_tests.ps1",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def validate_repository(root: Path) -> list[str]:
    root = root.resolve(strict=True)
    errors: list[str] = []
    for relative in sorted(REQUIRED_FILES):
        if not (root / relative).is_file():
            errors.append(f"missing required file: {relative}")

    if errors:
        return errors

    try:
        product = json.loads(read(root / "product.json"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"invalid product.json: {error}")
        product = {}
    expected_identity = {
        "displayName": "LimitHalo",
        "displayNameStatus": "temporary-working-name",
        "internalProductId": "AILimitsWidget",
        "architecture": "x64",
        "installerAppId": "{{A858F5C8-BBD1-4B0F-B5BD-FB812A5EEA73}}",
        "releaseChannel": "unsigned-community-candidate",
        "installerArchitecture": "signing-ready-explicitly-unsigned-preview",
        "aiAgentEntryPoint": "LimitHalo-Agent.ps1",
        "aiAgentProtocol": "limit-halo-agent/1",
    }
    for key, expected in expected_identity.items():
        if product.get(key) != expected:
            errors.append(f"product identity mismatch: {key}")
    version = product.get("version", "")
    if not isinstance(version, str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        errors.append("product version must be numeric SemVer")
    init_text = read(root / "src/limit_halo/__init__.py")
    if f'__version__ = "{version}"' not in init_text or f'RELEASE_TAG = "v{version}-beta.1"' not in init_text:
        errors.append("runtime release identity differs from product.json")

    for relative in DOCS_WITH_INTEGRATION_DISCLOSURE:
        text = read(root / relative)
        folded = text.casefold()
        for phrase in DISCLOSURES:
            if phrase.casefold() not in folded:
                errors.append(f"{relative} missing disclosure: {phrase}")

    readme = read(root / "README.md")
    for phrase in (
        "candidate is **unsigned**",
        "Installation is never unattended",
        "not a clean-machine install or real reboot test",
        "not a code-signing system or a guarantee of full automatic rollback",
    ):
        if phrase.casefold() not in readme.casefold():
            errors.append(f"README.md missing public limitation: {phrase}")
    development = read(root / "docs/DEVELOPMENT.md")
    for phrase in ("display name is provisional", "An ordinary click does not open a details window"):
        if phrase.casefold() not in development.casefold():
            errors.append(f"DEVELOPMENT.md missing product boundary: {phrase}")

    ai_docs = read(root / "INSTALL_WITH_AI.md")
    agent_instructions = read(root / "AGENTS.md")
    agent = read(root / "LimitHalo-Agent.ps1")
    for phrase in (
        "LimitHalo-Agent.ps1",
        "needs no Python",
        "UNSIGNED_UNTRUSTED",
        "never reads LimitHalo settings, provider credentials",
    ):
        if phrase.casefold() not in ai_docs.casefold():
            errors.append(f"INSTALL_WITH_AI.md missing AI-agent boundary: {phrase}")
    for phrase in ("LimitHalo-Agent.ps1", "Status", "Auto", "Never invent Setup switches"):
        if phrase.casefold() not in agent_instructions.casefold():
            errors.append(f"AGENTS.md missing exact AI-agent instruction: {phrase}")
    for phrase in (
        "@('Auto','Status','Install','Repair')",
        "UNSIGNED_UNTRUSTED",
        "EXACT_ACCEPTANCE_REQUIRED",
        "settingsAccess='NOT_READ'",
        "authenticationPerformed=$false",
        "if($arts.Count-ne3)",
        "if($sums.Count-ne4)",
    ):
        if phrase not in agent:
            errors.append(f"LimitHalo-Agent.ps1 missing closed protocol control: {phrase}")

    privacy = read(root / "PRIVACY.md")
    for phrase in ("no telemetry", "quota values", "in memory", "Remove local settings"):
        if phrase.casefold() not in privacy.casefold():
            errors.append(f"PRIVACY.md missing boundary: {phrase}")
    portable_disclosure = "%LOCALAPPDATA%\\AILimitsWidget\\config.json"
    for relative in ("README.md", "PRIVACY.md", "packaging/README.md"):
        text = read(root / relative)
        if (
            portable_disclosure.casefold() not in text.casefold()
            or "never" not in text.casefold()
            or "beside the executable" not in text.casefold()
        ):
            errors.append(f"{relative} missing portable LocalAppData boundary")

    license_text = read(root / "LICENSE")
    if not license_text.startswith("MIT License\n\nCopyright (c) 2026 LimitHalo contributors\n"):
        errors.append("MIT license draft identity changed")

    if sha256(root / "requirements/python-build.lock.json") != EXPECTED_PYTHON_LOCK:
        errors.append("Python build lock hash mismatch")
    if sha256(root / "requirements/inno-setup.lock.json") != EXPECTED_INNO_LOCK:
        errors.append("Inno Setup lock hash mismatch")

    for workflow_name in ("validate.yml", "release.yml"):
        workflow = read(root / ".github/workflows" / workflow_name)
        for line_number, line in enumerate(workflow.splitlines(), 1):
            match = re.match(r"\s*uses:\s*([^\s#]+)", line)
            if match:
                reference = match.group(1)
                if not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference):
                    errors.append(f"{workflow_name}:{line_number} action is not pinned to a full SHA")
        if not re.search(r"(?m)^permissions:\s*\n\s+contents:\s*read\s*$", workflow):
            errors.append(f"{workflow_name} does not set repository contents read-only")
        if "runs-on: windows-2025" not in workflow:
            errors.append(f"{workflow_name} does not use windows-2025")
        if re.search(r"(?im)^\s*contents:\s*write\s*$", workflow):
            errors.append(f"{workflow_name} elevates contents permission")
        if "id: setup-python" not in workflow or "steps.setup-python.outputs.python-path" not in workflow:
            errors.append(f"{workflow_name} does not bind the setup-python interpreter output")
        if re.search(r"(?im)(?:^|[& (])python(?:\.exe)?\s+-|Get-Command\s+python", workflow):
            errors.append(f"{workflow_name} invokes a PATH-selected Python interpreter")

    release_workflow = read(root / ".github/workflows/release.yml")
    if "workflow_dispatch:" not in release_workflow:
        errors.append("release workflow is not manual")
    forbidden_publish = (
        "gh release create",
        "gh release upload",
        "softprops/action-gh-release",
        "actions/create-release",
        "ncipollo/release-action",
        "draft: false",
    )
    for phrase in forbidden_publish:
        if phrase.casefold() in release_workflow.casefold():
            errors.append(f"release workflow contains publication route: {phrase}")

    workflow_requirements = (
        "git archive",
        "source_manifest.py",
        " create --root ",
        " verify --root ",
        "LIMIT_HALO_SOURCE_IDENTITY=sha256:$tree",
        "Acquire-HostedInno.ps1",
        "Validate and build without acquisition",
        "-AppSourceRoot $env:LIMIT_HALO_SOURCE_ROOT",
        "-AuthorizedStateRoot $env:LIMIT_HALO_STATE_ROOT",
        "-SourceManifestPath $env:LIMIT_HALO_SOURCE_MANIFEST",
        "-SourceIdentity $env:LIMIT_HALO_SOURCE_IDENTITY",
        "-WheelhouseRoot $env:LIMIT_HALO_WHEELHOUSE",
        "-InnoSetupCompiler $env:LIMIT_HALO_ISCC",
        "-InnoProvenancePath $env:LIMIT_HALO_INNO_RECEIPT",
        "source changed during build",
    )
    for phrase in workflow_requirements:
        if phrase not in release_workflow:
            errors.append(f"release workflow missing clean build control: {phrase}")
    if "-SourceIdentity $env:SOURCE_REF" in release_workflow or "-AppSourceRoot $env:GITHUB_WORKSPACE" in release_workflow:
        errors.append("release workflow uses raw checkout identity instead of the clean source tree")
    offline_marker = "- name: Validate and build without acquisition"
    upload_marker = "- name: Upload the unsigned candidate for inspection"
    if offline_marker in release_workflow and upload_marker in release_workflow:
        offline_phase = release_workflow.split(offline_marker, 1)[1].split(upload_marker, 1)[0]
        for forbidden in ("Invoke-WebRequest", "pip download", "Acquire-HostedInno.ps1"):
            if forbidden.casefold() in offline_phase.casefold():
                errors.append(f"offline build phase contains acquisition route: {forbidden}")

    validate_workflow = read(root / ".github/workflows/validate.yml")
    for phrase in (
        "scripts/run_offline_tests.ps1",
        "-AuthorizedStateRoot $env:LIMIT_HALO_STATE_ROOT",
        "-TestOutputRoot $offlineRoot",
        "packaging/scripts/Test-ReleaseFragment.ps1",
        "-TestOutputRoot $fixtureRoot",
    ):
        if phrase not in validate_workflow:
            errors.append(f"validate workflow missing redirected control: {phrase}")

    referenced_scripts = set(
        match.replace("\\", "/")
        for workflow in (read(root / ".github/workflows/validate.yml"), release_workflow)
        for match in re.findall(r"(?:packaging|tests|requirements|scripts)[/\\][A-Za-z0-9_.*/\\-]+", workflow)
        if "*" not in match and not match.endswith("/")
    )
    for relative in referenced_scripts:
        path = root / relative
        if path.suffix.lower() in {".ps1", ".py", ".json", ".txt"} and not path.exists():
            errors.append(f"workflow references missing path: {relative}")

    inno = read(root / "packaging/installer/LimitHalo.iss")
    required_inno = (
        "PrivilegesRequired=lowest",
        "DefaultDirName={localappdata}\\Programs\\{#InternalProductId}",
        "AppId={#AppIdValue}",
        "versions\\{#CandidateId}\\{#ProductExe}",
        "Remove local LimitHalo settings?",
        "Invoke-ManifestOwnedCleanup.ps1",
        "RunManifestTool",
        "active-candidate.txt",
        "rollback-candidate.txt",
        "SignedUninstaller=no",
        "Name: \"ukrainian\"",
        "CreateInputOptionPage",
        "ClaudeAccessPage.Values[0] := False;",
        "function ClaudeConsentValue(): string;",
        "'  \"claudeConsent\": \"' + ClaudeConsentValue() + '\",'",
        "RunCandidateHealthCheck",
        "--health-check --offline --no-provider-start",
        "DeleteCandidate {#CandidateId}",
        "Configure {#ProductName}",
        "PreviousStartupPresent",
        "Invoke-OwnedShortcut.ps1",
        "RunOwnedShortcut",
        "WorkingDir: \"{app}\\versions\\{#CandidateId}\"",
        "ActualStartup",
    )
    for phrase in required_inno:
        if phrase not in inno:
            errors.append(f"installer missing invariant: {phrase}")
    for line_number, line in enumerate(inno.splitlines(), 1):
        if line.startswith("Filename: ") and "postinstall" in line:
            if (
                'Filename: "{app}\\versions\\{#CandidateId}\\{#ProductExe}"' not in line
                or 'WorkingDir: "{app}\\versions\\{#CandidateId}"' not in line
            ):
                errors.append(f"installer postinstall launch escapes active manifest-owned version at line {line_number}")
    in_files_section = False
    for line_number, line in enumerate(inno.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_files_section = stripped.casefold() == "[files]"
            continue
        if not in_files_section or not stripped.casefold().startswith("source:"):
            continue
        flags_match = re.search(r"(?i)(?:^|;)\s*Flags:\s*([^;]+)", stripped)
        flags = set(flags_match.group(1).casefold().split()) if flags_match else set()
        if "external" not in flags and "notimestamp" not in flags:
            errors.append(f"embedded installer source missing notimestamp at line {line_number}")
    for forbidden_shortcut_route in ("CreateShellLink(", "DeleteOwnedShortcut("):
        if forbidden_shortcut_route in inno:
            errors.append(f"installer bypasses ownership-aware shortcut helper: {forbidden_shortcut_route}")
    for phrase in ("{pf}", "{commonappdata}", "PrivilegesRequired=admin", "runascurrentuser"):
        if phrase.casefold() in inno.casefold():
            errors.append(f"installer contains forbidden machine/live route: {phrase}")
    for phrase in ("AppPublisherURL=", "AppSupportURL=", "AppUpdatesURL=", "github.com/OWNER"):
        if phrase.casefold() in inno.casefold():
            errors.append(f"installer contains unverifiable URL metadata: {phrase}")
    public_text = "\n".join(
        read(root / name)
        for name in (
            "README.md", "PRIVACY.md", "SECURITY.md", "INTEGRATIONS.md",
            "CONTRIBUTING.md", "CHANGELOG.md", "product.json",
        )
    )
    if re.search(r"(?i)github\.com/OWNER|<OWNER>|OWNER/ai-limits-widget", public_text):
        errors.append("public metadata contains an OWNER placeholder")

    policy_path = (root / "packaging" / "scripts" / "release_policy.py").resolve()
    package_text = "\n".join(
        read(path)
        for path in sorted((root / "packaging").rglob("*"))
        if path.is_file() and path.resolve() != policy_path and path.suffix.lower() in {".ps1", ".py", ".iss", ".spec", ".md"}
    )
    if re.search(r"(?i)C:\\Users\\shumi|AIUsageWidgetBrowserless|AI Limits\.lnk", package_text):
        errors.append("packaging fragment contains a private or legacy live path")
    for forbidden in ("signatureStatus = 'signed'", '"status": "signed"', "publicationPerformed = $true"):
        if forbidden in package_text:
            errors.append(f"packaging fragment contains a false release claim: {forbidden}")

    build_script = read(root / "packaging/scripts/Build-Release.ps1")
    for phrase in (
        "Build-NativeBroker.ps1",
        "SourceRoot = $brokerSource",
        "ClaudeUsageBroker.exe",
        "payload-manifest",
        "release-manifest",
        "checksums",
        "normalize-stdlib-zip",
        "[Parameter(Mandatory = $true)][string]$SourceIdentity",
        "[Parameter(Mandatory = $true)][string]$InnoProvenancePath",
        "inno_provenance.py",
        "Source changed during the build",
        "if ($item -is [System.IO.DirectoryInfo])",
        "elseif ($item -is [System.IO.FileInfo])",
        "$item = $item.Directory",
        'throw "$Label contains an unsupported filesystem object"',
        "installerExecutionPerformed = $false",
        "Invoke-OwnedShortcut.ps1",
        "LimitHalo-Agent.ps1",
        "agentSha256",
    ):
        if phrase not in build_script:
            errors.append(f"build script missing source-build/release closure: {phrase}")

    provenance = read(root / "packaging/scripts/inno_provenance.py")
    hosted_acquisition = read(root / "packaging/scripts/Acquire-HostedInno.ps1")
    for phrase in (
        '"networkEnabled": False',
        '"hostInstallPerformed": False',
        '"networkEnabled": True',
        '"hostInstallPerformed": True',
        '"disposableRunner": True',
        '"RUNNER_ENVIRONMENT": "github-hosted"',
        '"ImageOS": "win25"',
    ):
        if phrase not in provenance:
            errors.append(f"provenance validator missing truthful branch control: {phrase}")
    for phrase in (
        "$env:GITHUB_ACTIONS -cne 'true'",
        "$env:RUNNER_ENVIRONMENT -cne 'github-hosted'",
        "$env:RUNNER_OS -cne 'Windows'",
        "$env:ImageOS -cne 'win25'",
        "Invoke-WebRequest",
        "Get-AuthenticodeSignature",
        "hostInstallPerformed = $true",
        "disposableRunner = $true",
    ):
        if phrase not in hosted_acquisition:
            errors.append(f"hosted acquisition missing exact environment/provenance control: {phrase}")

    compile_only_surfaces = {
        "Build-Release.ps1": build_script,
        "Test-ReleaseFragment.ps1": read(root / "packaging/scripts/Test-ReleaseFragment.ps1"),
        "validate.yml": validate_workflow,
        "release.yml": release_workflow,
    }
    for label, text in compile_only_surfaces.items():
        for forbidden in ("Invoke-Setup", "/TASKS=", "Start-Process -FilePath $setup"):
            if forbidden.casefold() in text.casefold():
                errors.append(f"{label} contains an unsigned installer execution route: {forbidden}")

    source_manifest = read(root / "packaging/scripts/source_manifest.py")
    if "0d01b424176f389cc9bb1e602dc0574ed1f2e16a5d6c0da62cd4fd2473e66962" not in source_manifest:
        errors.append("source manifest tool is not bound to the successor contract")
    test_fragment = read(root / "packaging/scripts/Test-ReleaseFragment.ps1")
    offline_tests = read(root / "scripts/run_offline_tests.ps1")
    for label, text in (("Test-ReleaseFragment.ps1", test_fragment), ("run_offline_tests.ps1", offline_tests)):
        for parameter in ("AuthorizedStateRoot", "TestOutputRoot", "PythonExe"):
            pattern = rf"\[Parameter\(Mandatory = \$true\)\]\[string\]\${parameter}"
            if not re.search(pattern, text):
                errors.append(f"{label} does not require {parameter}")
        if "strict descendant of AuthorizedStateRoot" not in text:
            errors.append(f"{label} does not confine its output beneath AuthorizedStateRoot")

    direct_python = re.compile(
        r"(?im)&\s*\$(?:python|pythonRuntime|buildPython|PythonExe|bootstrapPython)\s+(?!-I\s+-B(?:\s|$))"
    )
    checked_python = re.compile(
        r"(?is)Invoke-Checked\s+\$(?:pythonRuntime|buildPython|PythonExe)\s+@\((?!\s*['\"]-I['\"]\s*,\s*['\"]-B['\"])"
    )
    for relative in PYTHON_EXECUTION_SURFACES:
        text = read(root / relative)
        dynamic_runner = relative == "scripts/run_offline_tests.ps1"
        if (not dynamic_runner and direct_python.search(text)) or checked_python.search(text):
            errors.append(f"{relative} contains a Python invocation missing exact -I -B")
        for line in text.splitlines():
            without_exact = line.replace("-I -B", "").replace("'-I', '-B'", "").replace('"-I", "-B"', "")
            if re.search(r"(?<![A-Za-z0-9_])-B(?=(?:['\"]|\s|,|\)|$))", without_exact):
                errors.append(f"{relative} contains a Python invocation missing exact -I -B")
                break

    build_release = read(root / "packaging/scripts/Build-Release.ps1")
    if not re.search(r"\$manifestArguments\s*=\s*@\(\s*'-I'\s*,\s*'-B'", build_release, re.DOTALL):
        errors.append("Build-Release.ps1 manifest Python invocation is missing exact -I -B")
    for forbidden in ("$env:PYTHONPATH", "isolated build environment creation", "Join-Path $buildRoot 'venv'"):
        if forbidden.casefold() in build_release.casefold():
            errors.append(f"Build-Release.ps1 uses an unlisted nested Python environment: {forbidden}")
    for required in ("ExpectedPythonSha256", "Staged Python package closure mismatch", "NativeToolchainRoot"):
        if required not in build_release:
            errors.append(f"Build-Release.ps1 lacks local staged-build control: {required}")

    native_build = read(root / "packaging/scripts/Build-NativeBroker.ps1")
    for required in (
        "NativeToolchainRoot", "RunSyntheticSelfTest", "NOT_EXECUTED_LOCAL_POLICY",
        "staged-explicit", "manifestToolSha256", "resourceCompilerExecuted",
    ):
        if required not in native_build:
            errors.append(f"Build-NativeBroker.ps1 lacks explicit local compile-only control: {required}")
    if "if ($RunSyntheticSelfTest) {" not in native_build:
        errors.append("Build-NativeBroker.ps1 does not guard synthetic execution behind explicit opt-in")
    if '"mt.exe"' not in read(root / "requirements/native-build.lock.json"):
        errors.append("native build lock does not bind the staged manifest tool")

    verifier = read(root / "packaging/scripts/Verify-Release.ps1")
    if "IsPathFullyQualified" in verifier or "IsPathRooted" not in verifier:
        errors.append("Verify-Release.ps1 is not Windows PowerShell 5.1 path-check compatible")

    bug_template = read(root / ".github/ISSUE_TEMPLATE/bug_report.yml").casefold()
    for forbidden_data in ("credentials", "raw provider", "chats", "browser data", "logs"):
        if forbidden_data not in bug_template:
            errors.append(f"bug template lacks sensitive-data warning: {forbidden_data}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        errors = validate_repository(args.repository_root)
    except OSError as error:
        errors = [str(error)]
    result = {"schemaVersion": "1.0.0", "status": "PASS" if not errors else "FAIL", "errors": errors}
    if args.json:
        print(json.dumps(result, indent=2))
    elif errors:
        for error in errors:
            print(error, file=sys.stderr)
    else:
        print("release fragment policy: PASS")
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
