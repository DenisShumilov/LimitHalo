[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot,
    [Parameter(Mandatory = $true)][string]$WheelhouseRoot,
    [Parameter(Mandatory = $true)][string]$TestOutputRoot,
    [Parameter(Mandatory = $true)][string]$AuthorizedStateRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-AbsoluteDrivePath([string]$PathValue) {
    return [System.IO.Path]::IsPathRooted($PathValue) -and $PathValue -cmatch '^[A-Za-z]:[\\/]'
}

function Resolve-Directory([string]$PathValue, [string]$Label) {
    if (-not (Test-AbsoluteDrivePath $PathValue)) { throw "$Label must be an absolute local drive path" }
    $resolved = (Resolve-Path -LiteralPath $PathValue).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) { throw "$Label must be a directory" }
    return [System.IO.Path]::GetFullPath($resolved)
}

function Test-Within([string]$Candidate, [string]$Parent) {
    $child = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $root = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\')
    return $child.Equals($root, [System.StringComparison]::OrdinalIgnoreCase) -or
        $child.StartsWith($root + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparseComponents([string]$PathValue, [string]$Label) {
    $item = Get-Item -LiteralPath $PathValue -Force
    while ($null -ne $item) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label contains a reparse component" }
        if ($item -is [System.IO.DirectoryInfo]) {
            $item = $item.Parent
        } elseif ($item -is [System.IO.FileInfo]) {
            $item = $item.Directory
        } else {
            throw "$Label contains an unsupported filesystem object"
        }
    }
}

$state = Resolve-Directory $AuthorizedStateRoot 'AuthorizedStateRoot'
$repository = Resolve-Directory $RepositoryRoot 'RepositoryRoot'
$wheelhouse = Resolve-Directory $WheelhouseRoot 'WheelhouseRoot'
$testOutput = Resolve-Directory $TestOutputRoot 'TestOutputRoot'
foreach ($entry in @(
    @($repository, 'RepositoryRoot'),
    @($wheelhouse, 'WheelhouseRoot'),
    @($testOutput, 'TestOutputRoot')
)) {
    if (-not (Test-Within $entry[0] $state) -or $entry[0].Equals($state, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ($entry[1] + ' must be a strict descendant of AuthorizedStateRoot')
    }
}
Assert-NoReparseComponents $state 'AuthorizedStateRoot'
Assert-NoReparseComponents $repository 'RepositoryRoot'
Assert-NoReparseComponents $wheelhouse 'WheelhouseRoot'
Assert-NoReparseComponents $testOutput 'TestOutputRoot'
$lock = Join-Path $repository 'requirements\python-build.lock.json'
$validator = Join-Path $repository 'packaging\scripts\Assert-PythonBuildLock.ps1'

& $validator -LockPath $lock -WheelhouseRoot $wheelhouse | Out-Null

$mutant = Join-Path $testOutput ('wheel-hash-mutant-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $mutant | Out-Null
foreach ($file in @(Get-ChildItem -LiteralPath $wheelhouse -File -Force)) {
    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $mutant $file.Name)
}
$target = Join-Path $mutant 'altgraph-0.17.5-py2.py3-none-any.whl'
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw 'Expected wheel mutant target is missing' }
$bytes = [System.IO.File]::ReadAllBytes($target)
if ($bytes.Length -lt 1) { throw 'Wheel mutant target is empty' }
$bytes[0] = $bytes[0] -bxor 1
[System.IO.File]::WriteAllBytes($target, $bytes)
$detected = $false
try { & $validator -LockPath $lock -WheelhouseRoot $mutant | Out-Null } catch { $detected = $true }
if (-not $detected) { throw 'Wheel hash-substitution mutant was not detected' }

[pscustomobject]@{
    status = 'PASS'
    positiveWheelhouse = 'PASS'
    wheelCount = @(Get-ChildItem -LiteralPath $wheelhouse -File).Count
    hashSubstitutionMutant = 'DETECTED'
}
