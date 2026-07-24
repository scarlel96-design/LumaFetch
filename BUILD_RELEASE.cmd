@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where pwsh.exe >nul 2>&1
if %errorlevel%==0 (
    set "POWERSHELL_EXE=pwsh.exe"
) else (
    set "POWERSHELL_EXE=powershell.exe"
)

echo ============================================================
echo  Luma Fetch 1.13.1 - Windows Release Build
echo ============================================================
"%POWERSHELL_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_windows.ps1"
set "EXIT_CODE=%errorlevel%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo BUILD FAILED. Exit code: %EXIT_CODE%
    exit /b %EXIT_CODE%
)

echo.
echo Output: %~dp0outputs\LumaFetch-Setup-1.13.1.exe
exit /b 0
