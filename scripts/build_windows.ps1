[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$IsccPath = "",
    [switch]$SkipBootstrap
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProgressPreference = "SilentlyContinue"

$ExpectedVersion = "1.14.0"
$ExpectedInstaller = "LumaFetch-Setup-$ExpectedVersion.exe"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-PythonCommand {
    param([string]$Requested)

    if ($Requested) {
        return @($Requested)
    }

    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($selector in @("-3.13", "-3.12")) {
            & py $selector -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return @("py", $selector)
            }
        }
    }

    foreach ($candidate in @("python", "python3")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            & $candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return @($candidate)
            }
        }
    }

    throw "Python 3.12 이상을 찾을 수 없습니다. Python x64를 설치한 뒤 다시 실행하세요."
}

function Invoke-Python {
    param(
        [string[]]$Command,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )
    $executable = $Command[0]
    $prefix = if ($Command.Length -gt 1) { $Command[1..($Command.Length - 1)] } else { @() }
    & $executable @prefix @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python 명령이 실패했습니다: $executable $($prefix -join ' ') $($Arguments -join ' ')"
    }
}

function Resolve-Iscc {
    param([string]$Requested)

    $candidates = @()
    if ($Requested) { $candidates += $Requested }
    if (${env:ProgramFiles(x86)}) { $candidates += "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
    if ($env:ProgramFiles) { $candidates += "$env:ProgramFiles\Inno Setup 6\ISCC.exe" }
    if ($env:LOCALAPPDATA) { $candidates += "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" }
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }

    foreach ($candidate in $candidates | Select-Object -Unique) {
        if ($candidate -and (Test-Path $candidate)) {
            return (Resolve-Path $candidate).Path
        }
    }
    return $null
}

function Install-InnoSetup {
    Write-Step "Inno Setup 6 설치"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install --id JRSoftware.InnoSetup --exact --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
        if ($LASTEXITCODE -eq 0) { return }
        Write-Warning "winget 설치가 실패했습니다. Chocolatey를 확인합니다."
    }
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        & choco install innosetup --no-progress -y
        if ($LASTEXITCODE -eq 0) { return }
    }
    throw "Inno Setup 6을 자동 설치하지 못했습니다. Inno Setup 6 설치 후 다시 실행하세요."
}

function Assert-File([string]$Path, [string]$Description) {
    if (-not (Test-Path $Path -PathType Leaf)) {
        throw "$Description 파일이 없습니다: $Path"
    }
}

Write-Step "필수 소스 및 자산 확인"
foreach ($required in @(
    "app.py",
    "requirements-dev.txt",
    "LumaFetch.spec",
    "installer\LumaFetch.iss",
    "installer\LumaFetch.ico",
    "installer\version_info.txt"
)) {
    Assert-File (Join-Path $root $required) $required
}

$pythonCommand = Resolve-PythonCommand -Requested $Python
$venvDir = Join-Path $root ".venv-build"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (-not $SkipBootstrap) {
    Write-Step "격리된 빌드 가상환경 준비"
    if (Test-Path $venvPython) {
        & $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Remove-Item -Recurse -Force $venvDir
        }
    }
    if (-not (Test-Path $venvPython)) {
        Invoke-Python -Command $pythonCommand -Arguments @("-m", "venv", $venvDir)
    }
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip 업그레이드에 실패했습니다." }
    & $venvPython -m pip install -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw "Python 빌드 의존성 설치에 실패했습니다." }
} elseif (-not (Test-Path $venvPython)) {
    throw "-SkipBootstrap을 사용했지만 .venv-build가 존재하지 않습니다."
}

Write-Step "버전 계약 확인"
$releaseContractFiles = @(
    @{ Path = "app.py"; Pattern = 'APP_VERSION = "1\.14\.0"' },
    @{ Path = "lumafetch\__init__.py"; Pattern = '__version__ = "1\.14\.0"' },
    @{ Path = "installer\LumaFetch.iss"; Pattern = '#define MyAppVersion "1\.14\.0"' },
    @{ Path = "installer\LumaFetch.iss"; Pattern = 'OutputBaseFilename=LumaFetch-Setup-1\.14\.0' }
)
foreach ($contract in $releaseContractFiles) {
    $content = Get-Content (Join-Path $root $contract.Path) -Raw -Encoding UTF8
    if ($content -notmatch $contract.Pattern) {
        throw "버전 계약 불일치: $($contract.Path) / $($contract.Pattern)"
    }
}

Write-Step "테스트 및 정적 컴파일 검사"
& $venvPython -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "테스트가 실패했습니다." }
& $venvPython -m compileall -q app.py lumafetch tests
if ($LASTEXITCODE -ne 0) { throw "compileall 검사가 실패했습니다." }

Write-Step "기존 빌드 산출물 정리"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
    (Join-Path $root "work\dist"), `
    (Join-Path $root "work\pyinstaller-build"), `
    (Join-Path $root "outputs")
New-Item -ItemType Directory -Force -Path (Join-Path $root "outputs") | Out-Null

Write-Step "PyInstaller Windows 애플리케이션 빌드"
& $venvPython -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $root "work\dist") `
    --workpath (Join-Path $root "work\pyinstaller-build") `
    (Join-Path $root "LumaFetch.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 빌드가 실패했습니다." }

$appExe = Join-Path $root "work\dist\LumaFetch\LumaFetch.exe"
Assert-File $appExe "LumaFetch 실행"
$appVersion = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($appExe)
if ($appVersion.FileVersion -notlike "1.14.0*") {
    throw "LumaFetch.exe 파일 버전이 올바르지 않습니다: $($appVersion.FileVersion)"
}

$iscc = Resolve-Iscc -Requested $IsccPath
if (-not $iscc -and -not $SkipBootstrap) {
    Install-InnoSetup
    $iscc = Resolve-Iscc -Requested $IsccPath
}
if (-not $iscc) {
    throw "Inno Setup 6 컴파일러(ISCC.exe)를 찾을 수 없습니다."
}

Write-Step "Inno Setup 설치 파일 컴파일"
& $iscc (Join-Path $root "installer\LumaFetch.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 컴파일이 실패했습니다." }

$installer = Join-Path $root "outputs\$ExpectedInstaller"
Assert-File $installer "설치"
$installerInfo = Get-Item $installer
if ($installerInfo.Length -lt 1MB) {
    throw "설치 파일 크기가 비정상적으로 작습니다: $($installerInfo.Length) bytes"
}
$installerVersion = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($installer)
if ($installerVersion.FileVersion -notlike "1.14.0*") {
    throw "설치 파일 버전이 올바르지 않습니다: $($installerVersion.FileVersion)"
}

Write-Step "해시 및 빌드 보고서 생성"
$hash = Get-FileHash $installer -Algorithm SHA256
"$($hash.Hash) *$ExpectedInstaller" | Set-Content -Encoding ASCII (Join-Path $root "outputs\SHA256SUMS.txt")
$pythonVersion = & $venvPython --version 2>&1
$buildInfo = @(
    "Luma Fetch $ExpectedVersion",
    "BuiltAtUtc=$([DateTime]::UtcNow.ToString('o'))",
    "Python=$pythonVersion",
    "PyInstaller=$(& $venvPython -m PyInstaller --version)",
    "InnoSetup=$iscc",
    "AppFileVersion=$($appVersion.FileVersion)",
    "InstallerFileVersion=$($installerVersion.FileVersion)",
    "InstallerSize=$($installerInfo.Length)",
    "SHA256=$($hash.Hash)",
    "Tests=PASS"
)
$buildInfo | Set-Content -Encoding UTF8 (Join-Path $root "outputs\BUILD_INFO.txt")

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Luma Fetch $ExpectedVersion 빌드 완료" -ForegroundColor Green
Write-Host " Installer: $installer"
Write-Host " SHA256:   $($hash.Hash)"
Write-Host "============================================================" -ForegroundColor Green
