[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ReleaseRoot,
    [Parameter()][string]$PythonExe = 'python'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$tool = Join-Path $PSScriptRoot 'release_tools.py'
if (-not [System.IO.Path]::IsPathRooted($ReleaseRoot) -or $ReleaseRoot -cnotmatch '^[A-Za-z]:[\\/]') { throw 'ReleaseRoot must be an absolute local drive path' }
$release = (Resolve-Path -LiteralPath $ReleaseRoot).Path
if (-not (Test-Path -LiteralPath $release -PathType Container)) { throw 'ReleaseRoot must be a directory' }
& $PythonExe -I -B $tool verify --release-root $release
if ($LASTEXITCODE -ne 0) { throw 'Release verification failed' }
