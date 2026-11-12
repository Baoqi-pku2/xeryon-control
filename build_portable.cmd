@echo off
setlocal
pushd "%~dp0" || exit /b 1
if not exist "build\tmp" mkdir "build\tmp"
set "TEMP=%CD%\build\tmp"
set "TMP=%TEMP%"
set "PYINSTALLER_CONFIG_DIR=%TEMP%\pyinstaller"
uv run --group build python -m PyInstaller --noconfirm --distpath release\portable --workpath build\portable XeryonControl.spec
if errorlevel 1 goto done
uv run --group build python tools\verify_portable.py release\portable\XeryonControl.exe --output build\portable\verification.json >build\portable\verification.log
if errorlevel 1 goto failed
echo %CD%\release\portable\XeryonControl.exe
goto done
:failed
type build\portable\verification.log
popd
exit /b 1
:done
set "result=%errorlevel%"
popd
exit /b %result%
