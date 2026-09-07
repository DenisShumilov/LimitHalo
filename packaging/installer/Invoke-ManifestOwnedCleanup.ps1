[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AllowedParentRoot,
    [Parameter(Mandatory = $true)][string]$ProductRoot,
    [Parameter()][ValidatePattern('^[0-9a-f]{64}$')][string]$ActiveCandidate,
    [Parameter()][ValidatePattern('^[0-9a-f]{64}$')][string]$RollbackCandidate,
    [Parameter()][switch]$UseCandidateMarkers,
    [Parameter()][ValidatePattern('^[0-9a-f]{64}$')][string]$ValidateCandidate,
    [Parameter()][ValidatePattern('^[0-9a-f]{64}$')][string]$DeleteCandidate,
    [Parameter()][switch]$DeleteAll
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-AbsoluteDirectory([string]$PathValue, [string]$Label) {
    $pathRoot = [System.IO.Path]::GetPathRoot($PathValue)
    if (-not [System.IO.Path]::IsPathRooted($PathValue) -or $pathRoot -notmatch '^[A-Za-z]:\\$') { throw "$Label must be an absolute local-drive path" }
    $resolved = (Resolve-Path -LiteralPath $PathValue).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) { throw "$Label must be an existing directory" }
    if ((Get-Item -LiteralPath $resolved).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw "$Label cannot be a reparse point" }
    return [System.IO.Path]::GetFullPath($resolved).TrimEnd('\')
}

function Get-Sha256([string]$PathValue) {
    $stream = [System.IO.File]::OpenRead($PathValue)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Get-CanonicalCandidateId($Entries) {
    $byPath = [System.Collections.Generic.Dictionary[string,object]]::new([System.StringComparer]::Ordinal)
    foreach ($entry in @($Entries)) {
        $names = @($entry.PSObject.Properties.Name)
        if ($names.Count -ne 3 -or -not ($names -ccontains 'path') -or -not ($names -ccontains 'size') -or -not ($names -ccontains 'sha256')) { throw 'Manifest entry schema is invalid' }
        $relative = [string]$entry.path
        $segments = @($relative.Split('/'))
        if ([string]::IsNullOrWhiteSpace($relative) -or $relative.Contains('\') -or $relative.StartsWith('/') -or @($segments | Where-Object { $_ -ceq '' -or $_ -ceq '.' -or $_ -ceq '..' }).Count -ne 0) { throw 'Manifest entry path is unsafe' }
        if ([string]$entry.sha256 -cnotmatch '^[0-9a-f]{64}$' -or -not ($entry.size -is [int] -or $entry.size -is [long]) -or [int64]$entry.size -lt 0) { throw 'Manifest entry identity is invalid' }
        if ($byPath.ContainsKey($relative)) { throw 'Manifest contains a duplicate ordinal path' }
        $byPath.Add($relative, $entry)
    }
    $paths = [string[]]@($byPath.Keys)
    [System.Array]::Sort($paths, [System.StringComparer]::Ordinal)
    $lines = @($paths | ForEach-Object {
        $entry = $byPath[$_]
        ([string]$entry.path) + "`t" + ([int64]$entry.size) + "`t" + ([string]$entry.sha256)
    })
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes([string]::Join("`n", $lines))))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Test-OwnedVersion([System.IO.DirectoryInfo]$Directory) {
    if ($Directory.Name -cnotmatch '^[0-9a-f]{64}$' -or ($Directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { return $false }
    $manifestPath = Join-Path $Directory.FullName 'package-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or ((Get-Item -LiteralPath $manifestPath).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { return $false }
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $false }
    $required = @('schemaVersion', 'kind', 'internalProductId', 'displayName', 'displayNameStatus', 'version', 'architecture', 'candidateId', 'sourceIdentity', 'signatureStatus', 'brokerSha256', 'files')
    $names = @($manifest.PSObject.Properties.Name)
    if ($names.Count -ne $required.Count -or @($required | Where-Object { -not ($names -ccontains $_) }).Count -ne 0) { return $false }
    if ($manifest.schemaVersion -cne '1.0.0' -or $manifest.kind -cne 'installed-payload' -or $manifest.internalProductId -cne 'AILimitsWidget' -or $manifest.candidateId -cne $Directory.Name) { return $false }
    try { if ((Get-CanonicalCandidateId $manifest.files) -cne $Directory.Name) { return $false } } catch { return $false }
    $rootPrefix = [System.IO.Path]::GetFullPath($Directory.FullName).TrimEnd('\') + '\'
    $declared = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in @($manifest.files)) {
        $relative = [string]$entry.path
        if (-not $declared.Add($relative)) { return $false }
        $target = [System.IO.Path]::GetFullPath((Join-Path $Directory.FullName $relative))
        if (-not $target.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $target -PathType Leaf)) { return $false }
        $item = Get-Item -LiteralPath $target
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or $item.Length -ne [int64]$entry.size) { return $false }
        if ((Get-Sha256 $target) -cne [string]$entry.sha256) { return $false }
    }
    $actual = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($item in @(Get-ChildItem -LiteralPath $Directory.FullName -Recurse -File -Force)) {
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $false }
        $relative = $item.FullName.Substring($Directory.FullName.Length).TrimStart('\').Replace('\', '/')
        if ($relative -ceq 'package-manifest.json') { continue }
        if (-not $actual.Add($relative)) { return $false }
    }
    return $actual.SetEquals($declared)
}

$allowedParent = Resolve-AbsoluteDirectory $AllowedParentRoot 'AllowedParentRoot'
$product = Resolve-AbsoluteDirectory $ProductRoot 'ProductRoot'
$expectedProduct = Join-Path $allowedParent 'AILimitsWidget'
if (-not $product.Equals([System.IO.Path]::GetFullPath($expectedProduct).TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)) { throw 'ProductRoot is not the exact AILimitsWidget child of AllowedParentRoot' }

if ($ValidateCandidate) {
    $candidateRoot = Join-Path (Join-Path $product 'versions') $ValidateCandidate
    if (-not (Test-Path -LiteralPath $candidateRoot -PathType Container)) { throw 'Candidate validation root is missing' }
    $candidateDirectory = Get-Item -LiteralPath $candidateRoot
    if (-not (Test-OwnedVersion $candidateDirectory)) { throw 'Installed candidate failed manifest ownership validation' }
    [pscustomobject]@{ status = 'PASS'; candidateId = $ValidateCandidate }
    return
}

if ($DeleteCandidate) {
    $candidateRoot = Join-Path (Join-Path $product 'versions') $DeleteCandidate
    if (-not (Test-Path -LiteralPath $candidateRoot -PathType Container)) {
        [pscustomobject]@{ status = 'PASS'; deleted = @(); preserved = @() }
        return
    }
    $candidateDirectory = Get-Item -LiteralPath $candidateRoot
    if (-not (Test-OwnedVersion $candidateDirectory)) { throw 'DeleteCandidate refuses an unowned or tampered candidate' }
    $versionsRoot = [System.IO.Path]::GetFullPath((Join-Path $product 'versions')).TrimEnd('\') + '\'
    $target = [System.IO.Path]::GetFullPath($candidateDirectory.FullName)
    if (-not $target.StartsWith($versionsRoot, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'DeleteCandidate target escapes versions root' }
    Remove-Item -LiteralPath $target -Recurse -Force
    [pscustomobject]@{ status = 'PASS'; deleted = @($DeleteCandidate); preserved = @() }
    return
}

if ($UseCandidateMarkers) {
    $activeMarker = Join-Path $product 'active-candidate.txt'
    $rollbackMarker = Join-Path $product 'rollback-candidate.txt'
    if (Test-Path -LiteralPath $activeMarker -PathType Leaf) { $ActiveCandidate = (Get-Content -LiteralPath $activeMarker -Raw -Encoding UTF8).Trim() }
    if (Test-Path -LiteralPath $rollbackMarker -PathType Leaf) { $RollbackCandidate = (Get-Content -LiteralPath $rollbackMarker -Raw -Encoding UTF8).Trim() }
    if ($ActiveCandidate -and $ActiveCandidate -cnotmatch '^[0-9a-f]{64}$') { throw 'Active candidate marker is invalid' }
    if ($RollbackCandidate -and $RollbackCandidate -cnotmatch '^[0-9a-f]{64}$') { throw 'Rollback candidate marker is invalid' }
}

$versions = Join-Path $product 'versions'
if (-not (Test-Path -LiteralPath $versions -PathType Container)) { return }
if ((Get-Item -LiteralPath $versions).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Versions root cannot be a reparse point' }
$versionsPrefix = [System.IO.Path]::GetFullPath($versions).TrimEnd('\') + '\'
$deleted = @()
$preserved = @()
foreach ($directory in @(Get-ChildItem -LiteralPath $versions -Directory -Force)) {
    $owned = Test-OwnedVersion $directory
    $retain = -not $DeleteAll -and ($directory.Name -ceq $ActiveCandidate -or $directory.Name -ceq $RollbackCandidate)
    if ($owned -and -not $retain) {
        $target = [System.IO.Path]::GetFullPath($directory.FullName)
        if (-not $target.StartsWith($versionsPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup target escapes versions root' }
        Remove-Item -LiteralPath $target -Recurse -Force
        $deleted += $directory.Name
    } else {
        $preserved += $directory.Name
    }
}

[pscustomobject]@{ status = 'PASS'; deleted = @($deleted); preserved = @($preserved) }
