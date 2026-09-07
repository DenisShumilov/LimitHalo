[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$LockPath,
    [Parameter(Mandatory = $true)][string]$WheelhouseRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-AbsoluteFile([string]$PathValue, [string]$Label) {
    if (-not [System.IO.Path]::IsPathFullyQualified($PathValue)) { throw "$Label must be absolute" }
    $resolved = (Resolve-Path -LiteralPath $PathValue).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { throw "$Label must be a file" }
    return $resolved
}

function Resolve-AbsoluteDirectory([string]$PathValue, [string]$Label) {
    if (-not [System.IO.Path]::IsPathFullyQualified($PathValue)) { throw "$Label must be absolute" }
    $resolved = (Resolve-Path -LiteralPath $PathValue).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) { throw "$Label must be a directory" }
    return $resolved
}

$lockFile = Resolve-AbsoluteFile $LockPath 'LockPath'
$wheelhouse = Resolve-AbsoluteDirectory $WheelhouseRoot 'WheelhouseRoot'
$expectedLockHash = '7fc014ce7148891c323f6244a0c0a09c762dc38d712176f8b1c407beb932b7b0'
$actualLockHash = (Get-FileHash -LiteralPath $lockFile -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualLockHash -cne $expectedLockHash) { throw 'Python build lock identity mismatch' }

$lock = Get-Content -LiteralPath $lockFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($lock.schemaVersion -cne '1.0.0' -or $lock.python.version -cne '3.12.13' -or $lock.python.architecture -cne 'x64' -or $lock.pyinstaller.version -cne '6.21.0') {
    throw 'Unsupported Python build lock content'
}

$expected = [System.Collections.Generic.Dictionary[string,object]]::new([System.StringComparer]::Ordinal)
foreach ($entry in @($lock.wheels)) {
    $name = [string]$entry.name
    if ([System.IO.Path]::GetFileName($name) -cne $name -or $name -notmatch '\.whl$' -or $expected.ContainsKey($name)) {
        throw 'Invalid or duplicate wheel name in build lock'
    }
    if ([string]$entry.sha256 -notmatch '^[0-9a-f]{64}$' -or [int64]$entry.size -lt 1) {
        throw 'Invalid wheel identity in build lock'
    }
    $expected.Add($name, $entry)
}

$actualFiles = @(Get-ChildItem -LiteralPath $wheelhouse -File -Force)
if ($actualFiles.Count -ne $expected.Count) { throw 'Wheelhouse file set differs from the exact build lock' }
foreach ($file in $actualFiles) {
    if (-not $expected.ContainsKey($file.Name)) { throw "Unexpected wheel: $($file.Name)" }
    $entry = $expected[$file.Name]
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($file.Length -ne [int64]$entry.size -or $hash -cne [string]$entry.sha256) {
        throw "Wheel identity mismatch: $($file.Name)"
    }
}

[pscustomobject]@{
    status = 'PASS'
    lockSha256 = $actualLockHash
    wheelCount = $expected.Count
}
