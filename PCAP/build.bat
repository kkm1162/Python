@echo off
setlocal
cd /d "%~dp0"

echo === O-RAN Protocol Analyzer EXE 빌드 ===
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo Python을 PATH에서 찾을 수 없습니다.
    echo Python 3.10+ 설치 후 다시 실행하세요.
    pause
    exit /b 1
)

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller>=6.0

python -m PyInstaller --noconfirm --distpath dist --workpath build build_exe.spec

if errorlevel 1 (
    echo.
    echo 빌드 실패.
    pause
    exit /b 1
)

echo.
echo 빌드 완료: dist\O-RAN-Protocol-Analyzer.exe
echo.
pause
