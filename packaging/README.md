# Packaging contract

Every writable build and test root is a mandatory absolute path strictly beneath one caller-supplied `AuthorizedStateRoot`. Use a clean source export beneath that root. Never point these tools at the installed widget, a real profile, Startup, browser/chat/log state, or provider credentials.

`Build-Release.ps1` requires source containing the Python entry point, native Claude broker sources, assets, and fixture-only tests. It verifies a canonical source manifest, requires `SourceIdentity` to equal `sha256:<verified tree>`, validates an already provisioned Python 3.12.13 x64 environment and the exact offline wheel closure, rebuilds the native broker through either an explicit staged native toolchain or the separately hosted toolchain route, runs fixture tests, builds the PyInstaller onedir payload, and verifies the source again after the build. Every build Python invocation is exactly `-I -B`; the script never creates or executes a nested interpreter.

It emits only unsigned candidate artifacts:

- `LimitHalo-1.0.1-Setup.exe`
- `LimitHalo-1.0.1-Windows-x64.zip`
- `LimitHalo-Agent.ps1`

The release directory also contains `release-manifest.json` and `SHA256SUMS.txt`. The manifest binds exactly the three artifacts above. The checksum file has exactly four records: those three artifacts plus `release-manifest.json`. The standalone helper, installed payload member, and portable ZIP member are byte-identical.

The signing-ready setup keeps advanced provider, appearance, and alert choices out of the normal path and retains one separate unchecked quota-only Claude consent choice. Fresh settings initialize only `%LOCALAPPDATA%\AILimitsWidget\config.json`, with alerts off and monochrome colors. Claude consent is `granted` only when the person selects that separate choice; the AI helper cannot preselect it, so its default is `not-asked`. Existing preferences remain byte-for-byte untouched by setup. A staged candidate must pass manifest validation and an offline `--health-check --offline --no-provider-start` before active/rollback markers or shortcuts change.

`LimitHalo-Agent.ps1` is the only supported AI-facing entry point. It needs no Python and exposes exactly `Auto`, `Status`, `Install`, and `Repair`. `Status` is read-only. A live action requires the human to accept the exact candidate ID, Setup SHA-256, and helper SHA-256 for each invocation. The current community bundle is always reported as `UNSIGNED_UNTRUSTED`; same-bundle hashes prove consistency, not publisher identity. The helper never reads settings or credentials, grants Claude consent, performs provider authentication, downloads another artifact, or retries a live mutation automatically.

## First-open ownership

Each postinstall route has exactly one launch owner. Interactive and explicitly opted-in raw silent Setup use the mutually exclusive HUD/Configure `[Run]` entries. The AI helper disables those entries, verifies the installed candidate, and then performs the route's single launch itself.

| Setup route | Postinstall result |
| --- | --- |
| Interactive, finish action accepted | Exactly one HUD-or-Configure start |
| Interactive, finish action unchecked | No start |
| Silent, `/ACCEPTDEFAULTS=1 /OPENAFTERINSTALL=1` | Exactly one HUD-or-Settings start |
| Silent, accepted invocation but flag absent, `0`, or malformed | No start |
| Silent without `/ACCEPTDEFAULTS=1` | Abort before install and before start |
| AI helper (`/OPENAFTERINSTALL=0`) | Setup does not start; helper verifies, then starts exactly once |

The AI helper supplies the exact `/OPENAFTERINSTALL=0` literal when it invokes the transactional installer, validates the result, and owns the one subsequent launch. For Setup-owned HUD launches, immediately before the HUD `[Run]` entry Setup marks only that child process for activation. The app reveals the HUD at its saved position, clamps it only if needed to fit the current monitor work area, and briefly brings it forward. Activation never replaces the user's chosen position with the cursor location. The Configure branch does not receive that mark. When a normal no-argument app launch has no usable provider, it opens the existing provider-free Configure window in the same process instead of exiting invisibly.

The portable ZIP has fixed member timestamps and sorted paths. Portable mode changes packaging and shortcut behavior only: settings remain at `%LOCALAPPDATA%\AILimitsWidget\config.json`, never beside the executable.

## Source and state binding

Create the source manifest outside the governed source tree, then pass the printed tree with the `sha256:` prefix. `AppSourceRoot`, the packaging scripts themselves, `WheelhouseRoot`, `NativeToolchainRoot`, `InnoSetupCompiler`, `InnoProvenancePath`, `SourceManifestPath`, and `OutputRoot` must all stay beneath `AuthorizedStateRoot`. The exact Python interpreter and `vswhere` path are explicit read-only tool inputs. With `NativeToolchainRoot`, `vswhere` is not executed; compiler, linker, manifest tool, include directories, and libraries resolve only from that staged root.

The default local staged route is compile/static-only and records `selfTestExecuted: false`. Explicit `-RunNativeSelfTest` runs the deterministic synthetic broker with fixture data and records that execution; it never launches the production broker, widget or installer. Local native builds omit the optional version resource so no unlisted resource-conversion child can run. The manual hosted workflow also opts into the synthetic self-test on its disposable runner.

`OutputRoot` is ownership marked. A later clean rebuild is allowed only when the exact marker is present, and cleanup is limited to direct children of that marked root. Reparse components and path aliases fail closed.

## Inno Setup provenance

Local builds accept only the exact Inno Setup 7.1.0 compiler and its existing `local-compiler-receipt.json`. The repository does not synthesize this receipt. It must report the pinned signed installer, compiler SHA-256, `networkEnabled=false`, `clipboardEnabled=false`, and `hostInstallPerformed=false`, and it must sit beside the exact compiler.

The manual GitHub workflow has a separate truthful acquisition branch. It runs only when `GITHUB_ACTIONS=true`, `RUNNER_ENVIRONMENT=github-hosted`, `RUNNER_OS=Windows`, and `ImageOS=win25`. That branch downloads the one locked installer, verifies its size, SHA-256, and Authenticode leaf subject, installs it only into the disposable runner state root, verifies the compiler hash, and writes a run-bound receipt with `networkEnabled=true`, `hostInstallPerformed=true`, and `disposableRunner=true`. The following build step has no acquisition commands and revalidates the receipt.

The workflow build job also requires `github.ref_protected`. Dispatch it from a protected branch/ref after configuring the appropriate protection or ruleset. Supplying a valid commit in `source_ref` does not bypass this guard; an unprotected dispatch ref skips the job. Hosted success remains unobserved for this publication tree.

The local route neither acquires nor installs Inno Setup. Both routes compile one explicitly unsigned setup preview, verify its manifest/checksum structure, and never launch the product installer. Fresh install, upgrade, failed-validation rollback, uninstall, settings choices, and reboot persistence on a clean machine remain unverified.

## Installer and release limits

The installer is per-user and x64-only. It owns one AppId, one current-user Apps & Features entry, a Start Menu shortcut, an optional Startup shortcut, and manifest-owned version directories. Cleanup retains the active version and at most one rollback version, rejects reparse points and invalid candidate names, and preserves unknown paths. Settings are removed only after explicit choice.

The runtime update feed is pinned to `DenisShumilov/LimitHalo`. Background checking does not authorize downloading or installing code. There is no unattended installation, default signing action, signature claim, build-time publication, installer-execution action, or live-provider test. The legacy `automaticUpdater=false` manifest field denotes no unattended installer; `publicationPerformed=false` records that the build itself did not publish. These fields are not a live GitHub status report. Hosted Actions remain unobserved until an external run for the exact candidate is reviewed. See [Updates](../docs/UPDATES.md).
