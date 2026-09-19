@echo off
if exist "%~dp0..\.env" (
  start "" "%~dp0Srez.exe" --data-dir "%~dp0.."
) else if exist "%~dp0..\flower-competitor-app\.env" (
  start "" "%~dp0Srez.exe" --data-dir "%~dp0..\flower-competitor-app"
) else (
  start "" "%~dp0Srez.exe"
)
exit /b 0
