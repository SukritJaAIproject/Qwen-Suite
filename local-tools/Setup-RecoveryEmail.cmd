@echo off
title Qwen Suite - recovery e-mail for "forgot password"
rem Sets up the Gmail account that e-mails Open WebUI password-reset links (account_recovery.py --setup).
rem The App Password is typed here, hidden, and stored with Windows DPAPI for this Windows user only.
"%~dp0..\webui-venv\Scripts\python.exe" -I "%~dp0account_recovery.py" --setup
echo.
pause
