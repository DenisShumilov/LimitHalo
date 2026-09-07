# Contributing

Thank you for helping with LimitHalo. The display name is temporary; the stable internal ID is `AILimitsWidget`. A name or license change after candidate freeze changes release identity and must not be folded into an unrelated patch.

## Ground rules

- Keep pull requests fixture-only. Never use live Claude/Codex credentials, provider requests, chats, transcripts, browser state, Startup, the installed widget, or provider/application logs in tests or issue attachments.
- Preserve explicit Claude consent and the equally operable Claude-off path.
- Preserve fail-closed Codex signer/root/protocol verification and the narrow broker boundary.
- Do not add telemetry, analytics, quota history, browser automation, WebView/Electron, a local server, inbound listeners, conversation parsing, clipboard access, admin services, or an automatic updater.
- Never hardcode a daily Codex limit. Derive duration labels from validated `windowDurationMins` values.
- Safe diagnostics are an allowlist, not a redaction-after-capture system.
- Keep `LimitHalo-Agent.ps1` closed to the exact four public actions and the exact reviewed unsigned hash acceptance. Never add a download, arbitrary path/executable/command, secret, provider consent, authentication, elevation, raw exception, or automatic retry route.

## Local validation

From the repository root:

```powershell
$state = 'C:\fixture\state'
$source = Join-Path $state 'source' # clean source export, not a live installation
$python = 'C:\fixture\python\Scripts\python.exe' # pre-provisioned Python 3.12.13 x64 environment with exact locked packages
pwsh -NoProfile -File $source\scripts\run_offline_tests.ps1 `
  -AuthorizedStateRoot $state `
  -TestOutputRoot (Join-Path $state 'offline-tests') `
  -PythonExe $python
pwsh -NoProfile -File $source\packaging\scripts\Test-ReleaseFragment.ps1 `
  -RepositoryRoot $source `
  -AuthorizedStateRoot $state `
  -TestOutputRoot (Join-Path $state 'release-fixtures') `
  -PythonExe $python
```

Both commands are static/parser/redirected-fixture routes. They must not install software or access a provider. Full packaging requires explicit fixture roots:

This is not a bootstrap installer: stage the exact pinned toolchain and wheelhouse first, and create the source manifest as described in [Development](docs/DEVELOPMENT.md#build-from-source). A missing compiler receipt must not be fabricated. Ordinary users should use a release bundle instead.

```powershell
pwsh -NoProfile -File $source\packaging\scripts\Build-Release.ps1 `
  -AppSourceRoot $source `
  -AuthorizedStateRoot $state `
  -SourceManifestPath (Join-Path $state 'source-manifest.json') `
  -SourceIdentity 'sha256:<verified source tree>' `
  -OutputRoot (Join-Path $state 'release-output') `
  -WheelhouseRoot (Join-Path $state 'python-wheels') `
  -PythonExe $python `
  -VsWherePath 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe' `
  -NativeToolchainRoot (Join-Path $state 'native-toolchain') `
  -InnoSetupCompiler (Join-Path $state 'inno\ISCC.exe') `
  -InnoProvenancePath (Join-Path $state 'inno\local-compiler-receipt.json')
```

The Python environment, native toolchain, and `local-compiler-receipt.json`/compiler pair must be the exact pinned inputs. The local route compiles without executing the new widget, broker, synthetic broker, or setup and records that native self-test limitation. Contributor validation may statically inspect the unsigned setup preview, but it must never launch that setup. Clean-machine install, upgrade, rollback, uninstall, and reboot behavior are separate, currently unverified owner gates.

## Fixture policy

Fixtures must be synthetic, contain no real usernames, absolute profile paths, tokens, account identifiers, precise activity timestamps, raw provider text, or logs, and be safe to publish. Add a negative fixture for every parser or security-boundary relaxation. A test that requires network access is not acceptable for pull-request validation.

## Pull requests

Describe the observable behavior, affected privacy/security invariant, fixture coverage, and commands run. Do not claim hosted Actions, signing, installation, provider compatibility, or publication unless evidence for that exact candidate exists. Initial community builds remain unsigned unless the release manifest records a verified signer.

The manual unsigned-release workflow runs only when its dispatch ref is protected (`github.ref_protected`). The input `source_ref` must be a full lowercase commit SHA; it does not itself satisfy branch protection. The workflow produces an inspection artifact and never publishes a release. See [Development](docs/DEVELOPMENT.md#hosted-workflow-is-a-separate-path).
