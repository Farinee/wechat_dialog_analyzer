@echo off
setlocal
cd /d "%~dp0"
if not exist "D:\conda\python.exe" (
  echo Python was not found at D:\conda\python.exe
  pause
  exit /b 1
)
"D:\conda\python.exe" "%~dp0main.py" %*
set "CHAT_EXIT_CODE=%ERRORLEVEL%"
if not "%CHAT_EXIT_CODE%"=="0" pause
exit /b %CHAT_EXIT_CODE%
