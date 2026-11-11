@echo off
setlocal
call "%~dp0project_uv.cmd" run --locked python -m xeryon_control %*
exit /b %errorlevel%
