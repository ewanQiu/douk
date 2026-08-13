@echo off
REM Windows blocks .ps1 by default (ExecutionPolicy Restricted).
REM Batch files are exempt, so this wrapper runs setup.ps1 with Bypass.
REM Double-click this file, or run "setup.bat" from a terminal.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "_rc=%errorlevel%"
REM Pause only when double-clicked, so scripted runs don't hang waiting for a key.
REM When double-clicked, cmd's command line contains this file's full path.
REM Set DOUK_NOPAUSE=1 to never pause (for CI or "cmd /c setup.bat" from a script,
REM which otherwise looks identical to a double-click).
if defined DOUK_NOPAUSE exit /b %_rc%
echo %cmdcmdline% | find /i "%~f0" >nul
if not errorlevel 1 pause
exit /b %_rc%
