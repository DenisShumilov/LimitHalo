# Updating LimitHalo

LimitHalo uses releases from [DenisShumilov/LimitHalo](https://github.com/DenisShumilov/LimitHalo/releases). A release must be published with the complete Windows bundle before it can be offered. Drafts and a Git tag without a release are not updates.

## The everyday flow

1. **Check quietly.** A background check starts about 15 seconds after launch and repeats every six hours. Choose **Check for updates** from the right-click menu for an immediate check.
2. **See the version.** The context menu shows an available newer version. Background discovery does not open an intrusive pop-up or start installation.
3. **Choose Download.** LimitHalo fetches all five release assets listed below and verifies the complete bundle. Downloading both Setup and the portable ZIP is intentional: the existing local helper requires that exact bundle. The time depends on the network and disk, not a fixed speed promise.
4. **Approve the exact candidate.** Review the unsigned-build warning and candidate identity. Installation needs a separate explicit confirmation for those exact bytes. A different candidate requires new approval.
5. **Update and relaunch.** In installed mode, the existing local helper owns the installer and relaunch process. Portable mode stops at a downloaded bundle for manual replacement.

This is automatic **checking**, with user-triggered download and installation. There is no unattended installation or silent publisher trust. The updater does not sign in to GitHub or to an AI provider on your behalf.

## What is verified—and what is not

The updater checks downloaded assets against SHA-256 digests supplied by the GitHub API over TLS, then validates the bundle's internal manifest and hashes. The helper checks the exact local candidate again before installing it.

These checks detect inconsistent or changed bytes. They do **not** establish the publisher's identity independently of GitHub, protect against a compromised publishing account supplying an entirely new bundle, or provide Windows Authenticode trust. The beta remains **unsigned**; matching checksums do not make it publisher-authenticated. Never approve a candidate you do not trust.

The updater contacts GitHub for release metadata and downloads. As with any connection, GitHub can see the request and network metadata. This is separate from provider quota collection; it is not a channel for uploading your chats, credentials, or widget preferences. See [Privacy](../PRIVACY.md).

## Installed and portable copies

| Mode | What happens after download |
| --- | --- |
| Installed | After exact unsigned-candidate approval, `LimitHalo-Agent.ps1` validates and applies the bundle, then launches the verified installed executable. Existing preferences—including chosen position and startup choice—are preserved by the installed upgrade path. |
| Portable | No in-place automatic replacement. Close the portable widget, extract the new Windows ZIP into a separate folder, and run that copy. Do not merge files from two releases. Preferences are stored outside the portable folder. |

The helper is the single owner of process stopping, Setup execution, and verified relaunch. An AI must not add its own stop/kill/install/retry sequence around it. [Install or repair with AI](../INSTALL_WITH_AI.md) describes the exact local-bundle acceptance flow.

## If something goes wrong

- **No newer release:** the current version remains in use. Publishing documentation or a tag alone does not supply an update.
- **Offline or GitHub unavailable:** a failed check is not evidence that the installed widget needs repair. Check again when the connection is available.
- **Missing files or failed verification:** do not install a partial or inconsistent release, edit its manifest, or bypass a digest failure.
- **Installation fails:** do not automatically retry Setup, stop processes, or claim the update completed. Follow the visible result; use the local helper's read-only `Status` for a bounded diagnosis.
- **Rollback:** the installed path has marker-restoration safeguards, not a promise of full automatic rollback after every failure. It does not prove recovery from an interrupted Windows session, damaged disk, or arbitrary external changes.

Local checks do not prove real-machine installation, upgrade, provider access, or placement after a Windows reboot. Those live scenarios remain separate from release metadata and hash verification. Report a reproducible issue through the [bug form](https://github.com/DenisShumilov/LimitHalo/issues/new?template=bug_report.yml), without credentials or private logs.

## Maintainer release rules

The first updater-enabled beta uses application version **1.0.1** and tag **`v1.0.1-beta.1`**. It contains exactly these five named assets:

| Asset | Purpose |
| --- | --- |
| `LimitHalo-1.0.1-Setup.exe` | Per-user Windows installer |
| `LimitHalo-1.0.1-Windows-x64.zip` | Portable Windows bundle |
| `LimitHalo-Agent.ps1` | Local install/repair helper |
| `release-manifest.json` | Candidate and artifact metadata |
| `SHA256SUMS.txt` | Checksums for the other four assets |

For **every subsequent beta**, increase the numeric application version and use the matching `vX.Y.Z-beta.1` tag—for example, `1.0.2` / `v1.0.2-beta.1`. Do not ship changed bytes under the same numeric version or merely change `beta.1` to `beta.2`: the numeric version must move forward for existing installations.

Build the complete candidate, create a **draft pre-release**, upload all five assets, and verify their metadata/digests against that exact build. Publish the completed draft last. Never expose an incomplete release as an update, edit published bytes in place, or reuse old checksums for a new build. A source-code ZIP created automatically by GitHub is not the Windows ZIP above.

Beta discovery uses the releases listing, not `/releases/latest`: GitHub's latest-release endpoint excludes pre-releases and drafts. See the official [GitHub release API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release) and [release asset API](https://docs.github.com/en/rest/releases/assets#get-a-release-asset). The longer [publishing checklist](PUBLISHING.md) covers source scope, disclosures, and release handoff.
