@echo off
setlocal enableextensions
cd /d "%~dp0"
rem Setup + launch. Also creates a "Dictation Typer" desktop shortcut that starts the app with no console window.

set "PY_CMD="
where py >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3"

if not defined PY_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
    echo Python was not found. Install Python 3.12+ from https://www.python.org/downloads/
    echo During setup, check "Add python.exe to PATH".
    pause
    exit /b 1
)

set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys" >nul 2>&1
    if errorlevel 1 rmdir /s /q "%VENV_DIR%"
)

if not exist "%VENV_PY%" (
    echo Creating local environment...
    %PY_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Failed to create the local environment.
        pause
        exit /b 1
    )
)

rem Install only when requirements.txt changed since the last successful install.
fc /b "%~dp0requirements.txt" "%VENV_DIR%\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    "%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b 1
    )
    copy /y "%~dp0requirements.txt" "%VENV_DIR%\requirements.installed" >nul
)

powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Dictation Typer.lnk'); $s.TargetPath = '%VENV_DIR%\Scripts\pythonw.exe'; $s.Arguments = '\"%~dp0qt_main.py\"'; $s.WorkingDirectory = '%~dp0'; $s.IconLocation = '%~dp0assets\icon.ico'; $s.Save()"

start "" "%VENV_DIR%\Scripts\pythonw.exe" "%~dp0qt_main.py"
exit /b 0
