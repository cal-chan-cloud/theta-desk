@echo off
REM ---------------------------------------------------------------------------
REM Theta Desk daily refresh.  Registered as a Task Scheduler job by
REM register_task.ps1 -- see the README for the exact command and the quoting
REM rules that matter.
REM
REM The interpreter is pinned to the python.org build rather than "python",
REM because Task Scheduler resolves PATH differently from an interactive shell
REM and the Microsoft Store build is not what it finds.  Everything here runs on
REM flask + requests + the standard library, both of which that build has.
REM ---------------------------------------------------------------------------
setlocal
set PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe
if not exist "%PY%" set PY=python

cd /d "%~dp0"
if not exist "data\logs" mkdir "data\logs"

set LOG=data\logs\daily.log
echo. >> "%LOG%"
echo ======================================================== >> "%LOG%"
echo Theta Desk daily run started %DATE% %TIME% >> "%LOG%"

"%PY%" pipeline.py >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

REM pipeline.py already re-reads the database and exits non-zero if the data is
REM not actually there, so this exit code is worth trusting -- a fetch that
REM silently returned empty JSON does NOT exit 0.
if %RC% NEQ 0 (
  echo FAILED with exit code %RC% >> "%LOG%"
  exit /b %RC%
)

echo Completed OK %DATE% %TIME% >> "%LOG%"
exit /b 0
