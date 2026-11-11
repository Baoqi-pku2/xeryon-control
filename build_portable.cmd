@echo off
setlocal
call "%~dp0project_uv.cmd" run --locked --group build python -m PyInstaller --noconfirm --distpath release/portable --workpath build/portable XeryonControl.spec
if errorlevel 1 exit /b %errorlevel%
echo Portable app created: %~dp0release\portable\XeryonControl.exe
