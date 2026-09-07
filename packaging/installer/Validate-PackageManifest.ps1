[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedCandidateId
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-CanonicalCandidateId($Entries) {
    $byPath = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([System.StringComparer]::Ordinal)
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

$manifestItem = Get-Item -LiteralPath $ManifestPath -Force
if (-not $manifestItem.Exists -or ($manifestItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { throw 'Preflight manifest is missing or unsafe' }
$manifest = Get-Content -LiteralPath $manifestItem.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
$required = @('schemaVersion', 'kind', 'internalProductId', 'displayName', 'displayNameStatus', 'version', 'architecture', 'candidateId', 'sourceIdentity', 'signatureStatus', 'brokerSha256', 'files')
$names = @($manifest.PSObject.Properties.Name)
if ($names.Count -ne $required.Count -or @($required | Where-Object { -not ($names -ccontains $_) }).Count -ne 0) { throw 'Preflight manifest schema is invalid' }
if ($manifest.schemaVersion -cne '1.0.0' -or $manifest.kind -cne 'installed-payload' -or $manifest.internalProductId -cne 'AILimitsWidget' -or $manifest.candidateId -cne $ExpectedCandidateId) { throw 'Preflight manifest identity mismatch' }
if ((Get-CanonicalCandidateId $manifest.files) -cne $ExpectedCandidateId) { throw 'Preflight candidate ID mismatch' }
[pscustomobject]@{ status = 'PASS'; candidateId = $ExpectedCandidateId }
