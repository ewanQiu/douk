@echo off
REM Run douk without setting up an alias, from cmd or PowerShell:
REM     .\douk verify
REM     .\douk sync "https://www.tiktok.com/@user/video/123"
REM %~dp0 is this file's own folder, so it works no matter where you cd from.
"%~dp0.venv\Scripts\python.exe" "%~dp0douk.py" %*
