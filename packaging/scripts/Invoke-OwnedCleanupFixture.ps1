[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProductRoot,
    [Parameter(Mandatory = $true)][string]$AuthorizedStateRoot,
    [Parameter()][ValidatePattern('^[0-9a-f]{64}$')][string]$ActiveCandidate,
    [Parameter()][ValidatePattern('^[0-9a-f]{64}$')][string]$RollbackCandidate,
    [Parameter()][switch]$DeleteAll
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

if (-not (Test-AbsoluteDrivePath $ProductRoot) -or -not (Test-AbsoluteDrivePath $AuthorizedStateRoot)) { throw 'ProductRoot and AuthorizedStateRoot must be absolute local drive paths' }
$product = (Resolve-Path -LiteralPath $ProductRoot).Path
$state = (Resolve-Path -LiteralPath $AuthorizedStateRoot).Path
if (-not (Test-Path -LiteralPath $product -PathType Container)) { throw 'ProductRoot must be a directory' }
if (-not (Test-Path -LiteralPath $state -PathType Container)) { throw 'AuthorizedStateRoot must be a directory' }
if (-not (Test-Within $product $state) -or $product.Equals($state, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'ProductRoot must be a strict descendant of AuthorizedStateRoot' }
Assert-NoReparseComponents $state 'AuthorizedStateRoot'
Assert-NoReparseComponents $product 'ProductRoot'
$marker = Join-Path $product '.limit-halo-cleanup-fixture'
if (-not (Test-Path -LiteralPath $marker -PathType Leaf) -or (Get-Content -LiteralPath $marker -Raw -Encoding UTF8).Trim() -cne 'AILimitsWidget cleanup fixture v1') {
    throw 'Cleanup is allowed only below an exact fixture ownership marker'
}
if ((Get-Item -LiteralPath $product).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Fixture product root cannot be a reparse point' }
$versions = Join-Path $product 'versions'
if (-not (Test-Path -LiteralPath $versions -PathType Container)) {
    [pscustomobject]@{ status = 'PASS'; deleted = @(); preserved = @() }
    return
}
if ((Get-Item -LiteralPath $versions).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Fixture versions root cannot be a reparse point' }

function Test-OwnedVersion([System.IO.DirectoryInfo]$Directory) {
    if ($Directory.Name -cnotmatch '^[0-9a-f]{64}$' -or ($Directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { return $false }
    $manifestPath = Join-Path $Directory.FullName 'package-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or ((Get-Item -LiteralPath $manifestPath).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { return $false }
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $false }
    $names = @($manifest.PSObject.Properties.Name)
    $required = @('schemaVersion', 'kind', 'internalProductId', 'displayName', 'displayNameStatus', 'version', 'architecture', 'candidateId', 'sourceIdentity', 'signatureStatus', 'brokerSha256', 'files')
    if ($names.Count -ne $required.Count -or @($required | Where-Object { -not ($names -ccontains $_) }).Count -ne 0) { return $false }
    if ($manifest.schemaVersion -cne '1.0.0' -or $manifest.kind -cne 'installed-payload' -or $manifest.internalProductId -cne 'AILimitsWidget' -or $manifest.candidateId -cne $Directory.Name) { return $false }
    $rootPrefix = [System.IO.Path]::GetFullPath($Directory.FullName).TrimEnd('\') + '\'
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in @($manifest.files)) {
        $relative = [string]$entry.path
        if ([string]::IsNullOrWhiteSpace($relative) -or [System.IO.Path]::IsPathRooted($relative) -or $relative.Contains('\') -or $relative -match '(^|/)\.\.(/|$)' -or -not $seen.Add($relative)) { return $false }
        $target = [System.IO.Path]::GetFullPath((Join-Path $Directory.FullName $relative))
        if (-not $target.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $target -PathType Leaf)) { return $false }
        $item = Get-Item -LiteralPath $target
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or $item.Length -ne [int64]$entry.size) { return $false }
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$entry.sha256) { return $false }
    }
    return $true
}

$deleted = @()
$preserved = @()
foreach ($directory in @(Get-ChildItem -LiteralPath $versions -Directory -Force)) {
    $owned = Test-OwnedVersion $directory
    $retained = -not $DeleteAll -and ($directory.Name -ceq $ActiveCandidate -or $directory.Name -ceq $RollbackCandidate)
    if ($owned -and -not $retained) {
        $full = [System.IO.Path]::GetFullPath($directory.FullName)
        $prefix = [System.IO.Path]::GetFullPath($versions).TrimEnd('\') + '\'
        if (-not $full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup target escapes versions root' }
        Remove-Item -LiteralPath $full -Recurse -Force
        $deleted += $directory.Name
    } else {
        $preserved += $directory.Name
    }
}

[pscustomobject]@{ status = 'PASS'; deleted = @($deleted); preserved = @($preserved) }
