@echo off
:: Kalshi Dashboard — Flask web server
:: Registered with Windows Task Scheduler to run at login.

cd /d "%~dp0"
title Kalshi Dashboard
python web\app.py >> data\dashboard.log 2>&1
