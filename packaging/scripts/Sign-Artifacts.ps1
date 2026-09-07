[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)][string[]]$ArtifactPath,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9A-Fa-f]{40}$')][string]$CertificateThumbprint,
    [Parameter(Mandatory = $true)][ValidatePattern('^https://')][string]$TimestampUrl,
    [Parameter(Mandatory = $true)][string]$SignToolPath,
    [Parameter()][switch]$Execute
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $Execute) {
    throw 'Dry-run only: pass -Execute under a separately authorized signing contract'
}
if (-not $PSCmdlet.ShouldProcess(($ArtifactPath -join ', '), 'Apply and verify Authenticode signatures')) { return }

$signTool = (Resolve-Path -LiteralPath $SignToolPath).Path
foreach ($pathValue in $ArtifactPath) {
    $path = (Resolve-Path -LiteralPath $pathValue).Path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Signing target is not a file: $path" }
    & $signTool sign /sha1 $CertificateThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 /v $path
    if ($LASTEXITCODE -ne 0) { throw "SignTool failed: $path" }
    & $signTool verify /pa /all /v $path
    if ($LASTEXITCODE -ne 0) { throw "Signature verification failed: $path" }
}

Write-Warning 'Sign inner broker/widget before package-manifest and ZIP generation; sign setup only after Inno compilation; regenerate release-manifest and checksums last.'
