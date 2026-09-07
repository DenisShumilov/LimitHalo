[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AuthorizedStateRoot,
    [Parameter(Mandatory = $true)][string]$InstallerLockPath,
    [Parameter(Mandatory = $true)][string]$InstallerPath,
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [Parameter(Mandatory = $true)][string]$ReceiptPath,
    [Parameter(Mandatory = $true)][string]$PythonExe
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$contractSha256 = '0d01b424176f389cc9bb1e602dc0574ed1f2e16a5d6c0da62cd4fd2473e66962'
$expectedCompilerSha256 = 'd06ebd38f38e3cee60a3c50cc45bd449d77e0bc6a5cabc607ea9886808e4de1a'

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
        if (($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse component"
        }
        $cursor = $cursor.Parent
    }
}

if ($env:GITHUB_ACTIONS -cne 'true' -or
    $env:RUNNER_ENVIRONMENT -cne 'github-hosted' -or
    $env:RUNNER_OS -cne 'Windows' -or
    $env:ImageOS -cne 'win25' -or
    $env:GITHUB_RUN_ID -cnotmatch '^[1-9][0-9]*$') {
    throw 'Hosted Inno acquisition is allowed only on the exact disposable GitHub-hosted Windows 2025 runner'
}
foreach ($value in @($AuthorizedStateRoot, $InstallerLockPath, $InstallerPath, $InstallRoot, $ReceiptPath, $PythonExe)) {
    if (-not (Test-AbsoluteDrivePath $value)) { throw 'Every acquisition path must be an absolute local drive path' }
}
$state = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $AuthorizedStateRoot).Path).TrimEnd('\')
if (-not (Test-Path -LiteralPath $state -PathType Container)) { throw 'AuthorizedStateRoot must be a directory' }
Assert-NoReparseComponents $state 'AuthorizedStateRoot'
$runnerTemp = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $env:RUNNER_TEMP).Path).TrimEnd('\')
if (-not (Test-Within $state $runnerTemp) -or $state.Equals($runnerTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'AuthorizedStateRoot must be a strict descendant of RUNNER_TEMP'
}
$runtimeRoot = Join-Path $state 'hosted-acquisition-state'
$runtimeTemp = Join-Path $runtimeRoot 'temp'
$runtimeLocalAppData = Join-Path $runtimeRoot 'local-app-data'
$runtimeRoamingAppData = Join-Path $runtimeRoot 'roaming-app-data'
$runtimeUserProfile = Join-Path $runtimeRoot 'user-profile'
New-Item -ItemType Directory -Force -Path $runtimeTemp, $runtimeLocalAppData, $runtimeRoamingAppData, $runtimeUserProfile | Out-Null
$env:TEMP = $runtimeTemp
$env:TMP = $runtimeTemp
$env:LOCALAPPDATA = $runtimeLocalAppData
$env:APPDATA = $runtimeRoamingAppData
$env:USERPROFILE = $runtimeUserProfile
$lockPath = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $InstallerLockPath).Path)
$python = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $PythonExe).Path)
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw 'InstallerLockPath must be a file' }
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'PythonExe must be a file' }
$download = [System.IO.Path]::GetFullPath($InstallerPath)
$install = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$receipt = [System.IO.Path]::GetFullPath($ReceiptPath)
foreach ($item in @($lockPath, $download, $install, $receipt)) {
    if (-not (Test-Within $item $state) -or $item.Equals($state, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installer lock and acquisition outputs must be strict descendants of AuthorizedStateRoot'
    }
}
if (Test-Path -LiteralPath $download) { throw 'InstallerPath must not already exist' }
if (Test-Path -LiteralPath $receipt) { throw 'ReceiptPath must not already exist' }
if (Test-Path -LiteralPath $install) { throw 'InstallRoot must not already exist' }
foreach ($parent in @((Split-Path -Parent $download), (Split-Path -Parent $install), (Split-Path -Parent $receipt))) {
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { New-Item -ItemType Directory -Path $parent | Out-Null }
    Assert-NoReparseComponents $parent 'Acquisition output parent'
}

$lock = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($lock.schemaVersion -cne '1.0.0' -or
    $lock.product -cne 'Inno Setup' -or
    $lock.version -cne '7.1.0' -or
    $lock.edition -cne 'x64' -or
    [int64]$lock.size -ne 14304168 -or
    $lock.sha256 -cne '0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f' -or
    $lock.authenticode.requiredStatus -cne 'Valid' -or
    $lock.authenticode.requiredLeafSubject -cne 'CN=Pyrsys B.V., O=Pyrsys B.V., S=Noord-Holland, C=NL' -or
    $lock.source -cne 'https://github.com/jrsoftware/issrc/releases/download/is-7_1_0/innosetup-7.1.0-x64.exe') {
    throw 'Inno Setup lock identity mismatch'
}

# This is the only network acquisition branch. It runs before the acquisition-free build step.
Invoke-WebRequest -UseBasicParsing -Uri ([string]$lock.source) -OutFile $download
if ((Get-Item -LiteralPath $download).Length -ne [int64]$lock.size -or
    (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$lock.sha256) {
    throw 'Downloaded Inno Setup input does not match its frozen identity'
}
$signature = Get-AuthenticodeSignature -LiteralPath $download
if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
    $signature.SignerCertificate.Subject -cne [string]$lock.authenticode.requiredLeafSubject) {
    throw 'Downloaded Inno Setup signature does not match its frozen identity'
}
$process = Start-Process -FilePath $download -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', ('/DIR=' + $install)
) -Wait -PassThru
if ($process.ExitCode -ne 0) { throw ('Inno Setup installer exit code ' + $process.ExitCode) }
$compiler = Join-Path $install 'ISCC.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'ISCC.exe is missing after hosted installation' }
$compilerHash = (Get-FileHash -LiteralPath $compiler -Algorithm SHA256).Hash.ToLowerInvariant()
if ($compilerHash -cne $expectedCompilerSha256) { throw 'Hosted Inno Setup compiler identity mismatch' }

$value = [ordered]@{
    schemaVersion = '1.0.0'
    contractSha256 = $contractSha256
    status = 'PASS'
    provenanceMode = 'github-hosted-disposable-runner-acquisition'
    installerSha256 = [string]$lock.sha256
    installerSignature = 'Valid Pyrsys B.V.'
    compilerIdentitySource = 'pinned-signed-installer'
    compilerSha256 = $compilerHash
    networkEnabled = $true
    hostInstallPerformed = $true
    disposableRunner = $true
    runnerEnvironment = $env:RUNNER_ENVIRONMENT
    runnerOs = $env:RUNNER_OS
    imageOs = $env:ImageOS
    githubRunId = $env:GITHUB_RUN_ID
    createdUtc = [DateTime]::UtcNow.ToString('o')
}
[System.IO.File]::WriteAllText($receipt, (($value | ConvertTo-Json -Depth 4) + "`n"), [System.Text.UTF8Encoding]::new($false))
$validator = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\inno_provenance.py'
& $python -I -B $validator --receipt $receipt --compiler $compiler --installer-lock $lockPath --contract-sha256 $contractSha256 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Hosted Inno Setup provenance validation failed' }
[pscustomobject]@{ status = 'PASS'; compiler = $compiler; receipt = $receipt; acquisition = 'network-enabled-disposable-hosted-runner' }
