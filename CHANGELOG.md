# Changelog

All notable changes to this project will be documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.1-beta.1] - 2026-09-07

Release candidate notes; publication is a separate step. The application version is `1.0.1`.

### Added

- Background GitHub release checks about 15 seconds after launch and every six hours, with immediate manual checking from the right-click menu.
- A nonintrusive menu state for available versions, user-triggered complete-bundle download, GitHub API asset SHA-256 and internal-bundle verification, and separate exact unsigned-candidate approval before installation.
- Installed-mode update/relaunch through the existing local helper. Portable mode downloads a bundle for manual replacement rather than changing running files in place.
- English-first update guidance, direct Windows/portable download links, and release/bug-report navigation for `DenisShumilov/LimitHalo`.
- English-default installation with optional Ukrainian, automatic provider/appearance/alert defaults, and one explicit quota-only Claude consent.
- One no-Python `LimitHalo-Agent.ps1` entry point for local-bundle Status, Auto, Install, and Repair with closed redacted results and exact unsigned-byte acceptance.
- Offline pre-activation health check, manifest-owned failed-candidate cleanup, an installed-version recovery marker, Start Menu Configure recovery, and fresh config initialization with Claude consent `not-asked`.

### Security

- No unattended installation. This remains an unsigned community build; GitHub/TLS and matching hashes do not authenticate the publisher or replace Authenticode.
- The helper remains the sole owner of bounded process stopping, Setup execution, and verified relaunch. Failed installation is not automatically retried.
- Marker-restoration safeguards are not a guarantee of full automatic rollback. Real-machine install, upgrade, provider, and Windows reboot behavior are not established by metadata or local synthetic checks.

### Changed

- Saved coordinates now represent the user's preferred position. Temporary Windows startup, taskbar, monitor, DPI and layout corrections keep the widget visible without replacing that preference; the next successful display tick restores it when the display is ready.
- A completed drag saves the new position immediately and reports a save failure. Settings changes and rollback preserve the preferred position even while the widget is temporarily clamped.
- Restored automatic Codex and consented Claude collection before the first widget frame after an unfinished update-menu change removed initialization.
- Replaced the previous informational update-menu placeholder with GitHub release checking; local installer status is not treated as an online update check.
- Removed the redundant AutoInstall wrapper from packaging and instructions. AI setup and repair use the single manifest-verified `LimitHalo-Agent.ps1` entry point.
- Refresh retains the existing canvas items and cached provider icons, including preallocated recovery-action targets; it no longer clears and recreates the complete HUD every second.
- Reopening the shortcut or finishing an upgrade reveals the chosen position instead of moving the HUD next to the pointer. Healthy ticks avoid redundant native position, z-order and input-region writes.
- Settings commits preserve the most recent user-selected position, including when a dialog predates a drag. Failed relayout restores the visible window while retaining that preference.
- Compatible unknown settings survive inside provider, alert and quiet-hour objects. Invalid, unreadable or externally changed settings are protected against automatic overwriting; read-only fallback positions remain session-only.
- A transient display callback failure no longer cancels every subsequent refresh tick.
- Claude usage parsing now accepts both the legacy `five_hour` projection and the newer canonical `limits[].session` projection, including a legitimately omitted session reset time. Conflicting, duplicate, stale, or malformed snapshots fail closed without replacing the last good values.
- Provider failures now keep any last good percentage and offer a direct Retry action instead of a generic attention message. Disabled, offline, authorization, refreshing, and optional-not-provided states remain distinct in English and Ukrainian.
- Every HUD placement route uses verified native screen coordinates. Automatic repairs clamp both aligned windows to a monitor work area; only first placement and explicit dragging establish a saved position.
- Fixed the packaged Windows launch crash caused by using the unavailable `ctypes.wintypes.HRESULT`; COM and WinRT bindings now use the raw signed 32-bit Windows `LONG` type expected by their explicit result-code checks.
- Setup now starts with English selected, keeps Ukrainian available on the language page, and explains low-limit warnings in plain language.
- The publication target is `DenisShumilov/LimitHalo`; any runtime or packaging change still requires a new candidate identity and matching release metadata.
- Release validation now compiles one explicitly unsigned setup preview without any installer-execution route; clean-machine lifecycle behavior remains disclosed as unverified.
- Local packaging uses caller-bound staged Python/native toolchains and Python `-I -B`. Native synthetic self-tests are disabled by default and may be explicitly enabled with `-RunNativeSelfTest`; production executables are never launched by the build.
- Startup and Start Menu shortcuts are changed only when target, arguments, and working directory prove LimitHalo ownership; foreign or unreadable links are preserved.
- The original HUD appearance remains unchanged while the daily menu is reduced to six essential actions and Settings progressively reveals detected providers, optional alerts, privacy, and safe AI repair.
- Fresh installed mode now starts with Windows using an exact `--startup` route; provider-free startup exits quietly, portable mode remains off, and upgrades preserve the existing choice.

## [1.0.0] - 2026-08-13

### Added

- Transparent compact and wide Windows HUDs for validated Codex and optional Claude quota windows.
- Explicit context-menu settings, privacy, safe diagnostics, refresh, startup, layout, and exit actions without an ordinary-click details popup.
- First-run Claude consent with a complete Claude-off path.
- Fail-closed official Codex app-server discovery and protocol validation.
- Per-user installer design, portable ZIP, deterministic checksums, version metadata, and fixture-only release validation.

### Security

- No telemetry, quota history, browser/chat/log access, or automatic updater.
- Unsupported private Anthropic integration and unsigned community-build limitations are disclosed.

[Release history](https://github.com/DenisShumilov/LimitHalo/releases) · [Update guide](docs/UPDATES.md)
