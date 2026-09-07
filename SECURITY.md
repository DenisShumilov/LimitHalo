# Security policy

## Supported versions

Only the latest published `1.x` release is intended to receive security fixes. This repository fragment is a local candidate, not evidence that a public signed release or hosted workflow has completed.

## Reporting a vulnerability

Use GitHub private vulnerability reporting from the repository **Security** tab if the maintainer has enabled it. If no private channel is available, open a minimal issue requesting a private contact route. Do not attach or paste credentials, OAuth material, account identifiers, raw requests/responses, absolute user paths, chats, transcripts, browser data, screenshots containing private data, or provider/application logs.

Include only the application version, Windows version family and architecture, install type, redacted status/error category, expected behavior, and safe reproduction steps using fixtures. Maintainers should acknowledge a complete report before asking for additional data.

## Security boundary

The product does not automate a browser, inspect browser profiles, read chats/transcripts/provider logs, run a local web server, accept inbound connections, send telemetry, or install updates without explicit user confirmation.

The Claude option is disabled until explicit consent. It uses an **unsupported private Anthropic OAuth usage/refresh interface** through a short-lived native broker. That broker owns the narrow credential read/refresh operation; the GUI never receives tokens, credential blobs, account identity, raw bodies, or unstructured provider text.

Codex depends on the **official Codex app-server**. Discovery requires a canonical, non-reparse regular `codex.exe` beneath an official resolved root, a cache-only valid Authenticode result, Code Signing EKU, the exact OpenAI signer subject, and compatible allowlisted protocol behavior. Discovery never executes npm, Node, package scripts, PATH shims, or lifecycle hooks.

This community project is **not affiliated with, sponsored by, or endorsed by Anthropic or OpenAI**. Signer, trust, identity, schema, or allowlisted-protocol drift fails closed to the affected provider being unavailable. Do not patch around those checks to restore compatibility; update fixtures and receive review first.

Integration changes fail closed: a mismatch makes that provider unavailable and never broadens discovery, credentials, methods, or network access.

## Build and release safety

- Pull-request validation uses fixture data, read-only repository permissions, pinned action commits, and no repository secrets.
- Local build/test/demo routes must use explicit roots and zero live provider requests.
- Automated validation compiles and statically verifies the unsigned setup preview but never launches it. Clean-machine installer lifecycle behavior remains unverified.
- Initial community builds are unsigned unless the exact `release-manifest.json` records a verified Authenticode signer. The project makes no SmartScreen or publisher-trust claim.
- The bundled `LimitHalo-Agent.ps1` accepts only `Auto`, `Status`, `Install`, or `Repair`, uses only its extracted local bundle, reads no settings or credentials, performs no authentication, and never retries Setup automatically. Current unsigned community artifacts are labeled `UNSIGNED_UNTRUSTED`. A human must explicitly accept the exact candidate/Setup/helper hash triple before a mutating action; that detects byte changes but does not establish publisher authenticity.
- Version 1.0.1 checks the pinned public GitHub repository for updates. Download and installation require user action. All five assets are checked against GitHub API digests and local manifests before any downloaded code runs; the exact unsigned candidate/Setup/helper tuple must be confirmed. This trusts the GitHub publishing account, not an independently authenticated publisher. See [Updates](docs/UPDATES.md) for portable mode and recovery limitations.

## Threat-model limits

The app's containment claims do not cover opaque internal copies made by Windows Credential Manager, WinHTTP, SChannel, the operating system, or the provider. Administrator, debugger, kernel, firmware, physical-access, and same-user malware compromise are outside the threat model. Provider availability and permanence of private interfaces are not guaranteed.
