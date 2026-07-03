@echo off
rem Refuge launcher (Windows) - double-click or run from a terminal.
rem Prefers windowless pythonw so no console window lingers.
cd /d "%~dp0"
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw run.py
) else (
    python run.py
)
