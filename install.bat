@echo off
echo SquishIT - First-time setup
echo ============================
echo.
echo This runs once on each new machine.
echo.

cd /d "%~dp0"

REM --- Ghostscript ---
echo Checking Ghostscript...
where gswin64c >nul 2>&1
if %errorlevel%==0 (
    echo   Ghostscript already installed.
) else (
    echo   Installing Ghostscript via winget...
    winget install --id ArtifexSoftware.GhostScript --accept-package-agreements --accept-source-agreements
    if %errorlevel% neq 0 (
        echo   winget failed. Trying direct download...
        powershell -Command "Invoke-WebRequest -Uri 'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10031/gs10031w64.exe' -OutFile '%TEMP%\gs_installer.exe' -UseBasicParsing; Start-Process '%TEMP%\gs_installer.exe' -ArgumentList '/S' -Wait"
    )
    echo   Ghostscript installed.
)

REM --- Python venv ---
echo.
echo Setting up Python environment...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found. Install Python 3.10+ from python.org and re-run this.
    pause
    exit /b 1
)

if not exist venv (
    python -m venv venv
)

call venv\Scripts\activate.bat
pip install pillow pikepdf pyinstaller --quiet

echo.
echo Setup complete.
echo.
echo To RUN the app from source:   run.bat
echo To BUILD the .exe:            build_exe.bat
echo.
pause
