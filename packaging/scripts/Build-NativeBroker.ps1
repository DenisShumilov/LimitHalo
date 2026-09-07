[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [Parameter(Mandatory = $true)][string]$VsWherePath,
    [Parameter(Mandatory = $true)][string]$GeneratedResourceRoot,
    [Parameter(Mandatory = $true)][string]$NativeBuildLockPath,
    [Parameter(Mandatory = $true)][string]$AuthorizedStateRoot,
    [Parameter()][string]$NativeToolchainRoot = '',
    [Parameter()][switch]$RunSyntheticSelfTest
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-AbsoluteDrivePath([string]$PathValue) {
    return [System.IO.Path]::IsPathRooted($PathValue) -and $PathValue -cmatch '^[A-Za-z]:[\\/]'
}

function Resolve-Existing([string]$PathValue, [string]$Label, [bool]$Directory) {
    if (-not (Test-AbsoluteDrivePath $PathValue)) { throw "$Label must be an absolute local drive path" }
    $resolved = (Resolve-Path -LiteralPath $PathValue).Path
    $kind = if ($Directory) { 'Container' } else { 'Leaf' }
    if (-not (Test-Path -LiteralPath $resolved -PathType $kind)) { throw "$Label has the wrong type" }
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

function Assert-Hash([string]$PathValue, [string]$Expected, [string]$Label) {
    if (-not (Test-Path -LiteralPath $PathValue -PathType Leaf)) { throw "$Label is missing" }
    $actual = (Get-FileHash -LiteralPath $PathValue -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -cne $Expected) { throw "$Label hash mismatch" }
    return $actual
}

function Invoke-Checked([string]$FilePath, [string[]]$Arguments, [string]$Label) {
    & $FilePath @Arguments 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}

$state = Resolve-Existing $AuthorizedStateRoot 'AuthorizedStateRoot' $true
$source = Resolve-Existing $SourceRoot 'SourceRoot' $true
$output = Resolve-Existing $OutputRoot 'OutputRoot' $true
$vswhere = Resolve-Existing $VsWherePath 'VsWherePath' $false
$resources = Resolve-Existing $GeneratedResourceRoot 'GeneratedResourceRoot' $true
$lockPath = Resolve-Existing $NativeBuildLockPath 'NativeBuildLockPath' $false
$nativeToolchain = $null
if (-not [string]::IsNullOrWhiteSpace($NativeToolchainRoot)) {
    $nativeToolchain = Resolve-Existing $NativeToolchainRoot 'NativeToolchainRoot' $true
}
foreach ($entry in @(
    @($source, 'SourceRoot'),
    @($output, 'OutputRoot'),
    @($resources, 'GeneratedResourceRoot'),
    @($lockPath, 'NativeBuildLockPath'),
    @($nativeToolchain, 'NativeToolchainRoot')
)) {
    if ($null -eq $entry[0]) { continue }
    if (-not (Test-Within $entry[0] $state) -or $entry[0].Equals($state, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ($entry[1] + ' must be a strict descendant of AuthorizedStateRoot')
    }
}
Assert-NoReparseComponents $state 'AuthorizedStateRoot'
Assert-NoReparseComponents $source 'SourceRoot'
Assert-NoReparseComponents $output 'OutputRoot'
Assert-NoReparseComponents $resources 'GeneratedResourceRoot'
Assert-NoReparseComponents $lockPath 'NativeBuildLockPath'
if ($null -ne $nativeToolchain) { Assert-NoReparseComponents $nativeToolchain 'NativeToolchainRoot' }
$lock = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($lock.schemaVersion -cne '1.0.0' -or $lock.msvcToolsVersion -cne '14.44.35207' -or $lock.windowsSdkVersion -cne '10.0.26100.0' -or $lock.architecture -cne 'x64') {
    throw 'Unsupported native build lock'
}

$requiredSources = @('broker_core.cpp', 'broker_core.h', 'production_adapter.cpp', 'production_adapter.h', 'production_main.cpp', 'synthetic_main.cpp')
foreach ($name in $requiredSources) {
    $path = Join-Path $source $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing native source: $name" }
    if ((Get-Item -LiteralPath $path).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw "Native source is a reparse point: $name" }
}

$sdkVersion = [string]$lock.windowsSdkVersion
if ($null -ne $nativeToolchain) {
    $toolRoot = Join-Path $nativeToolchain 'msvc'
    $toolBin = Join-Path $toolRoot 'bin-x64'
    $sdkRoot = Join-Path $nativeToolchain 'sdk'
    $sdkBin = Join-Path $sdkRoot 'bin'
    $sdkInclude = Join-Path $sdkRoot 'include'
    $msvcLibraryRoot = Join-Path $nativeToolchain 'lib\msvc'
    $ucrtLibraryRoot = Join-Path $nativeToolchain 'lib\ucrt'
    $umLibraryRoot = Join-Path $nativeToolchain 'lib\um'
    $toolchainMode = 'staged-explicit'
} else {
    $installations = @(& $vswhere -latest -products '*' -version '[17.14,17.15)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath)
    if ($LASTEXITCODE -ne 0 -or $installations.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$installations[0])) {
        throw 'vswhere did not resolve one Visual Studio 17.14 installation'
    }
    $visualStudio = (Resolve-Path -LiteralPath ([string]$installations[0])).Path
    $toolRoot = Join-Path $visualStudio ('VC\Tools\MSVC\' + [string]$lock.msvcToolsVersion)
    $toolBin = Join-Path $toolRoot 'bin\Hostx64\x64'
    $kitsProperty = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots'
    $kitsRoot = [string]$kitsProperty.KitsRoot10
    if ([string]::IsNullOrWhiteSpace($kitsRoot)) { throw 'Windows 10 SDK root is not registered' }
    $sdkBin = Join-Path $kitsRoot ('bin\' + $sdkVersion + '\x64')
    $sdkInclude = Join-Path $kitsRoot ('Include\' + $sdkVersion)
    $sdkLib = Join-Path $kitsRoot ('Lib\' + $sdkVersion)
    $msvcLibraryRoot = Join-Path $toolRoot 'lib\x64'
    $ucrtLibraryRoot = Join-Path $sdkLib 'ucrt\x64'
    $umLibraryRoot = Join-Path $sdkLib 'um\x64'
    $toolchainMode = 'hosted-discovered'
}
$cl = Join-Path $toolBin 'cl.exe'
$link = Join-Path $toolBin 'link.exe'
$rc = Join-Path $sdkBin 'rc.exe'
$mt = Join-Path $sdkBin 'mt.exe'
Assert-Hash $cl ([string]$lock.toolHashes.'cl.exe') 'MSVC compiler' | Out-Null
Assert-Hash $link ([string]$lock.toolHashes.'link.exe') 'MSVC linker' | Out-Null
Assert-Hash $rc ([string]$lock.toolHashes.'rc.exe') 'Windows SDK resource compiler' | Out-Null
Assert-Hash $mt ([string]$lock.toolHashes.'mt.exe') 'Windows SDK manifest tool' | Out-Null

$libraries = @('kernel32.lib', 'advapi32.lib', 'winhttp.lib', 'wer.lib', 'crypt32.lib', 'bcrypt.lib')
foreach ($name in $libraries) {
    $path = Join-Path $umLibraryRoot $name
    Assert-Hash $path ([string]$lock.toolHashes.$name) ("Windows SDK library $name") | Out-Null
}
$msvcLibraries = @('libcmt.lib', 'libcpmt.lib', 'libvcruntime.lib', 'oldnames.lib')
foreach ($name in $msvcLibraries) {
    $path = Join-Path $msvcLibraryRoot $name
    Assert-Hash $path ([string]$lock.toolHashes.$name) ("MSVC library $name") | Out-Null
}
$ucrtLibrary = Join-Path $ucrtLibraryRoot 'libucrt.lib'
Assert-Hash $ucrtLibrary ([string]$lock.toolHashes.'libucrt.lib') 'Windows SDK static UCRT library' | Out-Null

$env:INCLUDE = [string]::Join(';', @(
    (Join-Path $toolRoot 'include'),
    (Join-Path $sdkInclude 'ucrt'),
    (Join-Path $sdkInclude 'shared'),
    (Join-Path $sdkInclude 'um'),
    (Join-Path $sdkInclude 'winrt'),
    (Join-Path $sdkInclude 'cppwinrt')
))
$env:LIB = [string]::Join(';', @(
    $msvcLibraryRoot,
    $ucrtLibraryRoot,
    $umLibraryRoot
))
$originalPath = $env:PATH
if ($null -ne $nativeToolchain) {
    $env:PATH = $toolBin + ';' + $sdkBin + ';' + (Join-Path $env:SystemRoot 'System32')
} else {
    $env:PATH = $toolBin + ';' + $sdkBin + ';' + $originalPath
}

$objectRoot = Join-Path $output 'obj'
New-Item -ItemType Directory -Path $objectRoot -Force | Out-Null
$resourceInputs = @()
$resourceCompilerExecuted = $false
if ($null -eq $nativeToolchain) {
    $resourceFile = Join-Path $resources 'ClaudeUsageBroker.version.rc'
    $resourceObject = Join-Path $objectRoot 'ClaudeUsageBroker.res'
    Invoke-Checked $rc @('/nologo', ('/fo' + $resourceObject), $resourceFile) 'resource compilation'
    $resourceInputs = @($resourceObject)
    $resourceCompilerExecuted = $true
}

$compileFlags = @('/nologo', '/std:c++20', '/O2', '/MT', '/EHsc', '/utf-8', '/DUNICODE', '/D_UNICODE', '/permissive-', '/Zc:__cplusplus', '/Zc:inline', '/GS', '/guard:cf', '/Qspectre', '/Brepro', '/W4', '/WX', '/c')
$objects = [System.Collections.Generic.Dictionary[string,string]]::new([System.StringComparer]::Ordinal)
foreach ($name in @('broker_core.cpp', 'production_adapter.cpp', 'production_main.cpp', 'synthetic_main.cpp')) {
    $object = Join-Path $objectRoot ([System.IO.Path]::GetFileNameWithoutExtension($name) + '.obj')
    Invoke-Checked $cl ($compileFlags + @(('/Fo' + $object), (Join-Path $source $name))) ("compile $name")
    $objects.Add($name, $object)
}

$brokerManifest = Join-Path $resources 'ClaudeUsageBroker.manifest'
$linkFlags = @('/NOLOGO', '/SUBSYSTEM:CONSOLE', '/DYNAMICBASE', '/NXCOMPAT', '/HIGHENTROPYVA', '/GUARD:CF', '/CETCOMPAT', '/Brepro', '/INCREMENTAL:NO', '/NODEFAULTLIB:uuid.lib', '/MANIFEST:EMBED', ('/MANIFESTINPUT:' + $brokerManifest))
$commonLibraries = @('kernel32.lib', 'advapi32.lib', 'winhttp.lib', 'wer.lib', 'crypt32.lib', 'bcrypt.lib')
$broker = Join-Path $output 'ClaudeUsageBroker.exe'
Invoke-Checked $link ($linkFlags + @(('/OUT:' + $broker), $objects['broker_core.cpp'], $objects['production_adapter.cpp'], $objects['production_main.cpp']) + $resourceInputs + $commonLibraries) 'production broker link'
$synthetic = Join-Path $output 'ClaudeUsageBrokerSynthetic.exe'
Invoke-Checked $link ($linkFlags + @(('/OUT:' + $synthetic), $objects['broker_core.cpp'], $objects['synthetic_main.cpp']) + $resourceInputs + $commonLibraries) 'synthetic broker link'

if ($RunSyntheticSelfTest) {
    $selfTestText = & $synthetic --selftest
    if ($LASTEXITCODE -ne 0) { throw "Native broker self-test failed with exit code $LASTEXITCODE" }
    $selfTest = $selfTestText | ConvertFrom-Json
    if ($selfTest.suite -cne 'broker-synthetic-v1' -or $selfTest.status -cne 'CANDIDATE_READY' -or $selfTest.sentinelLeakDetected -ne $false) {
        throw 'Native broker self-test result is invalid'
    }
    $selfTestExecuted = $true
} else {
    $selfTest = [ordered]@{
        suite = 'broker-synthetic-v1'
        status = 'NOT_EXECUTED_LOCAL_POLICY'
        sentinelLeakDetected = $null
    }
    $selfTestExecuted = $false
}

$sourceHashes = [ordered]@{}
foreach ($name in $requiredSources) {
    $sourceHashes[$name] = (Get-FileHash -LiteralPath (Join-Path $source $name) -Algorithm SHA256).Hash.ToLowerInvariant()
}
$receipt = [ordered]@{
    schemaVersion = '1.0.0'
    msvcToolsVersion = [string]$lock.msvcToolsVersion
    windowsSdkVersion = $sdkVersion
    compilerSha256 = (Get-FileHash -LiteralPath $cl -Algorithm SHA256).Hash.ToLowerInvariant()
    linkerSha256 = (Get-FileHash -LiteralPath $link -Algorithm SHA256).Hash.ToLowerInvariant()
    resourceCompilerSha256 = (Get-FileHash -LiteralPath $rc -Algorithm SHA256).Hash.ToLowerInvariant()
    resourceCompilerExecuted = $resourceCompilerExecuted
    versionResourceEmbedded = $resourceCompilerExecuted
    manifestToolSha256 = (Get-FileHash -LiteralPath $mt -Algorithm SHA256).Hash.ToLowerInvariant()
    toolchainMode = $toolchainMode
    suppressedAbsentDefaultLibrary = 'uuid.lib'
    sourceSha256 = $sourceHashes
    brokerSha256 = (Get-FileHash -LiteralPath $broker -Algorithm SHA256).Hash.ToLowerInvariant()
    syntheticSha256 = (Get-FileHash -LiteralPath $synthetic -Algorithm SHA256).Hash.ToLowerInvariant()
    selfTest = $selfTest
    selfTestExecuted = $selfTestExecuted
}
$receiptPath = Join-Path $output 'native-build-receipt.json'
[System.IO.File]::WriteAllText($receiptPath, (($receipt | ConvertTo-Json -Depth 8) + "`n"), [System.Text.UTF8Encoding]::new($false))
$env:PATH = $originalPath

[pscustomobject]@{
    broker = $broker
    brokerSha256 = $receipt.brokerSha256
    synthetic = $synthetic
    receipt = $receiptPath
}
