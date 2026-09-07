param(
    [Parameter(Mandatory=$true)][string]$Bundle,
    [Parameter(Mandatory=$true)][string]$Acceptance,
    [ValidateSet('en','uk')][string]$Language='en'
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2.0
# Windows PowerShell must not inherit PowerShell 7's incompatible module tree.
# Set this before any module-owned command (including Join-Path/Get-FileHash).
$env:PSModulePath=[IO.Path]::Combine($env:SystemRoot,'System32\WindowsPowerShell\v1.0\Modules')
# This installed wrapper is the only updater launch route. The existing local
# helper remains the sole writer of process/install/shortcut/marker state.
try {
    if ($Acceptance -cnotmatch '^(?:[0-9a-f]{64}:){2}[0-9a-f]{64}$') { throw 'CONSENT_INVALID' }
    $parts=$Acceptance.Split(':')
    $stage=(Resolve-Path -LiteralPath $Bundle).Path
    $allowed=[IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'AILimitsWidget\updates')).TrimEnd('\')+'\'
    if (-not $stage.StartsWith($allowed,[StringComparison]::OrdinalIgnoreCase)) { throw 'STAGE_INVALID' }
    $parent=Get-Item -LiteralPath $stage -Force
    while ($null -ne $parent) {
        if (($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'STAGE_INVALID' }
        $parent=$parent.Parent
    }
    $helper=Join-Path $stage 'LimitHalo-Agent.ps1'
    $manifest=Join-Path $stage 'release-manifest.json'
    foreach ($file in @($helper,$manifest)) {
        $item=Get-Item -LiteralPath $file -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'STAGE_INVALID' }
    }
    if ((Get-FileHash -LiteralPath $helper -Algorithm SHA256).Hash.ToLowerInvariant() -cne $parts[2]) { throw 'HELPER_CHANGED' }
    $m=Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($m.candidateId -cne $parts[0]) { throw 'CANDIDATE_CHANGED' }
    $setup=@($m.artifacts | Where-Object { $_.path -cmatch '^LimitHalo-[0-9]+\.[0-9]+\.[0-9]+-Setup\.exe$' })
    if ($setup.Count -ne 1 -or $setup[0].sha256 -cne $parts[1]) { throw 'SETUP_CHANGED' }
    # No downloaded code runs before exact user confirmation. No retry.
    # Console.Out is OS stdout, not PowerShell pipeline output. Drain both
    # pipes concurrently, retaining at most 16 KiB each, even on bad output.
    Add-Type -TypeDefinition @'
using System.IO;
using System.Text;
using System.Threading.Tasks;
public static class LimitHaloBoundedOutput {
    public static async Task<string> Drain(StreamReader reader) {
        var text = new StringBuilder();
        var buffer = new char[1024];
        int bytes = 0, count;
        bool exceeded = false;
        while ((count = await reader.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) != 0) {
            if (exceeded) continue;
            bytes += Encoding.UTF8.GetByteCount(buffer, 0, count);
            if (bytes > 16384) { exceeded = true; text.Clear(); }
            else text.Append(buffer, 0, count);
        }
        return exceeded ? null : text.ToString();
    }
}
'@
    $start=New-Object Diagnostics.ProcessStartInfo
    $start.FileName=[IO.Path]::Combine($env:SystemRoot,'System32\WindowsPowerShell\v1.0\powershell.exe')
    # Windows filenames cannot contain a quote; Acceptance is already hex-only.
    $start.Arguments='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$helper+'" Auto -AcceptUnsignedExact '+$Acceptance
    $start.WorkingDirectory=$stage
    $start.UseShellExecute=$false
    $start.CreateNoWindow=$true
    $start.WindowStyle=[Diagnostics.ProcessWindowStyle]::Hidden
    $start.RedirectStandardOutput=$true
    $start.RedirectStandardError=$true
    $start.EnvironmentVariables['PSModulePath']=$env:PSModulePath
    $child=New-Object Diagnostics.Process
    $child.StartInfo=$start
    if (-not $child.Start()) { throw 'UPDATE_STOPPED' }
    $stdout=[LimitHaloBoundedOutput]::Drain($child.StandardOutput)
    $stderr=[LimitHaloBoundedOutput]::Drain($child.StandardError)
    # Remain pending while the helper waits for Setup. Never time out, kill,
    # or retry an installation that could still be changing files. The helper
    # does NOT wait for the relaunched widget's lifetime.
    $child.WaitForExit()
    $code=$child.ExitCode
    $resultText=$stdout.GetAwaiter().GetResult()
    $errorText=$stderr.GetAwaiter().GetResult()
    $child.Dispose()
    if ($code -ne 0 -or [string]::IsNullOrWhiteSpace($resultText) -or $null -eq $errorText -or -not [string]::IsNullOrWhiteSpace($errorText)) {
        throw 'UPDATE_STOPPED'
    }
    $result=ConvertFrom-Json -InputObject $resultText
    if ($result -is [array] -or $result.schemaVersion -cne '1.0.0' -or
        $result.kind -cne 'LimitHaloAgentResult' -or $result.action -cne 'Auto' -or
        $result.status -cne 'OK' -or $result.acceptance -cne $Acceptance -or
        $result.candidateId -cne $parts[0] -or $result.setupSha256 -cne $parts[1] -or
        $result.helperSha256 -cne $parts[2] -or $result.launched -isnot [bool] -or $result.launched -ne $true) {
        throw 'UPDATE_STOPPED'
    }
    exit 0
} catch {
    Add-Type -AssemblyName PresentationFramework
    $message=if($Language -ceq 'uk') {
        # ASCII source is required by Windows PowerShell 5.1 (no UTF-8 BOM).
        -join ([char[]]@(1054,1085,1086,1074,1083,1077,1085,1085,1103,32,1079,1091,1087,1080,1085,1077,1085,1086,32,1073,1077,1079,32,1087,1086,1074,1090,1086,1088,1085,1086,1111,32,1089,1087,1088,1086,1073,1080,46,32,1042,1110,1076,1082,1088,1080,1081,1090,1077,32,1079,1072,1074,1072,1085,1090,1072,1078,1077,1085,1080,1081,32,1074,1080,1087,1091,1089,1082,32,1110,32,1087,1077,1088,1077,1076,1072,1081,1090,1077,32,76,105,109,105,116,72,97,108,111,45,65,103,101,110,116,46,112,115,49,32,83,116,97,116,117,115,32,1089,1074,1086,1108,1084,1091,32,1064,1030,45,1087,1086,1084,1110,1095,1085,1080,1082,1091,46,32,1053,1077,32,1074,1080,1076,1072,1083,1103,1081,1090,1077,32,1087,1086,1087,1077,1088,1077,1076,1085,1102,32,1074,1077,1088,1089,1110,1102,46,32,1055,1072,1087,1082,1072,58,32))
    } else {
        'The update stopped without retrying. Open the downloaded release and ask your AI assistant to run LimitHalo-Agent.ps1 Status. Do not delete the previous version. Folder: '
    }
    [void][Windows.MessageBox]::Show(($message+$Bundle),'LimitHalo update','OK','Warning')
    exit 30
}
