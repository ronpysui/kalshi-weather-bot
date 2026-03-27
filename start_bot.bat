@echo off
:: Kalshi Bot — auto-launcher
:: Starts main.py in a loop, restarts if it crashes.
:: Registered with Windows Task Scheduler to run at login.

cd /d "%~dp0"
title Kalshi NYC Temp Bot

:loop
echo [%date% %time%] Starting Kalshi bot...
python main.py >> data\bot.log 2>&1
echo [%date% %time%] Bot exited. Restarting in 60 seconds...
timeout /t 60 /nobreak
goto loop
