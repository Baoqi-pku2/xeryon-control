@echo off
setlocal
cd /d "%~dp0"

set "ENV_PYTHON=%CD%\.xeryon-venv\Scripts\python.exe"
if not exist "%ENV_PYTHON%" (
    echo Run run_gui.cmd once before building the portable app.
    pause
    exit /b 1
)

"%ENV_PYTHON%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo Installing the portable build tool...
    "%ENV_PYTHON%" -m pip install pyinstaller
    if errorlevel 1 (
        echo PyInstaller installation failed.
        pause
        exit /b 1
    )
)

"%ENV_PYTHON%" -m PyInstaller --noconfirm --distpath "%CD%\release\portable" --workpath "%CD%\build\portable" "%CD%\XeryonControl.spec"

if errorlevel 1 (
    echo Portable build failed.
    pause
    exit /b 1
)

echo Portable app created: %CD%\release\portable\XeryonControl.exe
pause
