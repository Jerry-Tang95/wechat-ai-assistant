@echo off
rem Stop WeChatAI Assistant (ASCII only)
taskkill /F /IM python.exe >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
echo WeChatAI Assistant stopped.
pause
