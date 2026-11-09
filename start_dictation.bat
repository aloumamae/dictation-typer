@echo off
setlocal enableextensions
cd /d "%~dp0"

set "PY_CMD="
where py >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3"

if not defined PY_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
    echo Python nao foi encontrado neste computador.
    echo Instale o Python 3.12+ em https://www.python.org/downloads/
    echo Durante a instalacao, marque "Add python.exe to PATH".
    pause
    exit /b 1
)

set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Criando ambiente local do Dictation Typer...
    %PY_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Falha ao criar o ambiente local.
        pause
        exit /b 1
    )
) else (
    echo Validando ambiente virtual...
    "%VENV_PY%" -c "import sys; sys.exit(0)" >nul 2>&1
    if errorlevel 1 (
        echo Ambiente virtual antigo encontrado. Recriando...
        rmdir /s /q "%VENV_DIR%"
        %PY_CMD% -m venv "%VENV_DIR%"
        if errorlevel 1 (
            echo Falha ao recriar o ambiente local.
            pause
            exit /b 1
        )
    )
)

echo Verificando dependencias...
"%VENV_PY%" -m pip install --upgrade pip >nul
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo Falha ao instalar dependencias.
    pause
    exit /b 1
)

start "" "%VENV_DIR%\Scripts\pythonw.exe" "%~dp0qt_main.py"
exit /b 0
