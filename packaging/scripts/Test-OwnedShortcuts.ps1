[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot,
    [Parameter(Mandatory = $true)][string]$AuthorizedStateRoot,
    [Parameter(Mandatory = $true)][string]$TestOutputRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-Within([string]$Candidate, [string]$Parent) {
    $child = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $root = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\')
    return $child.StartsWith($root + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparseComponents([string]$PathValue, [string]$Label) {
    $item = Get-Item -Force -LiteralPath $PathValue
    while ($null -ne $item) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label contains a reparse component" }
        if ($item -is [System.IO.FileInfo]) { $item = $item.Directory }
        elseif ($item -is [System.IO.DirectoryInfo]) { $item = $item.Parent }
        else { throw "$Label contains an unsupported filesystem object" }
    }
}

$repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$state = (Resolve-Path -LiteralPath $AuthorizedStateRoot).Path
$output = [System.IO.Path]::GetFullPath($TestOutputRoot)
if (-not (Test-Within $repository $state)) { throw 'RepositoryRoot must be a strict descendant of AuthorizedStateRoot' }
if (-not (Test-Within $output $state)) { throw 'TestOutputRoot must be a strict descendant of AuthorizedStateRoot' }
Assert-NoReparseComponents $repository 'RepositoryRoot'
Assert-NoReparseComponents $state 'AuthorizedStateRoot'
if (Test-Path -LiteralPath $output) {
    if (-not (Test-Path -LiteralPath $output -PathType Container) -or @(Get-ChildItem -Force -LiteralPath $output).Count -ne 0) {
        throw 'TestOutputRoot must be absent or empty'
    }
} else {
    New-Item -ItemType Directory -Path $output | Out-Null
}
Assert-NoReparseComponents $output 'TestOutputRoot'

$helper = Join-Path $repository 'packaging\installer\Invoke-OwnedShortcut.ps1'
$powershell = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
$appData = Join-Path $output 'appdata'
$product = Join-Path $output 'AILimitsWidget'
$candidateA = ('a' * 64) -join ''
$candidateB = ('b' * 64) -join ''
$candidateRootA = Join-Path (Join-Path $product 'versions') $candidateA
$candidateRootB = Join-Path (Join-Path $product 'versions') $candidateB
New-Item -ItemType Directory -Path $appData, $candidateRootA, $candidateRootB -Force | Out-Null
[System.IO.File]::WriteAllBytes((Join-Path $candidateRootA 'AILimitsWidget.exe'), [byte[]](1, 2, 3))
[System.IO.File]::WriteAllBytes((Join-Path $candidateRootB 'AILimitsWidget.exe'), [byte[]](4, 5, 6))

function Invoke-ShortcutHelper([string]$Action, [string]$Kind, [string]$Candidate) {
    $arguments = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $helper,
        '-Action', $Action, '-Kind', $Kind, '-ProductRoot', $product, '-AppDataRoot', $appData
    )
    if (-not [string]::IsNullOrWhiteSpace($Candidate)) { $arguments += @('-CandidateId', $Candidate) }
    $outputText = (& $powershell @arguments 2>&1) -join "`n"
    return [pscustomobject]@{ exitCode = $LASTEXITCODE; output = $outputText }
}

function Read-Shortcut([string]$PathValue) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($PathValue)
    try {
        return [pscustomobject]@{
            target = [string]$link.TargetPath
            arguments = [string]$link.Arguments
            workingDirectory = [string]$link.WorkingDirectory
        }
    } finally {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
    }
}

function Write-Shortcut([string]$PathValue, [string]$Target, [string]$Arguments, [string]$WorkingDirectory) {
    New-Item -ItemType Directory -Path ([System.IO.Path]::GetDirectoryName($PathValue)) -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($PathValue)
    try {
        $link.TargetPath = $Target
        $link.Arguments = $Arguments
        $link.WorkingDirectory = $WorkingDirectory
        $link.Save()
    } finally {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
    }
}

$programs = Join-Path $appData 'Microsoft\Windows\Start Menu\Programs'
$paths = @{
    Hud = Join-Path $programs 'LimitHalo.lnk'
    Configure = Join-Path $programs 'Configure LimitHalo.lnk'
    Startup = Join-Path (Join-Path $programs 'Startup') 'LimitHalo.lnk'
}

foreach ($kind in @('Hud', 'Configure', 'Startup')) {
    $result = Invoke-ShortcutHelper 'Ensure' $kind $candidateA
    if ($result.exitCode -ne 0) { throw "Owned $kind shortcut creation failed: $($result.output)" }
    $actual = Read-Shortcut $paths[$kind]
    $expectedArguments = if ($kind -ceq 'Configure') {
        '--configure'
    } elseif ($kind -ceq 'Startup') {
        '--startup'
    } else {
        ''
    }
    if (-not $actual.target.Equals((Join-Path $candidateRootA 'AILimitsWidget.exe'), [System.StringComparison]::OrdinalIgnoreCase) -or
        $actual.arguments -cne $expectedArguments -or
        -not $actual.workingDirectory.Equals($candidateRootA, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$kind shortcut target/arguments/working-directory triplet mismatch"
    }
}

# An exact legacy owned startup shortcut with empty arguments is migratable.
Write-Shortcut $paths.Startup (Join-Path $candidateRootA 'AILimitsWidget.exe') '' $candidateRootA
$upgrade = Invoke-ShortcutHelper 'Ensure' 'Startup' $candidateB
if ($upgrade.exitCode -ne 0) { throw "Owned startup upgrade failed: $($upgrade.output)" }
$upgraded = Read-Shortcut $paths.Startup
if (-not $upgraded.target.Equals((Join-Path $candidateRootB 'AILimitsWidget.exe'), [System.StringComparison]::OrdinalIgnoreCase) -or
    $upgraded.arguments -cne '--startup' -or
    -not $upgraded.workingDirectory.Equals($candidateRootB, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Owned startup shortcut did not move to the new candidate'
}

$foreignRoot = Join-Path $output 'foreign'
New-Item -ItemType Directory -Path $foreignRoot | Out-Null
$foreignExe = Join-Path $foreignRoot 'foreign.exe'
[System.IO.File]::WriteAllBytes($foreignExe, [byte[]](9))
$ownedTarget = Join-Path $candidateRootA 'AILimitsWidget.exe'
$mutants = @(
    [pscustomobject]@{ name = 'target'; target = $foreignExe; arguments = ''; working = $candidateRootA },
    [pscustomobject]@{ name = 'arguments'; target = $ownedTarget; arguments = '--foreign'; working = $candidateRootA },
    [pscustomobject]@{ name = 'working-directory'; target = $ownedTarget; arguments = ''; working = $foreignRoot }
)
foreach ($mutant in $mutants) {
    Write-Shortcut $paths.Startup $mutant.target $mutant.arguments $mutant.working
    $result = Invoke-ShortcutHelper 'Remove' 'Startup' ''
    if ($result.exitCode -ne 3 -or -not (Test-Path -LiteralPath $paths.Startup -PathType Leaf)) {
        throw "Foreign shortcut $($mutant.name) mutant was not preserved"
    }
    Remove-Item -LiteralPath $paths.Startup
}

[System.IO.File]::WriteAllBytes($paths.Startup, [byte[]](0, 1, 2, 3, 4))
$unreadable = Invoke-ShortcutHelper 'Remove' 'Startup' ''
if ($unreadable.exitCode -ne 3 -or -not (Test-Path -LiteralPath $paths.Startup -PathType Leaf)) {
    throw 'Unreadable shortcut was not preserved'
}
Remove-Item -LiteralPath $paths.Startup

$owned = Invoke-ShortcutHelper 'Ensure' 'Startup' $candidateB
if ($owned.exitCode -ne 0) { throw 'Final owned startup fixture creation failed' }
$removed = Invoke-ShortcutHelper 'Remove' 'Startup' ''
if ($removed.exitCode -ne 0 -or (Test-Path -LiteralPath $paths.Startup)) { throw 'Owned startup shortcut removal failed' }

[pscustomobject]@{
    status = 'PASS'
    targetArgumentsWorkingDirectory = 'PASS'
    legacyStartupMigrated = 'PASS'
    ownedUpgrade = 'PASS'
    foreignTargetPreserved = 'PASS'
    foreignArgumentsPreserved = 'PASS'
    foreignWorkingDirectoryPreserved = 'PASS'
    unreadablePreserved = 'PASS'
}
