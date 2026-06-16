# DashStation GUI (.exe) 빌드 스크립트
#
# 사용 예:
#   .\build_gui.ps1
#   .\build_gui.ps1 -PyFile "Dashstation_final.py" -ExeName "DashStation" -Icon "app_icon1.ico" -OneFile
#   .\build_gui.ps1 -PyFile "Dashstation_final.py" -ExeName "DashStation_v9.1" -Icon "app_icon1.ico" -OneFile:$false
#   .\build_gui.ps1 -UseSpec -SpecFile "DashStation_v9.1.spec"

param(
    [string]$PyFile   = "Dashstation_final.py",
    [string]$ExeName  = "DashStation",
    [string]$Icon     = "app_icon1.ico",
    [switch]$OneFile,
    [switch]$UseSpec,
    [string]$SpecFile = "DashStation_v9.1.spec"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# -OneFile 미지정 시 기본값 true
if (-not $PSBoundParameters.ContainsKey("OneFile")) {
    $OneFile = $true
}

Write-Host "=== DashStation GUI Build ===" -ForegroundColor Cyan

function Stop-DashStationProcesses {
    $targets = @($ExeName, "DashStation", "DashStation_v9.1")
    foreach ($name in ($targets | Select-Object -Unique)) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object {
            Write-Host "실행 중인 프로세스 종료: $($_.ProcessName) (PID $($_.Id))" -ForegroundColor Yellow
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 500
}

function Unlock-ExistingExe {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    try {
        Remove-Item $Path -Force -ErrorAction Stop
        return
    } catch {
        $backup = "$Path.old"
        Write-Host "기존 exe 잠금 감지. 백업 이름으로 이동 시도: $backup" -ForegroundColor Yellow
        if (Test-Path $backup) { Remove-Item $backup -Force -ErrorAction SilentlyContinue }
        Move-Item $Path $backup -Force
    }
}

Stop-DashStationProcesses
Unlock-ExistingExe (Join-Path $PSScriptRoot "dist\$ExeName.exe")

python -m PyInstaller --version 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller 설치 중..." -ForegroundColor Yellow
    python -m pip install pyinstaller
}

if ($UseSpec) {
    if (-not (Test-Path $SpecFile)) {
        throw "spec 파일을 찾을 수 없습니다: $SpecFile"
    }
    Write-Host "spec 파일로 빌드: $SpecFile" -ForegroundColor Yellow
    python -m PyInstaller --noconfirm --clean $SpecFile
    $ExePath = Join-Path $PSScriptRoot "dist\$ExeName.exe"
    if (-not (Test-Path $ExePath)) {
        $ExePath = Get-ChildItem -Path ".\dist\*.exe" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1 -ExpandProperty FullName
    }
} else {
    if (-not (Test-Path $PyFile)) {
        throw "Python 파일을 찾을 수 없습니다: $PyFile"
    }
    if (-not (Test-Path $Icon)) {
        throw "아이콘 파일을 찾을 수 없습니다: $Icon"
    }

    Write-Host "Python : $PyFile"
    Write-Host "Exe    : $ExeName.exe"
    Write-Host "Icon   : $Icon"
    Write-Host "Mode   : $(if ($OneFile) { 'onefile (단일 exe)' } else { 'onedir (폴더)' })"

    $pyArgs = @(
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name", $ExeName,
        "--icon", $Icon,
        "--add-data", "${Icon};."
    )

    if ($OneFile) {
        $pyArgs += "--onefile"
    } else {
        $pyArgs += "--onedir"
    }

    $pyArgs += $PyFile

    python -m PyInstaller @pyArgs
    $ExePath = Join-Path $PSScriptRoot "dist\$ExeName.exe"
    if (-not (Test-Path $ExePath)) {
        $ExePath = Join-Path $PSScriptRoot "dist\$ExeName\$ExeName.exe"
    }
}

if ($ExePath -and (Test-Path $ExePath)) {
    Write-Host ""
    Write-Host "빌드 완료: $ExePath" -ForegroundColor Green
    Write-Host "실행: Start-Process '$ExePath'" -ForegroundColor Green
} else {
    throw "빌드는 완료됐지만 exe 파일을 찾지 못했습니다. dist 폴더를 확인하세요."
}
