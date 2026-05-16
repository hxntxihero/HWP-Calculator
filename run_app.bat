@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD=python"
    )
)

if "%PYTHON_CMD%"=="" (
    echo Python was not found.
    echo.
    echo Install Python 3.10 or newer from:
    echo https://www.python.org/downloads/
    echo.
    echo During installation, enable "Add python.exe to PATH".
    pause
    exit /b 1
)

echo Installing/updating required libraries...
%PYTHON_CMD% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency installation failed. Check your internet connection and try again.
    pause
    exit /b 1
)

echo Starting HWBOT HWP Calculator...
%PYTHON_CMD% main.py
if errorlevel 1 (
    echo.
    echo The app closed with an error. Read the message above, then try again.
    pause
    exit /b 1
)
