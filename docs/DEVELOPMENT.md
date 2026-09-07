# Development and release details

This page keeps the technical behavior and build boundaries out of the short [README](../README.md). Read [Contributing](../CONTRIBUTING.md) before changing code and [Packaging](../packaging/README.md) before building a distribution.

## Product behavior

LimitHalo is a transparent Windows HUD for subscription quota windows reported by the official Codex app-server and, after explicit permission, Claude. The wide view can show up to two distinct Codex windows plus Claude 5-hour, 7-day, and Fable windows. The compact view uses the long-window layout. Labels come from integration-supplied durations; never hardcode a daily Codex limit.

The HUD is the daily-use surface. An ordinary click does not open a details window. The right-click menu includes Refresh, Check updates, Status colors, Start with Windows, Always on top, Settings, and Exit. Updates use a background worker and a UI-thread result queue: checks are quiet, downloads need a click, and installation needs exact unsigned-candidate confirmation. Language, provider visibility, optional alerts, safe AI repair, and privacy details are inside Settings.

The invisible icon-and-text contour is the drag target. A refresh reuses the existing windows and cached icons. A chosen position is stored as the user's preference; temporary adjustments to fit an available monitor area do not replace that preference. Settings changes use the current position even if Settings was opened earlier. Actual reboot/multi-monitor behavior still requires real-machine acceptance for the exact release.

Compatible settings from a newer build are preserved. Invalid or unreadable settings are left untouched, and the widget may use a temporary session position. The owner must restore access to a valid settings file or restore its valid backup, then restart. Agent Repair repairs program files; it does not restore rejected settings.

## Setup and first launch

Setup defaults to English; Ukrainian is available in its standard language selector. Existing saved language remains unchanged. Fresh defaults use automatic provider detection, the original monochrome compact appearance, alerts/quiet hours off, and installed autostart on. Portable mode never creates autostart. Upgrades preserve existing settings and the person's previous autostart choice.

Normal interactive setup has one product-specific choice: an unchecked, fully disclosed permission for quota-only Claude access. Only the person's affirmative choice may record `granted` and enable collection. Otherwise consent remains `not-asked`, and the in-app Connect action offers the human-only consent route. AI and silent installation cannot grant permission or complete provider login. Compatible Claude Code OAuth credentials are needed in `%USERPROFILE%\.claude\.credentials.json`; a signed-in Claude Desktop app alone does not establish that prerequisite.

If no supported client exists at Windows startup, LimitHalo exits quietly. An ordinary launch without a usable provider opens the provider-free Settings route. With consent and the required compatible authorization, collection starts before the first HUD frame; this does not guarantee that a network response will arrive immediately. Missing authorization or a failed interface leaves an explicit recovery status. Recovery may open only a trusted official client and then recheck automatically; the person owns the login. Turning Claude off prevents future broker launches without reading or editing credentials.

Interactive Setup owns one optional first launch via its checked finish action. The result is the HUD when a selected provider can be shown, or Settings otherwise. Raw silent Setup may request a one-time launch only with the exact `/OPENAFTERINSTALL=1` opt-in and accepted defaults. The AI helper uses `/OPENAFTERINSTALL=0`, verifies the installed candidate, and then launches it exactly once itself. These are separate launch-owner routes, not multiple simultaneous starts.

The per-user installer owns Start Menu and optional Startup shortcuts. Portable mode changes packaging and shortcut behavior, not the settings location: both use `%LOCALAPPDATA%\AILimitsWidget\config.json`, never a file beside the executable.

## Integration and privacy boundaries

The Codex discovery path reads public metadata for official npm and `OpenAI.Codex` AppX installations. It does not execute npm, package scripts, PATH shims, or lifecycle hooks. Candidates must pass canonical-root, signer, trust, and allowlisted protocol checks. Invalid or incompatible candidates are unavailable. See [Integration boundaries](../INTEGRATIONS.md) for the complete accepted protocol.

Claude uses an unsupported private Anthropic OAuth usage/refresh interface. A short-lived native broker reads only the `claudeAiOauth` projection from `%USERPROFILE%\.claude\.credentials.json` and, during provider-authorized refresh, may atomically replace only that projection under `.storage-write.lock`. Tokens and raw responses do not reach the GUI. No access is allowed before explicit consent.

No browser profiles, chats, transcripts, provider logs, clipboard contents, telemetry, analytics, or quota history are read or stored by the widget. Quota values remain in memory. Settings contain only product preferences and the explicit Claude consent enum. Do not broaden access to recover from integration drift. See [Privacy](../PRIVACY.md) and [Security](../SECURITY.md).

## Build from source

This is a controlled maintainer build, **not a one-command bootstrap**. Ordinary users should use a published release bundle. Local builds need a pre-provisioned Python 3.12.13 x64 environment with the exact locked packages, an offline wheelhouse, a pinned native toolchain, and the exact Inno Setup compiler with an existing provenance receipt. The scripts do not download or install those prerequisites.

All writable source, fixture, manifest, and build paths must be strictly beneath one authorized state root. Export a clean source tree there first; do not point these scripts at a working installation or private profile data. The following paths are illustrative and must already exist where required:

```powershell
$state = 'C:\fixture\state'
$source = Join-Path $state 'source'
$manifest = Join-Path $state 'source-manifest.json'
$contract = '0d01b424176f389cc9bb1e602dc0574ed1f2e16a5d6c0da62cd4fd2473e66962'
$python = 'C:\fixture\python\Scripts\python.exe'
& $python -I -B $source\packaging\scripts\source_manifest.py create `
  --root $source --manifest $manifest --contract-sha256 $contract

pwsh -NoProfile -File $source\packaging\scripts\Test-ReleaseFragment.ps1 `
  -RepositoryRoot $source `
  -AuthorizedStateRoot $state `
  -TestOutputRoot (Join-Path $state 'release-fixtures') `
  -PythonExe $python

pwsh -NoProfile -File $source\packaging\scripts\Build-Release.ps1 `
  -AppSourceRoot $source `
  -AuthorizedStateRoot $state `
  -SourceManifestPath $manifest `
  -SourceIdentity 'sha256:<tree printed by source_manifest.py>' `
  -OutputRoot (Join-Path $state 'release-output') `
  -WheelhouseRoot (Join-Path $state 'python-wheels') `
  -PythonExe $python `
  -VsWherePath 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe' `
  -NativeToolchainRoot (Join-Path $state 'native-toolchain') `
  -InnoSetupCompiler (Join-Path $state 'inno\ISCC.exe') `
  -InnoProvenancePath (Join-Path $state 'inno\local-compiler-receipt.json')
```

The manifest must be outside the governed source tree. Substitute the actual verified tree identity; the placeholder is not a valid build identity. The `local-compiler-receipt.json`/compiler pair must be staged and pinned; do not invent a receipt to satisfy validation.

Local builds never launch the production widget, production broker, or Setup. Optional `-RunNativeSelfTest` runs only the deterministic synthetic broker test and records whether it ran. Validation compiles and statically verifies an explicitly unsigned setup preview but never launches that setup. The scripts reject writable roots outside the authorized state root and known live product/profile locations.

Fresh installation, upgrade, failed-validation rollback, uninstall, settings choices, real reboot persistence, and live providers remain separate acceptance checks. A successful build is not evidence that those checks passed.

## Hosted workflow is a separate path

The automatic `.github/workflows/validate.yml` workflow uses **Python 3.12.10 x64** for fixture validation on Windows 2025. This is a validation-only interpreter pin: the Actions Python catalog has a Windows binary for 3.12.10, but not 3.12.13. It does not change the production build/runtime identity, which remains locked to 3.12.13. Validation still checks the exact eight-wheel build lock, rejects a deliberately changed wheel, installs offline with required hashes, and runs the full application and release-fixture suites. The CI regression tests keep these boundaries explicit.

The validation export explicitly disables Git's `core.autocrlf` conversion for `archive`. This preserves committed bytes and the frozen lock-file hashes even when a Windows runner defaults to CRLF checkout conversion; it does not rewrite or relax a lock.

The manual workflow `.github/workflows/release.yml` requires a full lowercase commit SHA as `source_ref`. Its build job is guarded by `github.ref_protected`: dispatch it from a protected branch/ref. An unprotected dispatch ref skips the job, even if the `source_ref` input names an otherwise valid commit. Configure the appropriate branch protection or ruleset before using this path; do not remove the guard to make a launch screenshot look green.

The manual hosted build path has a distinct acquisition step on a disposable Windows runner, unlike the local pre-staged route. It produces an unsigned candidate artifact with read-only repository permissions. It does **not** create or publish a GitHub Release. A successful automatic Validate run is not a hosted release build. The manual build still requires its exact 3.12.13 Windows toolchain to become available; its execution has not been established. Checked-in workflow syntax does not guarantee a successful remote build.

## Release identity

The current display name is provisional; the stable internal ID is `AILimitsWidget`. The first updater-enabled beta uses numeric version `1.0.1` and release tag `v1.0.1-beta.1`. Keep product.json, the runtime version and release tag consistent; the build derives installer names/version from product.json. Initial community artifacts remain unsigned. Online checking is implemented; unattended installation is not.

Each release must be rebuilt from its exact source export; never reuse the old v9 binaries or manifest as proof of this updater-enabled source. See [Publishing](PUBLISHING.md). A frozen provider-free `--check-update` probe returns 0 when current, 10 when a newer release exists, or 20 when validation/network access fails. `--check-update --from-version v1.0.0-beta.1` checks ordering against an older version without starting providers, reading settings, downloading binaries, or installing anything.
