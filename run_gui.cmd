@echo off
setlocal
cd /d "%~dp0"

set "ENV_DIR=%CD%\.xeryon-venv"
set "ENV_PYTHON=%ENV_DIR%\Scripts\python.exe"

if exist "%ENV_PYTHON%" (
    "%ENV_PYTHON%" -c "import sys" >nul 2>&1
    if not errorlevel 1 goto dependencies
)

call :find_python
if not defined BASE_PYTHON (
    echo Python 3.11 or newer was not found.
    echo Install Python, or use the portable XeryonControl.exe build.
    pause
    exit /b 1
)

echo Creating the local Xeryon environment...
"%BASE_PYTHON%" %BASE_ARGS% -m venv --clear "%ENV_DIR%"
if errorlevel 1 (
    echo Failed to create the Python environment.
    pause
    exit /b 1
)

:dependencies
"%ENV_PYTHON%" -c "import canopen, can, serial" >nul 2>&1
if errorlevel 1 (
    echo Installing Xeryon dependencies...
    "%ENV_PYTHON%" -m pip install -r "%CD%\requirements.txt"
    if errorlevel 1 (
        echo Dependency installation failed. Check the internet connection.
        pause
        exit /b 1
    )
)

echo Starting Xeryon Control GUI...
"%ENV_PYTHON%" "%CD%\examples\control_gui.py"
exit /b %errorlevel%

:find_python
set "BASE_PYTHON="
set "BASE_ARGS="

if exist "%USERPROFILE%\miniconda3\python.exe" (
    set "BASE_PYTHON=%USERPROFILE%\miniconda3\python.exe"
    exit /b 0
)
if exist "%USERPROFILE%\anaconda3\python.exe" (
    set "BASE_PYTHON=%USERPROFILE%\anaconda3\python.exe"
    exit /b 0
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "BASE_PYTHON=py"
        set "BASE_ARGS=-3"
        exit /b 0
    )
)

where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "BASE_PYTHON=python"
)
exit /b 0
