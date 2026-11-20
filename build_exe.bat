@echo off
echo SquishIT - Building .exe
echo ========================

cd /d "%~dp0"

call venv\Scripts\activate.bat

echo Installing PyInstaller...
pip install pyinstaller --quiet

echo Building...
pyinstaller --onefile ^
  --clean ^
  --name SquishIT ^
  --windowed ^
  --hidden-import PIL._tkinter_finder ^
  squishit.py

echo.
if exist dist\SquishIT.exe (
    echo SUCCESS: dist\SquishIT.exe built.
    echo Size:
    for %%F in (dist\SquishIT.exe) do echo   %%~zF bytes
    echo.
    echo Distribution layout:
    echo   dist\SquishIT.exe   ^<-- the exe
    echo   tools\ffmpeg.exe    ^<-- ship alongside
    echo   tools\ffprobe.exe   ^<-- ship alongside
    echo   ^(Ghostscript downloads itself on first run^)
) else (
    echo FAILED - check output above.
)
pause
