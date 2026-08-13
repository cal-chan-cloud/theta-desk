@echo off
REM Start the Theta Desk web server on http://127.0.0.1:5058
setlocal
set PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe
if not exist "%PY%" set PY=python
cd /d "%~dp0"
echo Theta Desk starting on http://127.0.0.1:5058
"%PY%" app.py
