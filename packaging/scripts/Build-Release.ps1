[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AppSourceRoot,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [Parameter(Mandatory = $true)][string]$WheelhouseRoot,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$VsWherePath,
    [Parameter(Mandatory = $true)][string]$InnoSetupCompiler,
    [Parameter(Mandatory = $true)][string]$InnoProvenancePath,
    [Parameter(Mandatory = $true)][string]$AuthorizedStateRoot,
    [Parameter(Mandatory = $true)][string]$SourceManifestPath,
    [Parameter(Mandatory = $true)][string]$SourceIdentity,
    [Parameter()][string]$EntryPointRelativePath = 'src\limit_halo\__main__.py',
    [Parameter()][string]$BrokerSourceRelativePath = 'native\claude_broker',
    [Parameter()][string]$AssetRootRelativePath = 'src\limit_halo\assets',
    [Parameter()][string]$NativeToolchainRoot = '',
    [Parameter()][string]$ExpectedPythonSha256 = '',
    [Parameter()][switch]$RunNativeSelfTest
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$contractSha256 = '0d01b424176f389cc9bb1e602dc0574ed1f2e16a5d6c0da62cd4fd2473e66962'

function Test-AbsoluteDrivePath([string]$PathValue) {
    return [System.IO.Path]::IsPathRooted($PathValue) -and $PathValue -cmatch '^[A-Za-z]:[\\/]'
}

function Resolve-AbsoluteExisting([string]$PathValue, [string]$Label, [bool]$Directory) {
    if (-not (Test-AbsoluteDrivePath $PathValue)) { throw "$Label must be an absolute local drive path" }
    $resolved = (Resolve-Path -LiteralPath $PathValue).Path
    $kind = if ($Directory) { 'Container' } else { 'Leaf' }
    if (-not (Test-Path -LiteralPath $resolved -PathType $kind)) { throw "$Label has the wrong type" }
    return [System.IO.Path]::GetFullPath($resolved)
}

function Assert-ChildPath([string]$Root, [string]$Child, [string]$Label) {
    $rootPrefix = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $full = [System.IO.Path]::GetFullPath($Child)
    if (-not $full.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw "$Label escapes its root" }
    return $full
}

function Test-Within([string]$Candidate, [string]$PotentialParent) {
    $candidateFull = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $parentFull = [System.IO.Path]::GetFullPath($PotentialParent).TrimEnd('\')
    return $candidateFull.Equals($parentFull, [System.StringComparison]::OrdinalIgnoreCase) -or $candidateFull.StartsWith($parentFull + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-NotReparse([string]$PathValue, [string]$Label) {
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

function Initialize-OwnedOutputRoot([string]$PathValue, [string[]]$ProtectedRoots) {
    if (-not (Test-AbsoluteDrivePath $PathValue)) { throw 'OutputRoot must be an absolute local drive path' }
    $full = [System.IO.Path]::GetFullPath($PathValue).TrimEnd('\')
    if ($full -eq [System.IO.Path]::GetPathRoot($full).TrimEnd('\')) { throw 'OutputRoot cannot be a drive root' }
    foreach ($protected in $ProtectedRoots) {
        if (-not [string]::IsNullOrWhiteSpace($protected) -and (Test-Within $full $protected)) { throw "OutputRoot overlaps a protected root: $protected" }
    }
    $markerName = '.limit-halo-build-root'
    $markerContent = 'AILimitsWidget build root v1'
    if (-not (Test-Path -LiteralPath $full)) {
        New-Item -ItemType Directory -Path $full | Out-Null
    } elseif (-not (Test-Path -LiteralPath $full -PathType Container)) {
        throw 'OutputRoot is not a directory'
    }
    $marker = Join-Path $full $markerName
    $children = @(Get-ChildItem -LiteralPath $full -Force)
    if ($children.Count -gt 0) {
        if (-not (Test-Path -LiteralPath $marker -PathType Leaf) -or (Get-Content -LiteralPath $marker -Raw -Encoding UTF8).Trim() -cne $markerContent) {
            throw 'A non-empty OutputRoot must contain the exact ownership marker'
        }
        foreach ($child in $children) {
            if ($child.Name -ceq $markerName) { continue }
            $target = Assert-ChildPath $full $child.FullName 'Output cleanup target'
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
    [System.IO.File]::WriteAllText($marker, $markerContent + "`n", [System.Text.UTF8Encoding]::new($false))
    return $full
}

function Invoke-Checked([string]$FilePath, [string[]]$Arguments, [string]$Label) {
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}

$appSource = Resolve-AbsoluteExisting $AppSourceRoot 'AppSourceRoot' $true
$wheelhouse = Resolve-AbsoluteExisting $WheelhouseRoot 'WheelhouseRoot' $true
$buildPython = Resolve-AbsoluteExisting $PythonExe 'PythonExe' $false
$vswhere = Resolve-AbsoluteExisting $VsWherePath 'VsWherePath' $false
$iscc = Resolve-AbsoluteExisting $InnoSetupCompiler 'InnoSetupCompiler' $false
$innoProvenance = Resolve-AbsoluteExisting $InnoProvenancePath 'InnoProvenancePath' $false
$authorizedState = Resolve-AbsoluteExisting $AuthorizedStateRoot 'AuthorizedStateRoot' $true
$sourceManifest = Resolve-AbsoluteExisting $SourceManifestPath 'SourceManifestPath' $false
$nativeToolchain = $null
if (-not [string]::IsNullOrWhiteSpace($NativeToolchainRoot)) {
    $nativeToolchain = Resolve-AbsoluteExisting $NativeToolchainRoot 'NativeToolchainRoot' $true
}
Assert-NotReparse $authorizedState 'AuthorizedStateRoot'
Assert-NotReparse $appSource 'AppSourceRoot'
Assert-NotReparse $wheelhouse 'WheelhouseRoot'
Assert-NotReparse $iscc 'InnoSetupCompiler'
Assert-NotReparse $innoProvenance 'InnoProvenancePath'
Assert-NotReparse $sourceManifest 'SourceManifestPath'
if ($null -ne $nativeToolchain) { Assert-NotReparse $nativeToolchain 'NativeToolchainRoot' }
$outputFull = [System.IO.Path]::GetFullPath($OutputRoot).TrimEnd('\')
if ($outputFull.Equals($authorizedState.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase) -or -not (Test-Within $outputFull $authorizedState)) {
    throw 'OutputRoot must be a strict descendant of AuthorizedStateRoot'
}
foreach ($entry in @(
    @($repositoryRoot, 'RepositoryRoot'),
    @($appSource, 'AppSourceRoot'),
    @($wheelhouse, 'WheelhouseRoot'),
    @($iscc, 'InnoSetupCompiler'),
    @($innoProvenance, 'InnoProvenancePath'),
    @($sourceManifest, 'SourceManifestPath'),
    @($nativeToolchain, 'NativeToolchainRoot')
)) {
    if ($null -eq $entry[0]) { continue }
    if (-not (Test-Within $entry[0] $authorizedState) -or $entry[0].Equals($authorizedState, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ($entry[1] + ' must be a strict descendant of AuthorizedStateRoot')
    }
}
if (Test-Path -LiteralPath $outputFull) { Assert-NotReparse $outputFull 'OutputRoot' }
$userProfilePath = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
$localAppDataPath = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$roamingAppDataPath = [Environment]::GetFolderPath([Environment+SpecialFolder]::ApplicationData)
$protected = @(
    $appSource,
    $repositoryRoot,
    $userProfilePath,
    (Join-Path $localAppDataPath 'Programs\AILimitsWidget'),
    (Join-Path $localAppDataPath 'AILimitsWidget'),
    (Join-Path $roamingAppDataPath 'Microsoft\Windows\Start Menu\Programs\Startup')
)

# An explicit nested run directory is allowed even though the broader user profile is protected.
$protected = @($protected | Where-Object { -not (Test-Within ([System.IO.Path]::GetFullPath($OutputRoot)) $_) -or $_ -ne $userProfilePath })
$output = Initialize-OwnedOutputRoot $OutputRoot $protected
$buildRoot = Join-Path $output '_build'
$releaseRoot = Join-Path $output 'release'
New-Item -ItemType Directory -Path $buildRoot, $releaseRoot | Out-Null
$redirectedProfileRoot = Join-Path $buildRoot 'profile'
$redirectedLocalAppData = Join-Path $redirectedProfileRoot 'local-app-data'
$redirectedRoamingAppData = Join-Path $redirectedProfileRoot 'roaming-app-data'
$redirectedUserProfile = Join-Path $redirectedProfileRoot 'user-profile'
New-Item -ItemType Directory -Path $redirectedLocalAppData, $redirectedRoamingAppData, $redirectedUserProfile | Out-Null
$env:LOCALAPPDATA = $redirectedLocalAppData
$env:APPDATA = $redirectedRoamingAppData
$env:USERPROFILE = $redirectedUserProfile
$env:PYTHONNOUSERSITE = '1'
$tempRoot = Join-Path $buildRoot 'temp'
$cacheRoot = Join-Path $buildRoot 'cache'
$testFixtureRoot = Join-Path $buildRoot 'test-fixtures'
New-Item -ItemType Directory -Path $tempRoot, $cacheRoot, $testFixtureRoot | Out-Null
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:PYTHONPYCACHEPREFIX = Join-Path $cacheRoot 'pycache'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $cacheRoot 'pyinstaller'
$env:PIP_CACHE_DIR = Join-Path $cacheRoot 'pip'
$env:PIP_CONFIG_FILE = 'NUL'
$env:PYTHONHASHSEED = '0'
$env:SOURCE_DATE_EPOCH = '1704067200'
$env:LIMIT_HALO_FIXTURE_ONLY = '1'
$env:LIMIT_HALO_TEST_ROOT = $testFixtureRoot
$env:PIP_NO_INDEX = '1'
$env:HTTP_PROXY = 'http://127.0.0.1:9'
$env:HTTPS_PROXY = 'http://127.0.0.1:9'
$env:ALL_PROXY = 'http://127.0.0.1:9'
$env:NO_PROXY = 'localhost,127.0.0.1'

$entrypoint = Assert-ChildPath $appSource (Join-Path $appSource $EntryPointRelativePath) 'Entrypoint'
$brokerSource = Assert-ChildPath $appSource (Join-Path $appSource $BrokerSourceRelativePath) 'Broker source'
$assetRoot = Assert-ChildPath $appSource (Join-Path $appSource $AssetRootRelativePath) 'Asset root'
if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) { throw 'App entrypoint is missing' }
if (-not (Test-Path -LiteralPath $brokerSource -PathType Container)) { throw 'Native broker source root is missing' }

$productPath = Join-Path $repositoryRoot 'product.json'
$productVersion = [string](Get-Content -LiteralPath $productPath -Raw -Encoding UTF8 | ConvertFrom-Json).version
if ($productVersion -cnotmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') { throw 'Invalid product version' }
$agentSource = Join-Path $repositoryRoot 'LimitHalo-Agent.ps1'
$pythonLock = Join-Path $repositoryRoot 'requirements\python-build.lock.json'
$nativeLock = Join-Path $repositoryRoot 'requirements\native-build.lock.json'
$innoLock = Join-Path $repositoryRoot 'requirements\inno-setup.lock.json'
$generated = Join-Path $buildRoot 'generated'
New-Item -ItemType Directory -Path $generated | Out-Null
if (-not (Test-Path -LiteralPath $agentSource -PathType Leaf)) { throw 'LimitHalo AI agent entry point is missing' }
Assert-NotReparse $agentSource 'LimitHalo AI agent entry point'

$pythonHash = (Get-FileHash -LiteralPath $buildPython -Algorithm SHA256).Hash.ToLowerInvariant()
if (-not [string]::IsNullOrWhiteSpace($ExpectedPythonSha256)) {
    if ($ExpectedPythonSha256 -cnotmatch '^[0-9a-f]{64}$' -or $pythonHash -cne $ExpectedPythonSha256) {
        throw 'Python build interpreter does not match the caller-bound provenance hash'
    }
}
$versionText = (& $buildPython -I -B -c 'import platform,struct; print(platform.python_version()+"|"+str(struct.calcsize("P")*8))').Trim()
if ($LASTEXITCODE -ne 0 -or $versionText -cne '3.12.13|64') { throw 'Python version or architecture mismatch' }

$sourceManifestTool = Join-Path $repositoryRoot 'packaging\scripts\source_manifest.py'
$manifestTree = (& $buildPython -I -B $sourceManifestTool verify --root $appSource --manifest $sourceManifest --contract-sha256 $contractSha256).Trim()
if ($LASTEXITCODE -ne 0 -or $manifestTree -notmatch '^[0-9a-f]{64}$') { throw 'Source manifest verification failed' }
$expectedSourceIdentity = 'sha256:' + $manifestTree
if ($SourceIdentity -cne $expectedSourceIdentity) {
    throw 'SourceIdentity does not match the verified source manifest tree'
}
$provenanceTool = Join-Path $PSScriptRoot 'inno_provenance.py'
$provenanceResult = (& $buildPython -I -B $provenanceTool --receipt $innoProvenance --compiler $iscc --installer-lock $innoLock --contract-sha256 $contractSha256).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup provenance validation failed' }
try { $normalizedProvenance = $provenanceResult | ConvertFrom-Json } catch { throw 'Inno Setup provenance validator returned invalid JSON' }
if ($normalizedProvenance.status -cne 'PASS') { throw 'Inno Setup provenance did not pass' }
Invoke-Checked $buildPython @('-I', '-B', (Join-Path $repositoryRoot 'scripts\check_source.py')) 'source and native broker lineage gate'

& (Join-Path $PSScriptRoot 'Assert-PythonBuildLock.ps1') -LockPath $pythonLock -WheelhouseRoot $wheelhouse | Out-Null
Invoke-Checked $buildPython @('-I', '-B', (Join-Path $PSScriptRoot 'generate_version_info.py'), '--product', $productPath, '--output-root', $generated) 'version metadata generation'

$pythonScriptsRoot = Split-Path -Parent $buildPython
if ((Split-Path -Leaf $pythonScriptsRoot) -ine 'Scripts') { throw 'PythonExe must be the exact staged build-environment interpreter' }
$buildEnvironmentRoot = Split-Path -Parent $pythonScriptsRoot
$expectedPackages = 'altgraph=0.17.5|packaging=26.3|pefile=2024.8.26|Pillow=12.3.0|pyinstaller=6.21.0|pyinstaller-hooks-contrib=2026.6|pywin32-ctypes=0.2.3|setuptools=83.0.0'
$packageProbe = 'import importlib.metadata as m; names=("altgraph","packaging","pefile","Pillow","pyinstaller","pyinstaller-hooks-contrib","pywin32-ctypes","setuptools"); print("|".join(n+"="+m.version(n) for n in names))'
$actualPackages = (& $buildPython -I -B -c $packageProbe).Trim()
if ($LASTEXITCODE -ne 0 -or $actualPackages -cne $expectedPackages) { throw 'Staged Python package closure mismatch' }
$pyinstallerBootloader = Join-Path $buildEnvironmentRoot 'Lib\site-packages\PyInstaller\bootloader\Windows-64bit-intel\runw.exe'
$pyinstallerVersion = (& $buildPython -I -B -m PyInstaller --version).Trim()
if ($LASTEXITCODE -ne 0 -or $pyinstallerVersion -cne '6.21.0') { throw 'PyInstaller package version mismatch' }
if ((Get-FileHash -LiteralPath $pyinstallerBootloader -Algorithm SHA256).Hash.ToLowerInvariant() -cne '184e0d1ade1e772b35531867c4b02215d81ca3df62caf202ee27ae3be94aee60') { throw 'PyInstaller bootloader hash mismatch' }

$nativeOutput = Join-Path $buildRoot 'native'
New-Item -ItemType Directory -Path $nativeOutput | Out-Null
$nativeArguments = @{
    SourceRoot = $brokerSource
    OutputRoot = $nativeOutput
    VsWherePath = $vswhere
    GeneratedResourceRoot = $generated
    NativeBuildLockPath = $nativeLock
    AuthorizedStateRoot = $authorizedState
}
if ($null -ne $nativeToolchain) { $nativeArguments.NativeToolchainRoot = $nativeToolchain }
if ($RunNativeSelfTest) { $nativeArguments.RunSyntheticSelfTest = $true }
$nativeResult = & (Join-Path $PSScriptRoot 'Build-NativeBroker.ps1') @nativeArguments
if ($null -eq $nativeResult -or -not (Test-Path -LiteralPath $nativeResult.broker -PathType Leaf)) { throw 'Native broker build did not return an artifact' }

$testsRoot = Join-Path $appSource 'tests'
if (-not (Test-Path -LiteralPath $testsRoot -PathType Container)) { throw 'Fixture-only app tests are required before packaging' }
$unittestBootstrap = @'
import pathlib
import sys
import unittest

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "src"))
suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_*.py", top_level_dir=str(root))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
'@
Invoke-Checked $buildPython @('-I', '-B', '-c', $unittestBootstrap, $appSource) 'fixture-only application tests'

$packagingSource = Join-Path $buildRoot 'packaging-source'
$packagingSrc = Join-Path $packagingSource 'src'
New-Item -ItemType Directory -Path $packagingSrc | Out-Null
Get-ChildItem -LiteralPath (Join-Path $appSource 'src') -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $packagingSrc -Recurse
}
$stagedBuildIdentity = Join-Path $packagingSrc 'limit_halo\build_identity.py'
if (-not (Test-Path -LiteralPath $stagedBuildIdentity -PathType Leaf)) { throw 'Staged build identity module is missing' }
$brokerHash = (Get-FileHash -LiteralPath $nativeResult.broker -Algorithm SHA256).Hash.ToLowerInvariant()
$identitySource = @(
    '"""Generated release identity for the compiled native broker."""',
    '',
    'BROKER_RELATIVE_PATH = "ClaudeUsageBroker.exe"',
    ('BROKER_SHA256 = "' + $brokerHash + '"'),
    'BROKER_PROTOCOL = "ai-usage-claude-broker/1"',
    ''
) -join "`n"
[System.IO.File]::WriteAllText($stagedBuildIdentity, $identitySource, [System.Text.UTF8Encoding]::new($false))

$distRoot = Join-Path $buildRoot 'dist'
$workRoot = Join-Path $buildRoot 'pyinstaller-work'
$env:LIMIT_HALO_APP_SOURCE_ROOT = $packagingSource
$env:LIMIT_HALO_ENTRYPOINT = $EntryPointRelativePath.Replace('\', '/')
$env:LIMIT_HALO_ASSET_ROOT = $AssetRootRelativePath.Replace('\', '/')
$env:LIMIT_HALO_VERSION_FILE = Join-Path $generated 'AILimitsWidget.version.txt'
$env:LIMIT_HALO_WIDGET_MANIFEST = Join-Path $generated 'AILimitsWidget.manifest'
Invoke-Checked $buildPython @('-I', '-B', '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', $distRoot, '--workpath', $workRoot, (Join-Path $repositoryRoot 'packaging\pyinstaller\AILimitsWidget.spec')) 'PyInstaller onedir build'

$payload = Join-Path $distRoot 'AILimitsWidget'
if (-not (Test-Path -LiteralPath (Join-Path $payload 'AILimitsWidget.exe') -PathType Leaf)) { throw 'PyInstaller output is missing the widget executable' }
$releaseTools = Join-Path $PSScriptRoot 'release_tools.py'
Invoke-Checked $buildPython @(
    '-I', '-B', $releaseTools, 'normalize-stdlib-zip',
    '--archive', (Join-Path $payload '_internal\base_library.zip')
) 'deterministic Python standard-library archive normalization'
Copy-Item -LiteralPath $nativeResult.broker -Destination (Join-Path $payload 'ClaudeUsageBroker.exe')
Copy-Item -LiteralPath $nativeResult.receipt -Destination (Join-Path $payload 'native-build-receipt.json')
foreach ($name in @('README.md', 'PRIVACY.md', 'SECURITY.md', 'INTEGRATIONS.md', 'CHANGELOG.md', 'LICENSE', 'THIRD-PARTY-NOTICES.md', 'product.json')) {
    Copy-Item -LiteralPath (Join-Path $repositoryRoot $name) -Destination (Join-Path $payload $name)
}
Copy-Item -LiteralPath (Join-Path $repositoryRoot 'INSTALL_WITH_AI.md') -Destination (Join-Path $payload 'INSTALL_WITH_AI.md')
Copy-Item -LiteralPath $agentSource -Destination (Join-Path $payload 'LimitHalo-Agent.ps1')
Copy-Item -LiteralPath (Join-Path $repositoryRoot 'packaging\installer\Invoke-ManifestOwnedCleanup.ps1') -Destination (Join-Path $payload 'Invoke-ManifestOwnedCleanup.ps1')
Copy-Item -LiteralPath (Join-Path $repositoryRoot 'packaging\installer\Invoke-OwnedShortcut.ps1') -Destination (Join-Path $payload 'Invoke-OwnedShortcut.ps1')
$assetLicense = Join-Path $assetRoot 'LICENSE-lobehub.txt'
$assetAttribution = Join-Path $assetRoot 'ATTRIBUTIONS.md'
$tclTkLicense = Join-Path $payload '_internal\_tk_data\license.terms'
$pythonLicense = (& $buildPython -I -B -c 'import pathlib,sys; print(pathlib.Path(sys.base_prefix, "LICENSE.txt"))').Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonLicense -PathType Leaf)) { throw 'Pinned Python PSF license is missing' }
$dependencyLicenses = Join-Path $PSScriptRoot 'dependency_licenses.py'
Invoke-Checked $buildPython @(
    '-I', '-B', $dependencyLicenses, 'collect',
    '--wheelhouse', $wheelhouse,
    '--lock', $pythonLock,
    '--payload-root', $payload,
    '--python-license', $pythonLicense,
    '--tcl-tk-license', $tclTkLicense,
    '--lobehub-license', $assetLicense,
    '--lobehub-attribution', $assetAttribution
) 'dependency license and METADATA closure'
$dependencyLicenseManifest = Join-Path $payload 'licenses\dependency-licenses.json'
if (-not (Test-Path -LiteralPath $dependencyLicenseManifest -PathType Leaf)) { throw 'Dependency license closure manifest is missing' }

$payloadManifest = Join-Path $payload 'package-manifest.json'
$candidateOutput = & $buildPython -I -B $releaseTools payload-manifest --root $payload --output $payloadManifest --product $productPath --source-identity $SourceIdentity
if ($LASTEXITCODE -ne 0) { throw 'Payload manifest generation failed' }
$candidateId = ([string]$candidateOutput).Trim()
if ($candidateId -notmatch '^[0-9a-f]{64}$') { throw 'Payload manifest returned an invalid candidate identity' }

$innoScript = Join-Path $repositoryRoot 'packaging\installer\LimitHalo.iss'
$preflightValidator = Join-Path $repositoryRoot 'packaging\installer\Validate-PackageManifest.ps1'
Invoke-Checked $iscc @(
    ('/DSourceDir=' + $payload),
    ('/DOutputDir=' + $releaseRoot),
    ('/DCandidateId=' + $candidateId),
    ('/DProductVersion=' + $productVersion),
    ('/DPreflightValidatorPath=' + $preflightValidator),
    $innoScript
) 'Inno Setup compilation'

$setup = Join-Path $releaseRoot ('LimitHalo-' + $productVersion + '-Setup.exe')
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { throw 'Expected setup artifact is missing' }

$portableRoot = Join-Path $buildRoot 'portable'
Copy-Item -LiteralPath $payload -Destination $portableRoot -Recurse
[System.IO.File]::WriteAllText((Join-Path $portableRoot 'portable.flag'), "AILimitsWidget portable mode v1`n", [System.Text.UTF8Encoding]::new($false))
$portableZip = Join-Path $releaseRoot ('LimitHalo-' + $productVersion + '-Windows-x64.zip')
Invoke-Checked $buildPython @('-I', '-B', $releaseTools, 'zip', '--source', $portableRoot, '--output', $portableZip, '--prefix', 'LimitHalo') 'deterministic portable ZIP generation'
$releaseAgent = Join-Path $releaseRoot 'LimitHalo-Agent.ps1'
Copy-Item -LiteralPath $agentSource -Destination $releaseAgent
if ((Get-FileHash -LiteralPath $releaseAgent -Algorithm SHA256).Hash -cne (Get-FileHash -LiteralPath (Join-Path $payload 'LimitHalo-Agent.ps1') -Algorithm SHA256).Hash) {
    throw 'Standalone and payload AI agent bytes differ'
}

$releaseManifest = Join-Path $releaseRoot 'release-manifest.json'
$manifestArguments = @(
    '-I', '-B', $releaseTools, 'release-manifest',
    '--output', $releaseManifest,
    '--product', $productPath,
    '--payload-manifest', $payloadManifest,
    '--dependency-license-manifest', $dependencyLicenseManifest,
    '--source-identity', $SourceIdentity,
    '--artifact', $setup,
    '--artifact', $portableZip,
    '--artifact', $releaseAgent,
    '--input', $productPath,
    '--input', $pythonLock,
    '--input', $nativeLock,
    '--input', $innoLock,
    '--input', $innoProvenance
)
Invoke-Checked $buildPython $manifestArguments 'release manifest generation'
$sums = Join-Path $releaseRoot 'SHA256SUMS.txt'
Invoke-Checked $buildPython @('-I', '-B', $releaseTools, 'checksums', '--output', $sums, '--artifact', $setup, '--artifact', $portableZip, '--artifact', $releaseAgent, '--artifact', $releaseManifest) 'release checksum generation'
& (Join-Path $PSScriptRoot 'Verify-Release.ps1') -ReleaseRoot $releaseRoot -PythonExe $buildPython
$postBuildTree = (& $buildPython -I -B $sourceManifestTool verify --root $appSource --manifest $sourceManifest --contract-sha256 $contractSha256).Trim()
if ($LASTEXITCODE -ne 0 -or $postBuildTree -cne $manifestTree) { throw 'Source changed during the build' }

[pscustomobject]@{
    status = 'CANDIDATE_READY'
    candidateId = $candidateId
    releaseRoot = $releaseRoot
    setupSha256 = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()
    portableSha256 = (Get-FileHash -LiteralPath $portableZip -Algorithm SHA256).Hash.ToLowerInvariant()
    agentSha256 = (Get-FileHash -LiteralPath $releaseAgent -Algorithm SHA256).Hash.ToLowerInvariant()
    releaseManifestSha256 = (Get-FileHash -LiteralPath $releaseManifest -Algorithm SHA256).Hash.ToLowerInvariant()
    checksumSha256 = (Get-FileHash -LiteralPath $sums -Algorithm SHA256).Hash.ToLowerInvariant()
    signatureStatus = 'unsigned'
    installerExecutionPerformed = $false
    nativeSelfTestExecuted = [bool]$RunNativeSelfTest
    publicationPerformed = $false
}
