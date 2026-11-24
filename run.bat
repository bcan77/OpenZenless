@echo off
setlocal enabledelayedexpansion
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "DATA_DIR=%LOCALAPPDATA%\OpenZenless"
if not defined LOCALAPPDATA set "DATA_DIR=%USERPROFILE%\AppData\Local\OpenZenless"
set "VENV_DIR=%DATA_DIR%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "REQ_FILE=%HERE%\requirements.txt"

if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import PySide6, PIL, numpy, OpenGL, pygltflib" >nul 2>&1
    if !errorlevel! equ 0 goto :run_venv
)

set "SYS_PY="
where py >nul 2>&1
if %errorlevel% equ 0 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if !errorlevel! equ 0 set "SYS_PY=py -3"
)
if not defined SYS_PY (
    where python >nul 2>&1
    if %errorlevel% equ 0 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
        if !errorlevel! equ 0 set "SYS_PY=python"
    )
)
if not defined SYS_PY (
    echo [OpenZenless] Python 3.10+ not found on PATH. Install Python from https://www.python.org/downloads/ >&2
    pause
    exit /b 1
)

%SYS_PY% -c "import PySide6, PIL, numpy, OpenGL, pygltflib" >nul 2>&1
if %errorlevel% equ 0 goto :run_sys

echo [OpenZenless] Creating virtual environment and installing dependencies into "%VENV_DIR%"... >&2
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
%SYS_PY% -m venv "%VENV_DIR%"
if %errorlevel% neq 0 (
    echo [OpenZenless] Failed to create venv. >&2
    pause
    exit /b 1
)
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r "%REQ_FILE%"
if %errorlevel% neq 0 (
    echo [OpenZenless] Failed to install requirements.txt. >&2
    pause
    exit /b 1
)

:run_venv
if "%~1"=="" (
    "%VENV_PY%" "%HERE%\openzenless.py" gui
) else (
    "%VENV_PY%" "%HERE%\openzenless.py" %*
)
exit /b %errorlevel%

:run_sys
if "%~1"=="" (
    %SYS_PY% "%HERE%\openzenless.py" gui
) else (
    %SYS_PY% "%HERE%\openzenless.py" %*
)
exit /b %errorlevel%
