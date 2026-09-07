# Integration boundaries

LimitHalo is a community display client, not a provider product. It is **not affiliated with, sponsored by, or endorsed by Anthropic or OpenAI**. Provider names and marks identify integrations only.

## Claude: unsupported private OAuth usage/refresh interface

Claude support uses an **unsupported private Anthropic OAuth usage/refresh interface observed in first-party Claude applications**. Anthropic does not publish or support this interface for third-party applications. It may change or stop working without notice.

Claude credential access is disabled by default. The provider lane uses Auto detection, but no Claude collector or broker can be constructed until the person grants the unchecked quota-only permission after reading its exact boundary. Continue without enabling, Escape, close, or an incomplete decision leaves credential access off.

Provider detection alone is not Claude data-access consent. Normal interactive setup has one product-specific page with the complete disclosure and an unchecked quota-only consent choice; it may record `claudeConsent: "granted"` only after the person selects it. An AI or silent install plan cannot preselect or emit `granted`; leaving the choice unchecked records `not-asked` and exposes the same human-only consent through the visible in-app **Connect** action. Any provider-owned login remains outside LimitHalo. When authorization recovery is needed, LimitHalo may launch only the validated official client and then recheck automatically; it never types credentials or completes login for the person.

After consent, a short-lived native broker may:

1. Read only `claudeAiOauth` from `%USERPROFILE%\.claude\.credentials.json` under `%USERPROFILE%\.claude\.storage-write.lock`.
2. Call `GET https://api.anthropic.com/api/oauth/usage`.
3. If the provider requires an OAuth refresh, call `POST https://platform.claude.com/v1/oauth/token` and atomically replace only the `claudeAiOauth` projection while preserving every unrelated byte.
4. Emit to the GUI only three sanitized percentages, provider-supplied reset timestamps (or an explicit missing reset), and a fixed status enum.

The GUI never receives a Claude access token, refresh token, credential blob, account identity, headers, raw HTTP body, chat, transcript, browser data, or provider log. Turning Claude off stops future broker launches without reading or editing credentials.

The broker accepts the observed legacy top-level session projection and the observed `limits` session projection. If both are present they must agree; duplicate, conflicting, stale, malformed, or partial required data fails closed while the GUI retains its last good in-memory values. Optional missing windows remain explicitly unavailable. Compatibility changes must add rejected and accepted offline fixtures while preserving the exact consent and data boundary. There is no guarantee that this private interface will remain available.

## Codex: official app-server dependency

Codex support depends on an **official Codex app-server** reached through `codex.exe app-server --listen stdio://`. The product locates only:

- the current-user official `@openai/codex` npm package and its declared Windows x64 dependency, without executing npm, Node, scripts, shims, or hooks; or
- current-user public metadata for the exact `OpenAI.Codex` AppX package and `app/resources/codex.exe` beneath its canonical install root.

Before selection, a candidate must be a canonical, non-reparse regular file named `codex.exe` beneath its exact resolved official root. Cache-only Authenticode validation must report `Valid`, include Code Signing EKU, and have leaf subject exactly `CN=\"OpenAI OpCo, LLC\", O=\"OpenAI OpCo, LLC\", L=San Francisco, S=California, C=US`. Missing trust data, path ambiguity, wrong signer, invalid/unsigned files, or malformed metadata fail closed.

Compatibility is checked in the first acquisition session, not by a hidden probe. The only permitted sequence is `initialize`, `initialized`, and `account/rateLimits/read`, with browser/computer/plugin/hook/apps features disabled and `mcp_servers={}`. Thread/turn methods, server requests, unexpected envelopes, invalid schemas, zero positive windows, or duplicate durations terminate the acquisition. One or two distinct returned windows are displayed with labels derived from `windowDurationMins`; no daily window is invented.

The official app-server may use its existing first-party authentication. LimitHalo does not read Codex credentials, tasks, chats, transcripts, logs, or unrelated user data.

Signer, package-layout, or protocol drift fails closed to Codex unavailable. Frozen hashes in tests are compatibility controls, not permanent public-version allowlists.

## Shared rules

- No telemetry, analytics, quota history, raw provider responses, or account identifiers are persisted.
- Provider errors are reduced to allowlisted state and redacted category codes.
- Build, CI, tests, and demos use offline fixtures only and must not inspect installed provider applications or user authentication.
- The bundled AI entry point accepts no provider, path, URL, arbitrary command, secret, authentication, or consent input. It can check, install, launch, or conservatively repair only the exact local LimitHalo bundle; provider consent and login remain human-only.
- GitHub update checking is separate from provider access. Version 1.0.1 uses the pinned public repository, with user-triggered download and exact unsigned confirmation before installation; there is no unattended installation. See [Updates](docs/UPDATES.md).
- Integration changes fail closed; compatibility drift never relaxes identity, credential, method, or network boundaries.
