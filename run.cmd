@echo off
setlocal
pushd "%~dp0" || exit /b 1
uv run python -m xeryon_control %*
set "result=%errorlevel%"
popd
exit /b %result%
