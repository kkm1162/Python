@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 사용 예:
REM   build_gui.bat
REM   build_gui.bat -PyFile "Dashstation_final.py" -ExeName "DashStation_v9.1" -Icon "app_icon1.ico" -OneFile
REM   build_gui.bat -PyFile "Dashstation_final.py" -ExeName "DashStation" -Icon "app_icon1.ico" -OneFile:$false

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_gui.ps1" %*

if errorlevel 1 pause
