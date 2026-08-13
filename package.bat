@echo off
REM Wrapper so package.ps1 runs without changing ExecutionPolicy.
REM Usage: package.bat
REM        package.bat -IncludeCookies -IncludeDb
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0package.ps1" %*
set "_rc=%errorlevel%"
if defined DOUK_NOPAUSE exit /b %_rc%
echo %cmdcmdline% | find /i "%~f0" >nul
if not errorlevel 1 pause
exit /b %_rc%
