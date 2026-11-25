@echo off
setlocal enabledelayedexpansion
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "DATA_DIR=%LOCALAPPDATA%\OpenZenless"
if not defined LOCALAPPDATA set "DATA_DIR=%USERPROFILE%\AppData\Local\OpenZenless"
set "VENV_DIR=%DATA_DIR%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe"
set "REQ_FILE=%HERE%\requirements.txt"
set "UV_DIR=%DATA_DIR%\uv"
set "UV_EXE=%UV_DIR%\uv.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import PySide6, PIL, numpy, OpenGL, pygltflib" >nul 2>&1
    if !errorlevel! equ 0 goto :ready_venv
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

if defined SYS_PY (
    %SYS_PY% -c "import PySide6, PIL, numpy, OpenGL, pygltflib" >nul 2>&1
    if %errorlevel% equ 0 goto :ready_sys
)

if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"

if defined SYS_PY (
    echo [OpenZenless] Creating virtual environment and installing dependencies into "%VENV_DIR%"... >&2
    %SYS_PY% -m venv "%VENV_DIR%"
    if !errorlevel! equ 0 if exist "%VENV_PY%" (
        "%VENV_PY%" -m pip install --upgrade pip
        "%VENV_PY%" -m pip install -r "%REQ_FILE%"
        if !errorlevel! equ 0 goto :ready_venv
    )
    echo [OpenZenless] Standard venv/pip setup incomplete; falling back to portable uv... >&2
)

if not exist "%UV_EXE%" (
    echo [OpenZenless] Bootstrapping portable uv + Python 3.12 into "%UV_DIR%"... >&2
    if not exist "%UV_DIR%" mkdir "%UV_DIR%"
    set "UV_ZIP=%DATA_DIR%\uv-win64.zip"
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile '%UV_ZIP%'; Expand-Archive -Force -Path '%UV_ZIP%' -DestinationPath '%UV_DIR%'; Remove-Item -Force -ErrorAction SilentlyContinue '%UV_ZIP%' } catch { Write-Error $_; exit 1 }"
    if !errorlevel! neq 0 (
        echo [OpenZenless] Failed to download portable uv. Install Python 3.10+ from https://www.python.org/downloads/ >&2
        if /i not "%~1"=="--no-pause" pause
        exit /b 1
    )
    for /f "delims=" %%F in ('dir /b /s "%UV_DIR%\uv.exe" 2^>nul') do (
        if /i not "%%~fF"=="%UV_EXE%" copy /y "%%~fF" "%UV_EXE%" >nul
    )
)

if not exist "%UV_EXE%" (
    echo [OpenZenless] uv.exe not found after extraction. Install Python 3.10+ from https://www.python.org/downloads/ >&2
    pause
    exit /b 1
)

echo [OpenZenless] Creating managed Python 3.12 virtual environment in "%VENV_DIR%"... >&2
"%UV_EXE%" venv --python 3.12 "%VENV_DIR%"
if %errorlevel% neq 0 (
    echo [OpenZenless] Failed to create managed Python 3.12 venv via uv. >&2
    pause
    exit /b 1
)
"%UV_EXE%" pip install --python "%VENV_PY%" -r "%REQ_FILE%"
if %errorlevel% neq 0 (
    echo [OpenZenless] Failed to install dependencies from requirements.txt. >&2
    pause
    exit /b 1
)

:ready_venv
if /i "%~1"=="--setup-only" exit /b 0
if "%~1"=="" (
    if "%OPENZENLESS_NO_CONSOLE%"=="1" if exist "%VENV_PYW%" (
        start "" "%VENV_PYW%" "%HERE%\openzenless.py" gui
        exit /b 0
    )
    "%VENV_PY%" "%HERE%\openzenless.py" gui
) else (
    "%VENV_PY%" "%HERE%\openzenless.py" %*
)
exit /b %errorlevel%

:ready_sys
if /i "%~1"=="--setup-only" exit /b 0
if "%~1"=="" (
    %SYS_PY% "%HERE%\openzenless.py" gui
) else (
    %SYS_PY% "%HERE%\openzenless.py" %*
)
exit /b %errorlevel%
