[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot,
    [Parameter(Mandatory = $true)][string]$TestOutputRoot,
    [Parameter(Mandatory = $true)][string]$AuthorizedStateRoot,
    [Parameter()][string]$PythonExe = 'python'
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

if (-not (Test-AbsoluteDrivePath $RepositoryRoot) -or -not (Test-AbsoluteDrivePath $TestOutputRoot) -or -not (Test-AbsoluteDrivePath $AuthorizedStateRoot)) { throw 'Fixture roots must be absolute local drive paths' }
$repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$testOutput = (Resolve-Path -LiteralPath $TestOutputRoot).Path
$state = (Resolve-Path -LiteralPath $AuthorizedStateRoot).Path
foreach ($entry in @(@($state, 'AuthorizedStateRoot'), @($repository, 'RepositoryRoot'), @($testOutput, 'TestOutputRoot'))) {
    if (-not (Test-Path -LiteralPath $entry[0] -PathType Container)) { throw ($entry[1] + ' must be a directory') }
    Assert-NoReparseComponents $entry[0] $entry[1]
}
foreach ($entry in @(@($repository, 'RepositoryRoot'), @($testOutput, 'TestOutputRoot'))) {
    if (-not (Test-Within $entry[0] $state) -or $entry[0].Equals($state, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ($entry[1] + ' must be a strict descendant of AuthorizedStateRoot')
    }
}
$fixture = Join-Path $testOutput 'owned-cleanup-fixture'
if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
New-Item -ItemType Directory -Path $fixture | Out-Null
$marker = Join-Path $fixture '.limit-halo-owned-cleanup-test'
[System.IO.File]::WriteAllText($marker, "AILimitsWidget owned cleanup test v1`n", [System.Text.UTF8Encoding]::new($false))
$programs = Join-Path $fixture 'Programs'
$product = Join-Path $programs 'AILimitsWidget'
$versions = Join-Path $product 'versions'
New-Item -ItemType Directory -Path $versions -Force | Out-Null
$tool = Join-Path $repository 'packaging\scripts\release_tools.py'
$productJson = Join-Path $repository 'product.json'
$cleanup = Join-Path $repository 'packaging\installer\Invoke-ManifestOwnedCleanup.ps1'

function New-OwnedVersion([string]$Label) {
    $stage = Join-Path $fixture ('stage-' + $Label)
    New-Item -ItemType Directory -Path $stage | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $stage 'AILimitsWidget.exe'), ('widget-' + $Label), [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText((Join-Path $stage 'ClaudeUsageBroker.exe'), ('broker-' + $Label), [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText((Join-Path $stage 'README.md'), "fixture only`n", [System.Text.UTF8Encoding]::new($false))
    $manifest = Join-Path $stage 'package-manifest.json'
    $candidateText = & $PythonExe -I -B $tool payload-manifest --root $stage --output $manifest --product $productJson --source-identity ('cleanup-' + $Label)
    if ($LASTEXITCODE -ne 0) { throw "Fixture manifest generation failed: $Label" }
    $candidate = ([string]$candidateText).Trim()
    if ($candidate -cnotmatch '^[0-9a-f]{64}$') { throw 'Fixture candidate identity is invalid' }
    $destination = Join-Path $versions $candidate
    Move-Item -LiteralPath $stage -Destination $destination
    return [pscustomobject]@{ id = $candidate; root = $destination }
}

$active = New-OwnedVersion 'active'
$rollback = New-OwnedVersion 'rollback'
$stale = New-OwnedVersion 'stale'
$tampered = New-OwnedVersion 'tampered'
[System.IO.File]::AppendAllText((Join-Path $tampered.root 'README.md'), "mutation`n", [System.Text.UTF8Encoding]::new($false))
$unknown = Join-Path $versions 'unknown-user-directory'
New-Item -ItemType Directory -Path $unknown | Out-Null
[System.IO.File]::WriteAllText((Join-Path $unknown 'keep.txt'), "unknown`n", [System.Text.UTF8Encoding]::new($false))

& $cleanup -AllowedParentRoot $programs -ProductRoot $product -ValidateCandidate $active.id | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Positive installed-candidate validation failed' }
$tamperedDetected = $false
try { & $cleanup -AllowedParentRoot $programs -ProductRoot $product -ValidateCandidate $tampered.id | Out-Null } catch { $tamperedDetected = $true }
if (-not $tamperedDetected) { throw 'Hash-substitution candidate mutant was not rejected' }

$failedHealth = New-OwnedVersion 'failed-health'
& $cleanup -AllowedParentRoot $programs -ProductRoot $product -DeleteCandidate $failedHealth.id | Out-Null
if (Test-Path -LiteralPath $failedHealth.root) { throw 'Failed-health candidate was not removed before activation' }
$tamperedDeleteDetected = $false
try { & $cleanup -AllowedParentRoot $programs -ProductRoot $product -DeleteCandidate $tampered.id | Out-Null } catch { $tamperedDeleteDetected = $true }
if (-not $tamperedDeleteDetected) { throw 'DeleteCandidate accepted a tampered version' }

& $cleanup -AllowedParentRoot $programs -ProductRoot $product -ActiveCandidate $active.id -RollbackCandidate $rollback.id | Out-Null
if (-not (Test-Path -LiteralPath $active.root -PathType Container) -or -not (Test-Path -LiteralPath $rollback.root -PathType Container)) { throw 'Cleanup removed an active or rollback version' }
if (Test-Path -LiteralPath $stale.root) { throw 'Cleanup retained a stale manifest-owned version' }
if (-not (Test-Path -LiteralPath $tampered.root -PathType Container) -or -not (Test-Path -LiteralPath $unknown -PathType Container)) { throw 'Cleanup removed tampered or unknown paths' }

& $cleanup -AllowedParentRoot $programs -ProductRoot $product -DeleteAll | Out-Null
if ((Test-Path -LiteralPath $active.root) -or (Test-Path -LiteralPath $rollback.root)) { throw 'DeleteAll retained a valid manifest-owned version' }
if (-not (Test-Path -LiteralPath $tampered.root -PathType Container) -or -not (Test-Path -LiteralPath $unknown -PathType Container)) { throw 'DeleteAll removed an unowned path' }

[pscustomobject]@{
    status = 'PASS'
    positiveValidation = 'PASS'
    hashSubstitutionMutant = 'DETECTED'
    failedHealthCandidateRemoved = 'PASS'
    tamperedDeleteMutant = 'DETECTED'
    retainedActiveAndRollback = 'PASS'
    staleOwnedVersionRemoved = 'PASS'
    unknownAndTamperedPreserved = 'PASS'
}
