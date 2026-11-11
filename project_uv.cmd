@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "UV_PROJECT_ENVIRONMENT=%CD%\.venv"
set "UV_DATA_DIR=%CD%\.uv\data"
set "UV_CACHE_DIR=%CD%\.uv\cache"
set "UV_PYTHON_INSTALL_DIR=%CD%\.uv\python"
set "UV_PYTHON_BIN_DIR=%CD%\.uv\bin"
set "UV_PYTHON_INSTALL_BIN=0"
set "UV_PYTHON_INSTALL_REGISTRY=0"
set "UV_MANAGED_PYTHON=1"
set "UV_LINK_MODE=copy"
set "PYINSTALLER_CONFIG_DIR=%CD%\.uv\pyinstaller"
set "TEMP=%CD%\.uv\tmp"
set "TMP=%TEMP%"
if not exist "%TEMP%\" mkdir "%TEMP%"
if not exist "%TEMP%\" goto failed

set "UV_EXE=%CD%\.uv\bin\uv.exe"
if exist "%UV_EXE%" goto run
set "UV_EXE=uv.exe"
where uv.exe >nul 2>&1
if errorlevel 1 (
    echo uv.exe was not found. See the setup instructions in README.md.
    goto failed
)
:run
"%UV_EXE%" %*
set "UV_EXIT_CODE=%errorlevel%"
popd
exit /b %UV_EXIT_CODE%

:failed
popd
exit /b 1
