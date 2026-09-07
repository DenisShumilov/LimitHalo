[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot,
    [Parameter(Mandatory = $true)][string]$AuthorizedStateRoot,
    [Parameter(Mandatory = $true)][string]$TestOutputRoot,
    [Parameter(Mandatory = $true)][string]$PythonExe
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
function Test-AbsoluteDrivePath([string]$PathValue) {
    return [System.IO.Path]::IsPathRooted($PathValue) -and $PathValue -cmatch '^[A-Za-z]:[\\/]'
}

function Test-Within([string]$Candidate, [string]$Parent) {
    $child = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $root = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\')
    return $child.Equals($root, [System.StringComparison]::OrdinalIgnoreCase) -or
        $child.StartsWith($root + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparseComponents([string]$PathValue, [string]$Label) {
    $cursor = Get-Item -LiteralPath $PathValue -Force
    while ($null -ne $cursor) {
        if (($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label contains a reparse component" }
        $cursor = $cursor.Parent
    }
}

foreach ($value in @($RepositoryRoot, $AuthorizedStateRoot, $TestOutputRoot, $PythonExe)) {
    if (-not (Test-AbsoluteDrivePath $value)) { throw 'RepositoryRoot, AuthorizedStateRoot, TestOutputRoot, and PythonExe must be absolute local drive paths' }
}
$pythonRuntime = (Resolve-Path -LiteralPath $PythonExe).Path
if (-not (Test-Path -LiteralPath $pythonRuntime -PathType Leaf) -or
    [System.IO.Path]::GetExtension($pythonRuntime) -ine '.exe' -or
    $pythonRuntime -match '(?i)[\\/]Microsoft[\\/]WindowsApps[\\/]') {
    throw 'PythonExe must be the resolved interpreter executable, not an app-execution alias'
}
$repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
if (-not (Test-Path -LiteralPath $repository -PathType Container)) { throw 'RepositoryRoot must be a directory' }
$state = (Resolve-Path -LiteralPath $AuthorizedStateRoot).Path
if (-not (Test-Path -LiteralPath $state -PathType Container)) { throw 'AuthorizedStateRoot must be a directory' }
Assert-NoReparseComponents $repository 'RepositoryRoot'
Assert-NoReparseComponents $state 'AuthorizedStateRoot'
if (-not (Test-Within $repository $state) -or $repository.Equals($state, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'RepositoryRoot must be a strict descendant of AuthorizedStateRoot'
}
$testOutput = [System.IO.Path]::GetFullPath($TestOutputRoot)
if (-not (Test-Within $testOutput $state) -or $testOutput.Equals($state, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'TestOutputRoot must be a strict descendant of AuthorizedStateRoot'
}
if (-not (Test-Path -LiteralPath $testOutput)) { New-Item -ItemType Directory -Path $testOutput | Out-Null }
if (-not (Test-Path -LiteralPath $testOutput -PathType Container)) { throw 'TestOutputRoot must be a directory' }
Assert-NoReparseComponents $testOutput 'TestOutputRoot'
$redirectedProfileRoot = Join-Path $testOutput 'profile'
$redirectedLocalAppData = Join-Path $redirectedProfileRoot 'local-app-data'
$redirectedRoamingAppData = Join-Path $redirectedProfileRoot 'roaming-app-data'
$redirectedUserProfile = Join-Path $redirectedProfileRoot 'user-profile'
New-Item -ItemType Directory -Force -Path $redirectedLocalAppData, $redirectedRoamingAppData, $redirectedUserProfile | Out-Null

$env:TEMP = $testOutput
$env:TMP = $testOutput
$env:LOCALAPPDATA = $redirectedLocalAppData
$env:APPDATA = $redirectedRoamingAppData
$env:USERPROFILE = $redirectedUserProfile
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONNOUSERSITE = '1'
$env:LIMIT_HALO_TEST_ROOT = $testOutput
$env:LIMIT_HALO_FIXTURE_ONLY = '1'
$env:PIP_NO_INDEX = '1'
$env:HTTP_PROXY = 'http://127.0.0.1:9'
$env:HTTPS_PROXY = 'http://127.0.0.1:9'
$env:ALL_PROXY = 'http://127.0.0.1:9'
$env:NO_PROXY = 'localhost,127.0.0.1'

$policy = Join-Path $repository 'packaging\scripts\release_policy.py'
& $pythonRuntime -I -B $policy --repository-root $repository --json
if ($LASTEXITCODE -ne 0) { throw 'Release policy validation failed' }

$parseErrors = @()
Get-ChildItem -LiteralPath $repository -Recurse -File -Filter '*.ps1' | ForEach-Object {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors) | Out-Null
    foreach ($error in @($errors)) { $parseErrors += ($_.FullName + ': ' + $error.Message) }
}
if ($parseErrors.Count -ne 0) { throw ('PowerShell parser failures: ' + ($parseErrors -join '; ')) }

& $pythonRuntime -I -B -m unittest discover -s (Join-Path $repository 'tests\release') -p 'test_*.py'
if ($LASTEXITCODE -ne 0) { throw 'Release fixture/mutant tests failed' }

& (Join-Path $repository 'packaging\scripts\Test-OwnedCleanup.ps1') `
    -RepositoryRoot $repository `
    -TestOutputRoot $testOutput `
    -AuthorizedStateRoot $state `
    -PythonExe $pythonRuntime | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Manifest-owned cleanup fixtures failed' }

$shortFixtureParent = Join-Path $state 't'
New-Item -ItemType Directory -Path $shortFixtureParent -Force | Out-Null
Assert-NoReparseComponents $shortFixtureParent 'ShortFixtureParent'
$shortFixture = Join-Path $shortFixtureParent ('os-' + [Guid]::NewGuid().ToString('N').Substring(0, 12))
if (-not (Test-Within $shortFixture $shortFixtureParent)) {
    throw 'Short shortcut fixture escaped its authorized parent'
}
try {
    & (Join-Path $repository 'packaging\scripts\Test-OwnedShortcuts.ps1') `
        -RepositoryRoot $repository `
        -AuthorizedStateRoot $state `
        -TestOutputRoot $shortFixture | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Owned shortcut fixtures failed' }
}
finally {
    if (Test-Path -LiteralPath $shortFixture) {
        $resolvedShortFixture = (Resolve-Path -LiteralPath $shortFixture).Path
        if (-not (Test-Within $resolvedShortFixture $shortFixtureParent) -or
            [System.IO.Path]::GetFileName($resolvedShortFixture) -cnotmatch '^os-[0-9a-f]{12}$') {
            throw 'Refusing to remove an unexpected shortcut fixture path'
        }
        Remove-Item -LiteralPath $resolvedShortFixture -Recurse -Force
    }
}

[pscustomobject]@{
    status = 'PASS'
    route = 'static-parser-redirected-fixture'
    hostInstallPerformed = $false
    installerExecutionPerformed = $false
    networkRequired = $false
}
