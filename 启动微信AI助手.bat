@echo off
rem WeChatAI Assistant launcher - close this window to stop (ASCII only)
cd /d "%~dp0app"
start "" http://127.0.0.1:8765/
"..untime\python.exe" main.py %*
echo.
echo Assistant stopped.
pause
