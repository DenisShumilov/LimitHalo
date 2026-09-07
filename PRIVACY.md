# Privacy

LimitHalo is designed to show current quota windows without collecting a usage history. It has no telemetry, analytics, crash upload, advertising, tracking, browser extension, local web server, or unattended update installation.

## Data read in production

Codex is read through the official signed `codex.exe app-server --listen stdio://` interface. Discovery reads only public current-user package metadata needed to locate an official npm or OpenAI.Codex AppX executable. It does not read Codex credentials, tasks, chats, transcripts, settings, logs, or unrelated package files.

Claude credential access is off by default even when the Auto-detected Claude lane is visible. After explicit informed consent, a short-lived native broker reads only the `claudeAiOauth` projection from `%USERPROFILE%\.claude\.credentials.json`, coordinated by `%USERPROFILE%\.claude\.storage-write.lock`. It does not transfer the access token, refresh token, credential document, account identity, raw provider response, chat, transcript, browser data, or provider log to the Python app.

The Claude integration uses an **unsupported private Anthropic OAuth usage/refresh interface**. The broker uses only:

- `GET https://api.anthropic.com/api/oauth/usage`
- `POST https://platform.claude.com/v1/oauth/token` when provider-authorized refresh is necessary

During refresh, the broker may atomically replace only the `claudeAiOauth` projection while preserving unrelated bytes under the coordination lock. Turning Claude off prevents future broker launches without reading or editing that file.

## Data sent

The widget itself sends no telemetry. The official Codex app-server may use its existing first-party authentication to obtain rate-limit state. The Claude broker sends only the requests described above after consent.

The updater checks public release metadata at `api.github.com/repos/DenisShumilov/LimitHalo/releases` after startup and every six hours, or when requested in the menu. Only after a download click does it retrieve this repository's release files through `github.com`, `release-assets.githubusercontent.com`, or `objects.githubusercontent.com`. It sends no GitHub token, provider credentials, quota values, settings or account identifiers. GitHub still sees the ordinary connection metadata, including IP address and updater User-Agent. See [Updates](docs/UPDATES.md).

## Data kept

User-requested update downloads are stored in separate folders under `%LOCALAPPDATA%\AILimitsWidget\updates`. They contain public release artifacts only, no quota or authentication data. Failed or cancelled downloads remain inert. This cache is separate from the preferences file described below; old download folders may be removed by the owner when no update is running.

Quota values, reset times, raw frames, and last-good provider values remain in memory and are discarded when the app exits. They are not written to disk.

Preferences are stored in `%LOCALAPPDATA%\AILimitsWidget\config.json`. The versioned settings schema may contain language, provider visibility selection, window position, topmost, display preset, colors, alerts/threshold/quiet hours, start-with-Windows preference, and the non-secret Claude consent enum `not-asked`, `granted`, or `denied`. Normal setup uses English, Auto providers, the original monochrome compact view, alerts/quiet hours off, and installed autostart on; upgrades preserve the person's existing setting and portable mode forces it off. Interactive setup writes `granted` only when the person selects its one unchecked quota-only consent choice. AI installation cannot preselect or emit it. Otherwise fresh settings use `not-asked`. Settings must not contain tokens, credentials, account identifiers, provider bodies or values, URLs, raw exceptions, chats, logs, precise activity timestamps, or quota history.

Portable mode changes packaging and shortcut behavior only. It writes the same bounded settings at `%LOCALAPPDATA%\AILimitsWidget\config.json` and never writes settings beside the executable. The installer owns its versioned payload, Start Menu/optional Startup shortcuts, and one current-user Apps & Features entry.

## Safe diagnostics

The copyable diagnostic surface is allowlisted to application name/version, Windows family/architecture, effective DPI bucket, settings schema version, provider state enum, freshness-age bucket, retry-after bucket, and a redacted error-category code. It excludes usernames, absolute user paths, hosts/URLs, headers, bodies, tokens or hashes of tokens, account identifiers, raw exception text, provider text, chats, logs, and precise activity timestamps.

## Removal

Uninstall removes manifest-owned program files, shortcuts, and the Apps & Features registration. Settings are preserved by default and deleted only when the user explicitly selects **Remove local settings**. Uninstall never reads or modifies Claude or Codex credentials. Portable users can exit the app and delete the portable folder; the shared LocalAppData settings remain until the user deletes `%LOCALAPPDATA%\AILimitsWidget\config.json`.

`LimitHalo-Agent.ps1` never opens the settings or provider credential files. Its closed result contains only bounded artifact/installation state and fixed enums; it omits paths, email, username, account identity, tokens, credential locations, provider values/output, browser/chat/log data, and authentication state. Authentication and provider consent remain visible human-only actions.

## Integration and affiliation limits

Codex depends on the **official Codex app-server**. Claude depends on an unsupported private Anthropic OAuth usage/refresh interface. This project is **not affiliated with, sponsored by, or endorsed by Anthropic or OpenAI**. Both integrations validate identity/protocol and fail closed: if an interface or trust condition drifts, the affected provider becomes unavailable instead of relaxing access.

Operating-system, TLS, credential-manager, administrator, debugger, kernel, firmware, physical-access, and same-user malware behavior is outside the app's memory-containment claim. See [SECURITY.md](SECURITY.md) and [INTEGRATIONS.md](INTEGRATIONS.md).
