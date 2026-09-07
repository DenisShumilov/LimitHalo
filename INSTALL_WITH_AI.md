# Install or repair LimitHalo with AI

Download the complete Windows bundle from [official LimitHalo releases](https://github.com/DenisShumilov/LimitHalo/releases), then give your coding AI the local folder containing all five matching assets:

- `LimitHalo-1.0.1-Setup.exe`
- `LimitHalo-1.0.1-Windows-x64.zip`
- `LimitHalo-Agent.ps1`
- `release-manifest.json`
- `SHA256SUMS.txt`

These filenames describe `v1.0.1-beta.1`; use the matching filenames and manifest for a later release, never a mixture. GitHub's automatic **Source code** archives are not a runnable Windows bundle. The helper downloads nothing and needs no Python. An AI must use the helper from this exact local bundle, not invent a remote install script or installer switches.

Ask the AI to run one command:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\LimitHalo-Agent.ps1 Auto
```

`Auto` selects one bounded outcome: install when absent, do nothing when healthy, launch when healthy but stopped, repair damaged product-owned files, or stop when state is foreign, ambiguous, locked, or unsafe. The other public actions are `Status`, `Install`, and `Repair`. `Status` never changes the computer.

Installation, launch, and repair require your approval of the exact value returned in the JSON `acceptance` field. After reviewing it, explicitly tell the AI to repeat the action with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\LimitHalo-Agent.ps1 Auto -AcceptUnsignedExact <exact-value>
```

That value binds the candidate ID, Setup SHA-256, and helper SHA-256. Any byte change invalidates it. The current bundle remains `UNSIGNED_UNTRUSTED`: matching hashes prove consistency only, not publisher authenticity. Never approve an unexpected bundle.

Exit codes are deterministic: `0` completed, `10` human action required, `20` invalid or unsafe, and `30` failed safely. Do not retry `20` or `30` automatically.

Before an upgrade or repair, the helper will stop a running widget only when there is exactly one matching process and its executable is the exact manifest-owned active executable. It first requests a normal close and waits two seconds. Because the current HUD may ignore that request, the helper then rechecks the same process ID, start time, and executable path; only that revalidated process may be forcibly stopped once, followed by one five-second wait. It never retries or kills an ambiguous, replaced, inaccessible, or foreign process, and Setup is not started when stopping fails.

Setup is run exactly once with its own automatic post-install launch disabled. The helper waits only for Setup to finish, validates the installed candidate, starts exactly one copy of that verified installed executable itself, and observes that process during a bounded five-second window before returning its single JSON result. It does not wait for the widget's lifetime, and it does not automatically retry Setup or launch.

The helper never reads LimitHalo settings, provider credentials, account identity, quota values, chats, browser data, or logs. It never grants Claude access or authenticates. Fresh AI installation uses English and automatic provider detection. Ukrainian remains available through manual Setup or LimitHalo settings; Claude consent and provider login remain visible human actions.

## Updating later

An installed LimitHalo copy can discover and download a newer release from its right-click menu. The widget still requests separate approval of the exact unsigned candidate before passing installation to this same helper; it does not give an AI blanket permission to accept future releases. In portable mode, replace the extracted application manually instead of asking the helper to overwrite the running folder. See [How updates work](docs/UPDATES.md).

If an agent is helping with a later release, provide its complete local five-file bundle and use the same `Auto` / exact-acceptance flow above. Do not ask the agent to read provider credentials, automate login or Claude consent, add a second process-stop route, or retry an unsafe/failed helper result. A hash match is byte consistency, not proof of publisher identity or universal Windows compatibility.
