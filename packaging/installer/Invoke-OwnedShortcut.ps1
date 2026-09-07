[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('Ensure', 'Remove', 'Probe')][string]$Action,
    [Parameter(Mandatory = $true)][ValidateSet('Hud', 'Configure', 'Startup')][string]$Kind,
    [Parameter(Mandatory = $true)][string]$ProductRoot,
    [Parameter(Mandatory = $true)][string]$AppDataRoot,
    [Parameter()][ValidatePattern('^[0-9a-f]{64}$')][string]$CandidateId
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-AbsoluteDrivePath([string]$PathValue) {
    return [System.IO.Path]::IsPathRooted($PathValue) -and $PathValue -cmatch '^[A-Za-z]:[\\/]'
}

function Assert-NoReparseComponents([string]$PathValue, [string]$Label) {
    $cursor = [System.IO.Path]::GetFullPath($PathValue)
    while (-not (Test-Path -LiteralPath $cursor)) {
        $parent = [System.IO.Path]::GetDirectoryName($cursor)
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -ceq $cursor) { throw "$Label has no existing local ancestor" }
        $cursor = $parent
    }
    $item = Get-Item -Force -LiteralPath $cursor
    while ($null -ne $item) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label contains a reparse component" }
        if ($item -is [System.IO.FileInfo]) { $item = $item.Directory }
        elseif ($item -is [System.IO.DirectoryInfo]) { $item = $item.Parent }
        else { throw "$Label contains an unsupported filesystem object" }
    }
}

function Resolve-SafeDirectory([string]$PathValue, [string]$Label) {
    if (-not (Test-AbsoluteDrivePath $PathValue)) { throw "$Label must be an absolute local-drive path" }
    $resolved = (Resolve-Path -LiteralPath $PathValue).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) { throw "$Label must be an existing directory" }
    Assert-NoReparseComponents $resolved $Label
    return [System.IO.Path]::GetFullPath($resolved).TrimEnd('\')
}

function Same-Path([string]$Left, [string]$Right) {
    try {
        $leftFull = [System.IO.Path]::GetFullPath($Left).TrimEnd('\')
        $rightFull = [System.IO.Path]::GetFullPath($Right).TrimEnd('\')
        return $leftFull.Equals($rightFull, [System.StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Read-Link([string]$PathValue) {
    if (-not (Test-Path -LiteralPath $PathValue -PathType Leaf)) { return $null }
    if ((Get-Item -Force -LiteralPath $PathValue).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $null }
    $shell = $null
    $link = $null
    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($PathValue)
        return [pscustomobject]@{
            target = [string]$link.TargetPath
            arguments = [string]$link.Arguments
            workingDirectory = [string]$link.WorkingDirectory
        }
    } catch {
        return $null
    } finally {
        if ($null -ne $link) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) }
        if ($null -ne $shell) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
    }
}

function Test-OwnedLink($Link, [string]$ExpectedArguments, [string]$Product) {
    if ($null -eq $Link -or [string]::IsNullOrWhiteSpace([string]$Link.target)) { return $false }
    try { $target = [System.IO.Path]::GetFullPath([string]$Link.target) } catch { return $false }
    $prefix = $Product.TrimEnd('\') + '\'
    if (-not $target.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { return $false }
    $relative = $target.Substring($prefix.Length).Split('\')
    if ($relative.Count -ne 3 -or
        -not $relative[0].Equals('versions', [System.StringComparison]::OrdinalIgnoreCase) -or
        $relative[1] -cnotmatch '^[0-9a-f]{64}$' -or
        -not $relative[2].Equals('AILimitsWidget.exe', [System.StringComparison]::OrdinalIgnoreCase)) { return $false }
    if ([string]$Link.arguments -cne $ExpectedArguments) { return $false }
    return Same-Path ([string]$Link.workingDirectory) ([System.IO.Path]::GetDirectoryName($target))
}

function Write-Result([string]$Status, [bool]$Active, [bool]$Changed) {
    [pscustomobject]@{
        schemaVersion = '1.0.0'
        status = $Status
        action = $Action
        kind = $Kind
        active = $Active
        changed = $Changed
    } | ConvertTo-Json -Compress
}

$product = Resolve-SafeDirectory $ProductRoot 'ProductRoot'
$appData = Resolve-SafeDirectory $AppDataRoot 'AppDataRoot'
if (-not ([System.IO.Path]::GetFileName($product).Equals('AILimitsWidget', [System.StringComparison]::OrdinalIgnoreCase))) {
    throw 'ProductRoot must be the exact AILimitsWidget directory'
}

$programs = Join-Path $appData 'Microsoft\Windows\Start Menu\Programs'
$shortcut = switch ($Kind) {
    'Hud' { Join-Path $programs 'LimitHalo.lnk' }
    'Configure' { Join-Path $programs 'Configure LimitHalo.lnk' }
    'Startup' { Join-Path (Join-Path $programs 'Startup') 'LimitHalo.lnk' }
}
$arguments = if ($Kind -ceq 'Configure') {
    '--configure'
} elseif ($Kind -ceq 'Startup') {
    '--startup'
} else {
    ''
}
Assert-NoReparseComponents $shortcut 'ShortcutPath'

$existing = Read-Link $shortcut
$existingOwned = (Test-OwnedLink $existing $arguments $product) -or
    (($Kind -ceq 'Startup') -and (Test-OwnedLink $existing '' $product))

if ($Action -ceq 'Remove') {
    if (-not (Test-Path -LiteralPath $shortcut)) {
        Write-Result 'ABSENT' $false $false
        exit 0
    }
    if (-not $existingOwned) {
        Write-Result 'PRESERVED_FOREIGN' $false $false
        exit 3
    }
    Remove-Item -LiteralPath $shortcut
    Write-Result 'REMOVED_OWNED' $false $true
    exit 0
}

if ([string]::IsNullOrWhiteSpace($CandidateId)) { throw 'CandidateId is required for Ensure and Probe' }
$candidateRoot = Join-Path (Join-Path $product 'versions') $CandidateId
$target = Join-Path $candidateRoot 'AILimitsWidget.exe'
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw 'Owned shortcut target is missing' }
Assert-NoReparseComponents $target 'ShortcutTarget'
$current = (Test-OwnedLink $existing $arguments $product) -and (Same-Path ([string]$existing.target) $target)

if ($Action -ceq 'Probe') {
    if ($current) {
        Write-Result 'ACTIVE' $true $false
        exit 0
    }
    Write-Result 'INACTIVE_OR_FOREIGN' $false $false
    exit 3
}

if ((Test-Path -LiteralPath $shortcut) -and -not $existingOwned) {
    Write-Result 'PRESERVED_FOREIGN' $false $false
    exit 3
}

$shortcutDirectory = [System.IO.Path]::GetDirectoryName($shortcut)
if (-not (Test-Path -LiteralPath $shortcutDirectory)) { New-Item -ItemType Directory -Path $shortcutDirectory | Out-Null }
Assert-NoReparseComponents $shortcutDirectory 'ShortcutDirectory'
$temporary = Join-Path $shortcutDirectory ('.LimitHalo.' + $Kind + '.' + $PID + '.tmp.lnk')
$backup = Join-Path $shortcutDirectory ('.LimitHalo.' + $Kind + '.' + $PID + '.backup.lnk')
if (Test-Path -LiteralPath $temporary) { throw 'Shortcut temporary path already exists' }
if (Test-Path -LiteralPath $backup) { throw 'Shortcut backup path already exists' }
$shell = $null
$link = $null
try {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($temporary)
    $link.TargetPath = $target
    $link.Arguments = $arguments
    $link.WorkingDirectory = $candidateRoot
    $link.IconLocation = $target + ',0'
    $link.Description = if ($Kind -ceq 'Configure') {
        'Configure LimitHalo'
    } elseif ($Kind -ceq 'Startup') {
        'LimitHalo owned startup shortcut v1'
    } else {
        'LimitHalo subscription-limit HUD'
    }
    $link.Save()
} finally {
    if ($null -ne $link) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) }
    if ($null -ne $shell) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
}

try {
    $staged = Read-Link $temporary
    if (-not (Test-OwnedLink $staged $arguments $product) -or -not (Same-Path ([string]$staged.target) $target)) {
        throw 'Staged shortcut triplet validation failed'
    }
    if (Test-Path -LiteralPath $shortcut) {
        [System.IO.File]::Replace($temporary, $shortcut, $backup)
    } else {
        [System.IO.File]::Move($temporary, $shortcut)
    }
} finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
}

$activated = Read-Link $shortcut
if (-not (Test-OwnedLink $activated $arguments $product) -or -not (Same-Path ([string]$activated.target) $target)) {
    throw 'Activated shortcut triplet validation failed'
}
if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup }
Write-Result 'ACTIVE' $true (-not $current)
exit 0
