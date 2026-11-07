param([switch]$SkipInstaller)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
$env:UV_CACHE_DIR = Join-Path $projectRoot 'build\uv-cache'

$versionMatch = [regex]::Match((Get-Content -LiteralPath (Join-Path $projectRoot 'pyproject.toml') -Raw), '(?m)^version = "([0-9]+\.[0-9]+\.[0-9]+)"\r?$')
if (-not $versionMatch.Success) { throw 'Could not read package version.' }
$appVersion = $versionMatch.Groups[1].Value
$releaseDir = Join-Path $projectRoot ("release\IndexTTS-VoiceApp-v{0}" -f $appVersion)
$env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot 'build\pyinstaller-cache'
$env:TEMP = Join-Path $projectRoot ("build\tmp-v{0}" -f $appVersion)
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required to build the release.'
}

$buildEnvironment = Join-Path $projectRoot ("build\frontend-venv-v{0}" -f $appVersion)
$buildPython = Join-Path $buildEnvironment 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    $previousBuildPython = Join-Path $projectRoot 'build\frontend-venv\Scripts\python.exe'
    $pythonForBuild = if (Test-Path -LiteralPath $previousBuildPython) { $previousBuildPython } else { '3.11.13' }
    uv venv --python $pythonForBuild $buildEnvironment
    if ($LASTEXITCODE -ne 0) { throw 'Could not create build environment.' }
}
uv pip install --python $buildPython -r (Join-Path $projectRoot 'packaging\frontend-requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install frontend build dependencies.' }

$wheelDir = Join-Path $projectRoot 'build\wheels'
uv build --wheel --out-dir $wheelDir
if ($LASTEXITCODE -ne 0) { throw 'Could not build backend wheel.' }

$workDir = Join-Path $projectRoot ("build\pyinstaller-v{0}" -f $appVersion)
& $buildPython -m PyInstaller `
    --noconfirm --clean `
    --distpath $releaseDir --workpath $workDir `
    (Join-Path $projectRoot 'packaging\voice_app.spec')
if ($LASTEXITCODE -ne 0) { throw 'Could not build frontend executable.' }

$bundleDir = Join-Path $releaseDir 'IndexTTSVoiceApp'
# PyInstaller may pick up a third-party icuuc.dll from PATH. Qt uses Windows'
# system ICU here; the unrelated copy breaks QtCore with WinError 127.
foreach ($name in @('icuuc.dll', 'icudt78.dll')) {
    $foreignDll = Join-Path (Join-Path $bundleDir '_internal') $name
    if (Test-Path -LiteralPath $foreignDll) {
        Remove-Item -LiteralPath $foreignDll
    }
}
$wheel = Get-ChildItem -LiteralPath $wheelDir -Filter "index_tts_voice_app-$appVersion-*.whl" -File |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $wheel) { throw 'Backend wheel was not found.' }
Copy-Item -LiteralPath $wheel.FullName -Destination $bundleDir
$uvCommand = Get-Command uv
Copy-Item -LiteralPath $uvCommand.Source -Destination (Join-Path $bundleDir 'uv.exe')
$driverCache = Join-Path $projectRoot 'build\VBCABLE_Driver_Pack45.zip'
if (Test-Path -LiteralPath $driverCache) {
    $driverBundle = Join-Path $bundleDir 'drivers'
    New-Item -ItemType Directory -Path $driverBundle -Force | Out-Null
    Copy-Item -LiteralPath $driverCache -Destination $driverBundle
}
# bundle_driver always checks the pinned size/hash, including cached archives.
& $buildPython -m voice_app.driver_setup --bundle (Join-Path $bundleDir 'drivers')
if ($LASTEXITCODE -ne 0) { throw 'Could not bundle the verified official VB-CABLE package.' }
& $buildPython -m voice_app.engine_setup --bundle-source (Join-Path $bundleDir 'engine-source')
if ($LASTEXITCODE -ne 0) { throw 'Could not bundle the verified IndexTTS source archive.' }
Copy-Item -LiteralPath (Join-Path $projectRoot 'THIRD_PARTY.md') -Destination $bundleDir
Copy-Item -LiteralPath (Join-Path $projectRoot 'packaging\backend-constraints.txt') -Destination $bundleDir
& $buildPython (Join-Path $projectRoot 'packaging\prepare_release.py') --bundle $bundleDir
if ($LASTEXITCODE -ne 0) { throw 'Third-party source/license preparation or package audit failed.' }

$zipPath = Join-Path $releaseDir ("IndexTTSVoiceApp-portable-{0}.zip" -f $appVersion)
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath }
Compress-Archive -LiteralPath $bundleDir -DestinationPath $zipPath
Write-Output "Portable package: $zipPath"

if (-not $SkipInstaller) {
    $compilerCandidates = @(Join-Path $projectRoot 'build\inno\ISCC.exe')
    if (${env:ProgramFiles(x86)}) {
        $compilerCandidates += Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
    }
    if ($env:ProgramFiles) {
        $compilerCandidates += Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'
    }
    $compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if ($compiler) {
        & $compiler '/Q' "/DBuildVersion=$appVersion" (Join-Path $projectRoot 'packaging\installer.iss')
        if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed.' }
    } else {
        Write-Output 'Inno Setup 6 was not found. Portable ZIP is ready; install Inno Setup and rerun to build Setup.exe.'
    }
}
