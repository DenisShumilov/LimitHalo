# Publishing a LimitHalo beta

Publication target: [DenisShumilov/LimitHalo](https://github.com/DenisShumilov/LimitHalo). The first updater-enabled candidate uses application version **1.0.1** and release tag **`v1.0.1-beta.1`**. This checklist describes a publication workflow; its presence does not establish that a repository, release, or successful real-machine test already exists.

English is the main documentation and release language. [README.uk.md](../README.uk.md) is the supplementary Ukrainian version. Keep the selected cover and real-renderer example with their existing generated/synthetic-data disclosures; neither is proof of live provider access or installation.

## 1. Publish only the intended source

Import this source tree, including `.github`, runtime/native code, build files, selected assets, documentation, and license. Do not upload the enclosing workspace, historical runs, installed app, private settings, credentials, local machine paths, or real-account screenshots.

Suggested repository description:

> A small Windows widget for Codex and optional Claude quota. Transparent, English-first, and AI-installable. Public beta.

Do not ask GitHub to generate a second README, license, or `.gitignore` when importing this tree. Set the repository's About description and relevant topics such as `windows`, `desktop-widget`, `codex`, `claude`, and `productivity`. Do not claim Store or package-manager availability unless those channels actually exist.

Confirm that the issue forms, local Markdown links, cover, and renderer example display correctly. Enable private vulnerability reporting where available or establish a real private reporting route before inviting sensitive reports. Never invent a support email. The [GitHub repository guide](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository) describes the creation options.

## 2. Build one complete, identified candidate

Use the release/build procedure in [Development](DEVELOPMENT.md) and [Packaging](../packaging/README.md). Keep the final source identity, numeric application version, filenames, release tag, manifest, and checksums consistent.

| File for `v1.0.1-beta.1` | Purpose |
| --- | --- |
| `LimitHalo-1.0.1-Setup.exe` | Per-user Windows x64 installer |
| `LimitHalo-1.0.1-Windows-x64.zip` | Portable Windows application |
| `LimitHalo-Agent.ps1` | The only local AI install/repair helper |
| `release-manifest.json` | Candidate, build-source identity, and artifact metadata |
| `SHA256SUMS.txt` | Checksums for the other four release assets |

Use hashes from **this exact build's** manifest and `SHA256SUMS.txt`. Do not reuse hashes or binaries from an earlier 1.0.0/startup-v9 candidate, rename an old binary to look new, or replace its manifest to manufacture provenance. GitHub's generated **Source code** ZIP is not the Windows portable ZIP.

The release manifest identifies the source used to build the binaries. A tag on a different documentation-enriched tree does not retroactively prove those binaries were built from that tag. Prefer building from the final intended source; if publishing with a different source identity, disclose that exact difference in the release notes instead of claiming tag-to-build provenance.

This is an **unsigned community build**. Hash consistency does not identify the publisher or provide Authenticode trust. The updater's GitHub API SHA-256 checks over TLS add transport/asset consistency, not an independent publisher signature.

## 3. Prepare a draft pre-release

Create a draft in [Releases](https://github.com/DenisShumilov/LimitHalo/releases) with:

- Tag: **`v1.0.1-beta.1`** on the intended source commit.
- Title: **LimitHalo 1.0.1 — Public Beta**.
- **Pre-release** selected; do not label the beta stable.
- All five complete, matching assets from the same build.

Use a short release body with **What's new**, **Fixed**, **Known limitations**, **Download**, and **How to update**. Put the Windows x64 installer first, portable second, and explain that the helper needs the complete five-file bundle.

Keep these disclosures:

- Windows 10/11 x64; ARM64 is not supported in this beta.
- English is the fresh default; Ukrainian is optional.
- Codex requires the compatible official app-server and existing sign-in described in [Integrations](../INTEGRATIONS.md).
- Optional Claude requires compatible **Claude Code** OAuth credentials and explicit consent. Claude Desktop sign-in alone is insufficient. Its **unsupported private Anthropic usage/refresh interface** can change or break.
- The build is unsigned. Updates are checked automatically, but downloading and applying an exact unsigned candidate require user action; portable replacement remains manual.
- Marker restoration is not a full automatic rollback guarantee. Describe every unverified real-machine scenario honestly.

The checked-in candidate-build workflow is separate from publication. Do not describe an unobserved, missing, skipped, or failed workflow as a passing release check. See [GitHub's release guide](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).

## 4. Verify the bundle, then publish the draft last

- [ ] Final source scope contains no private state or accidental historical files.
- [ ] Numeric application version, tag, asset names, manifest, and checksums match this candidate.
- [ ] All five uploaded assets are complete and match the exact local build; each GitHub asset exposes the expected SHA-256 digest.
- [ ] Release copy describes actual behavior and remaining limitations, not a guarantee of universal reliability.
- [ ] The English and Ukrainian README download links point to this exact planned tag and filenames.
- [ ] The release remains a draft until the complete bundle and metadata are ready.

Publish the completed draft **last**, then confirm the public release metadata and download links work without relying on a maintainer-only session. If an external publish/upload action gives an ambiguous result, inspect the state before doing anything else; do not blindly repeat it.

Do not expose a partial bundle as a new update or mutate published assets in place. A failed metadata or digest check is not permission to bypass the updater. Correct the release process and issue a new numerically versioned candidate when bytes change.

## 5. Keep live checks separate from file checks

Local automated, hash, and synthetic-window checks do not prove the following scenarios on the exact released candidate:

- Clean Windows installation, first launch, upgrade, repair, uninstall, or failure recovery.
- Startup position after an actual Windows reboot or a real monitor/DPI change.
- Live Codex collection or optional Claude access with consent accepted and declined.
- Real user acceptance of the new update and unsigned-candidate confirmation flow.

Use a safe real Windows environment if these checks are performed. If the owner chooses to release without them, mark them **unverified** in the beta notes; do not imply they passed or ask others to trust a synthetic screenshot as a substitute. Keep bug reports actionable and private-data-free.

## 6. Publish the next update correctly

For each new beta, increase the **numeric application version** and use the matching tag: for example `1.0.2` / `v1.0.2-beta.1`. Changing only a beta suffix is insufficient for numeric version discovery. Build and verify a new complete candidate, draft the release, upload all five assets, and publish only after they match.

The updater reads the releases listing, because GitHub's `/releases/latest` excludes pre-releases. Do not depend on the **Latest** badge to deliver beta updates. The [update guide](UPDATES.md) documents the six-hour background checks, manual check, explicit download, exact-candidate approval, and installed/portable distinction.

## 7. Announce once public links work

Use the real repository/release link in English LinkedIn or Reddit copy; Ukrainian can be an additional post. Check the destination community's current rules and flair before posting. Identify yourself as the builder and the product as a beta, and ask for specific feedback.

Use the generated cover as a labeled promotional concept. A real-machine demo, if later made, can demonstrate the exact visible actions it records—not universal stability. Do not invent users, saved time, “zero bugs,” or guaranteed popularity.
