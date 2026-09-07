[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AuthorizedStateRoot,
    [Parameter(Mandatory = $true)][string]$TestOutputRoot,
    [Parameter(Mandatory = $true)][string]$PythonExe
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$candidateRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
function Test-AbsoluteDrivePath([string]$PathValue) {
    return [System.IO.Path]::IsPathRooted($PathValue) -and $PathValue -cmatch '^[A-Za-z]:[\\/]'
}
function Test-Within([string]$Candidate, [string]$Parent) {
    $child = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $root = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\')
    return $child.Equals($root, [System.StringComparison]::OrdinalIgnoreCase) -or $child.StartsWith($root + '\', [System.StringComparison]::OrdinalIgnoreCase)
}
function Assert-NoReparseComponents([string]$PathValue, [string]$Label) {
    $cursor = Get-Item -LiteralPath $PathValue -Force
    while ($null -ne $cursor) {
        if (($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label contains a reparse component" }
        $cursor = $cursor.Parent
    }
}
foreach ($value in @($AuthorizedStateRoot, $TestOutputRoot, $PythonExe)) {
    if (-not (Test-AbsoluteDrivePath $value)) { throw 'AuthorizedStateRoot, TestOutputRoot, and PythonExe must be absolute local drive paths' }
}
$pythonRuntime = (Resolve-Path -LiteralPath $PythonExe).Path
if (-not (Test-Path -LiteralPath $pythonRuntime -PathType Leaf) -or
    [System.IO.Path]::GetExtension($pythonRuntime) -ine '.exe' -or
    $pythonRuntime -match '(?i)[\\/]Microsoft[\\/]WindowsApps[\\/]') {
    throw 'PythonExe must be the resolved interpreter executable, not an app-execution alias'
}
$state = (Resolve-Path -LiteralPath $AuthorizedStateRoot).Path
if (-not (Test-Path -LiteralPath $state -PathType Container)) { throw 'AuthorizedStateRoot must be a directory' }
Assert-NoReparseComponents $state 'AuthorizedStateRoot'
Assert-NoReparseComponents $candidateRoot 'CandidateRoot'
if (-not (Test-Within $candidateRoot $state) -or $candidateRoot.Equals($state, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'CandidateRoot must be a strict descendant of AuthorizedStateRoot'
}
$outputRoot = [System.IO.Path]::GetFullPath($TestOutputRoot)
if (-not (Test-Within $outputRoot $state) -or $outputRoot.Equals($state, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'TestOutputRoot must be a strict descendant of AuthorizedStateRoot'
}
if (-not (Test-Path -LiteralPath $outputRoot)) { New-Item -ItemType Directory -Path $outputRoot | Out-Null }
if (-not (Test-Path -LiteralPath $outputRoot -PathType Container)) { throw 'TestOutputRoot must be a directory' }
Assert-NoReparseComponents $outputRoot 'TestOutputRoot'
$profileRoot = Join-Path $outputRoot 'profile'
$localAppDataRoot = Join-Path $profileRoot 'local-app-data'
$roamingAppDataRoot = Join-Path $profileRoot 'roaming-app-data'
$userProfileRoot = Join-Path $profileRoot 'user-profile'
New-Item -ItemType Directory -Force -Path $localAppDataRoot, $roamingAppDataRoot, $userProfileRoot | Out-Null
$env:TEMP = $outputRoot
$env:TMP = $outputRoot
$env:LOCALAPPDATA = $localAppDataRoot
$env:APPDATA = $roamingAppDataRoot
$env:USERPROFILE = $userProfileRoot
$env:PYTHONPYCACHEPREFIX = Join-Path $outputRoot 'pycache'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONHASHSEED = '0'
$env:LIMIT_HALO_TEST_ROOT = $outputRoot
$env:LIMIT_HALO_FIXTURE_ONLY = '1'
$env:PIP_NO_INDEX = '1'
$env:HTTP_PROXY = 'http://127.0.0.1:9'
$env:HTTPS_PROXY = 'http://127.0.0.1:9'
$env:ALL_PROXY = 'http://127.0.0.1:9'
$env:NO_PROXY = 'localhost,127.0.0.1'

$unittestBootstrap = @'
import pathlib
import sys
import unittest

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "src"))
suite = unittest.defaultTestLoader.discover(
    str(root / "tests"),
    pattern="test_*.py",
    top_level_dir=str(root),
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
'@

$commandPlan = @(
    [pscustomobject]@{ label = 'source closure'; arguments = @('-I', '-B', (Join-Path $PSScriptRoot 'check_source.py')) },
    [pscustomobject]@{ label = 'offline tests'; arguments = @('-I', '-B', '-c', $unittestBootstrap, $candidateRoot) }
)
foreach ($command in $commandPlan) {
    $joined = $command.arguments -join ' '
    if ($joined -match '(?i)https?://|ftp://|\\\\|\bpip\b|\bdownload\b|\binstall\b') {
        throw ('Offline command plan contains a forbidden acquisition route: ' + $command.label)
    }
}

foreach ($command in $commandPlan) {
    & $pythonRuntime @($command.arguments)
    if ($LASTEXITCODE -ne 0) { throw ($command.label + ' failed') }
}
