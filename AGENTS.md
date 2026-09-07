# LimitHalo agent instructions

For checking, installing, starting, or repairing LimitHalo, run only `LimitHalo-Agent.ps1` from the same extracted local release bundle. Never invent Setup switches, download another asset, use the obsolete install-plan tools, inspect settings or credentials, or automate provider sign-in.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\LimitHalo-Agent.ps1 Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\LimitHalo-Agent.ps1 Auto
```

If `Auto` returns `ACTION_REQUIRED`, show the person the exact `acceptance` value and explain that this unsigned bundle is only internally consistent, not publisher-authenticated. Continue only after the person explicitly approves that exact value, then repeat with `-AcceptUnsignedExact <value>`. Never pass acceptance merely because the script printed it.

Exit `0` means completed, `10` means human action required, `20` means invalid or unsafe, and `30` means failed safely. Never retry `20` or `30` automatically.

For upgrade or repair, the helper may forcibly stop the widget only after one graceful-close attempt, exactly one two-second graceful wait, and a second exact ownership check of the same PID, start time, and manifest-owned executable path. There is at most one kill attempt followed by at most one five-second post-kill wait. Never add another close, kill, wait, or termination path; never bypass or retry a stop failure; and never start Setup after an ambiguous, replaced, inaccessible, or foreign process result.

For installation or repair completion, Setup must run once with `/OPENAFTERINSTALL=0`, and the helper waits only for Setup. After validating the installed candidate, it starts exactly one verified installed executable without `-Wait`, observes the launched PID and exact executable path within the fixed bounded polling window, then returns one JSON result. Never let widget lifetime hold the helper open, and never retry Setup or launch automatically.
