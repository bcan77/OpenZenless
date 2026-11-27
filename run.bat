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
set "SETUP_LOG=%DATA_DIR%\setup.log"

set "REPAIR_MODE=0"
set "SETUP_ONLY=0"
set "NO_PAUSE=0"
set "APP_ARGS="
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--repair" (
    set "REPAIR_MODE=1"
    shift
    goto :parse_args
)
if /i "%~1"=="--setup-only" (
    set "SETUP_ONLY=1"
    shift
    goto :parse_args
)
if /i "%~1"=="--no-pause" (
    set "NO_PAUSE=1"
    shift
    goto :parse_args
)
set "APP_ARGS=%APP_ARGS% %1"
shift
goto :parse_args
:args_done

if not exist "%DATA_DIR%" mkdir "%DATA_DIR%" 2>nul

if "%REPAIR_MODE%"=="1" (
    echo [OpenZenless] Repair mode: cleaning existing virtual environment... >&2
    if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%" 2>nul
)

rem Check Visual C++ 2015-2022 Redistributable (required by PySide6 & Python 3.12+ C extensions)
set "SYS32=%SystemRoot%\System32"
if not defined SystemRoot set "SYS32=C:\Windows\System32"
if not exist "%SYS32%\vcruntime140_1.dll" (
    if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64" (
        echo [OpenZenless] Checking Microsoft Visual C++ Redistributable x64... >&2
        where winget >nul 2>&1
        if !errorlevel! equ 0 (
            winget install --silent --accept-package-agreements --accept-source-agreements -e --id Microsoft.VCRedist.2015+.x64 >nul 2>&1
        )
        if not exist "%SYS32%\vcruntime140_1.dll" (
            set "VCREDIST_EXE=%DATA_DIR%\vc_redist.x64.exe"
            where curl.exe >nul 2>&1
            if !errorlevel! equ 0 (
                curl.exe -fsSL -L --retry 2 "https://aka.ms/vs/17/release/vc_redist.x64.exe" -o "!VCREDIST_EXE!" >nul 2>&1
            ) else (
                powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile $env:VCREDIST_EXE } catch { exit 1 }" >nul 2>&1
            )
            if exist "!VCREDIST_EXE!" (
                "!VCREDIST_EXE!" /install /quiet /norestart >nul 2>&1
                del /f /q "!VCREDIST_EXE!" >nul 2>&1
            )
        )
        if not exist "%SYS32%\vcruntime140_1.dll" (
            echo [OpenZenless WARN] vcruntime140_1.dll still not detected; PySide6 may require VC++ 2015-2022 x64 Redistributable. >&2
        )
    )
)

set "PY_CHECK=import sys, PySide6, PIL, numpy, OpenGL, pygltflib; __import__('zstandard') if sys.version_info < (3, 14) else __import__('compression.zstd')"

if "%REPAIR_MODE%"=="0" if exist "%VENV_PY%" (
    "%VENV_PY%" -c "%PY_CHECK%" >nul 2>&1
    if !errorlevel! equ 0 goto :ready_venv
)

rem Probe system Python >= 3.10 while ignoring Microsoft Store WindowsApps shim stubs
set "SYS_PY="
where py >nul 2>&1
if %errorlevel% equ 0 (
    py -3 -c "import sys; raise SystemExit(0 if (sys.version_info >= (3, 10) and 'WindowsApps' not in sys.executable) else 1)" >nul 2>&1
    if !errorlevel! equ 0 set "SYS_PY=py -3"
)
if not defined SYS_PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined SYS_PY (
            echo "%%~fP" | findstr /i "WindowsApps" >nul
            if !errorlevel! neq 0 (
                "%%~fP" -c "import sys; raise SystemExit(0 if (sys.version_info >= (3, 10) and 'WindowsApps' not in sys.executable) else 1)" >nul 2>&1
                if !errorlevel! equ 0 set "SYS_PY=%%~fP"
            )
        )
    )
)

if "%REPAIR_MODE%"=="0" if defined SYS_PY (
    "%SYS_PY%" -c "%PY_CHECK%" >nul 2>&1
    if !errorlevel! equ 0 goto :ready_sys
)

if defined SYS_PY (
    echo [OpenZenless] Creating virtual environment and installing dependencies into "%VENV_DIR%"... >&2
    if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%" 2>nul
    "%SYS_PY%" -m venv "%VENV_DIR%" > "%SETUP_LOG%" 2>&1
    if !errorlevel! equ 0 if exist "%VENV_PY%" (
        "%VENV_PY%" -m pip --version >nul 2>&1
        if !errorlevel! neq 0 (
            "%VENV_PY%" -m ensurepip --upgrade >> "%SETUP_LOG%" 2>&1
        )
        "%VENV_PY%" -m pip install --upgrade pip >> "%SETUP_LOG%" 2>&1
        "%VENV_PY%" -m pip install -r "%REQ_FILE%" >> "%SETUP_LOG%" 2>&1
        if !errorlevel! neq 0 (
            echo [OpenZenless] Retrying pip install with --no-cache-dir... >&2
            "%VENV_PY%" -m pip install --no-cache-dir -r "%REQ_FILE%" >> "%SETUP_LOG%" 2>&1
        )
        "%VENV_PY%" -c "%PY_CHECK%" >nul 2>&1
        if !errorlevel! equ 0 goto :ready_venv
    )
    echo [OpenZenless] Standard venv/pip setup incomplete; falling back to portable uv... >&2
)

rem Determine Windows architecture for portable uv (AMD64 vs ARM64)
set "WIN_ARCH=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "WIN_ARCH=%PROCESSOR_ARCHITEW6432%"
set "UV_ZIP_NAME=uv-x86_64-pc-windows-msvc.zip"
if /i "%WIN_ARCH%"=="ARM64" set "UV_ZIP_NAME=uv-aarch64-pc-windows-msvc.zip"
set "UV_URL=https://github.com/astral-sh/uv/releases/latest/download/%UV_ZIP_NAME%"

if not exist "%UV_EXE%" (
    echo [OpenZenless] Bootstrapping portable uv + Python 3.12 (%WIN_ARCH%) into "%UV_DIR%"... >&2
    if not exist "%UV_DIR%" mkdir "%UV_DIR%"
    set "UV_ZIP=%DATA_DIR%\uv-win.zip"
    set "UV_DL_OK=0"
    where curl.exe >nul 2>&1
    if !errorlevel! equ 0 (
        curl.exe -fsSL -L --retry 3 "%UV_URL%" -o "%UV_ZIP%" >> "%SETUP_LOG%" 2>&1
        if !errorlevel! equ 0 if exist "%UV_ZIP%" set "UV_DL_OK=1"
    )
    if "!UV_DL_OK!"=="0" (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri $env:UV_URL -OutFile $env:UV_ZIP } catch { Write-Error $_; exit 1 }" >> "%SETUP_LOG%" 2>&1
        if !errorlevel! equ 0 if exist "%UV_ZIP%" set "UV_DL_OK=1"
    )
    if "!UV_DL_OK!"=="0" (
        echo [OpenZenless] Failed to download portable uv. See "%SETUP_LOG%" or install Python 3.10+ from https://www.python.org/downloads/ >&2
        if "%NO_PAUSE%"=="0" pause
        exit /b 1
    )

    set "UV_EXT_OK=0"
    where tar.exe >nul 2>&1
    if !errorlevel! equ 0 (
        tar.exe -xf "%UV_ZIP%" -C "%UV_DIR%" >> "%SETUP_LOG%" 2>&1
        if !errorlevel! equ 0 set "UV_EXT_OK=1"
    )
    if "!UV_EXT_OK!"=="0" (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Add-Type -AssemblyName System.IO.Compression.FileSystem; try { Expand-Archive -Force -LiteralPath $env:UV_ZIP -DestinationPath $env:UV_DIR } catch { Write-Error $_; exit 1 }" >> "%SETUP_LOG%" 2>&1
        if !errorlevel! equ 0 set "UV_EXT_OK=1"
    )
    del /f /q "%UV_ZIP%" >nul 2>&1

    for /f "delims=" %%F in ('dir /b /s "%UV_DIR%\uv.exe" 2^>nul') do (
        if /i not "%%~fF"=="%UV_EXE%" copy /y "%%~fF" "%UV_EXE%" >nul
    )
)

if not exist "%UV_EXE%" (
    echo [OpenZenless] uv.exe not found after extraction. See "%SETUP_LOG%" or install Python 3.10+ from https://www.python.org/downloads/ >&2
    if "%NO_PAUSE%"=="0" pause
    exit /b 1
)

echo [OpenZenless] Creating managed Python 3.12 virtual environment in "%VENV_DIR%"... >&2
"%UV_EXE%" venv --clear --python 3.12 "%VENV_DIR%" >> "%SETUP_LOG%" 2>&1
if %errorlevel% neq 0 (
    echo [OpenZenless] Failed to create managed Python 3.12 venv via uv. See "%SETUP_LOG%". >&2
    if "%NO_PAUSE%"=="0" pause
    exit /b 1
)
"%UV_EXE%" pip install --python "%VENV_PY%" -r "%REQ_FILE%" >> "%SETUP_LOG%" 2>&1
if %errorlevel% neq 0 (
    echo [OpenZenless] Retrying uv pip install with --no-cache... >&2
    "%UV_EXE%" pip install --no-cache --python "%VENV_PY%" -r "%REQ_FILE%" >> "%SETUP_LOG%" 2>&1
)
"%VENV_PY%" -c "%PY_CHECK%" >> "%SETUP_LOG%" 2>&1
if %errorlevel% neq 0 (
    echo [OpenZenless] Failed to verify installed dependencies from requirements.txt. See "%SETUP_LOG%". >&2
    if "%NO_PAUSE%"=="0" pause
    exit /b 1
)

:ready_venv
if "%SETUP_ONLY%"=="1" exit /b 0
for /f "tokens=*" %%A in ("%APP_ARGS%") do set "APP_ARGS=%%A"
if not defined APP_ARGS (
    if "%OPENZENLESS_NO_CONSOLE%"=="1" if exist "%VENV_PYW%" (
        start "" "%VENV_PYW%" "%HERE%\openzenless.py" gui
        exit /b 0
    )
    "%VENV_PY%" "%HERE%\openzenless.py" gui
) else (
    "%VENV_PY%" "%HERE%\openzenless.py" %APP_ARGS%
)
exit /b %errorlevel%

:ready_sys
if "%SETUP_ONLY%"=="1" exit /b 0
for /f "tokens=*" %%A in ("%APP_ARGS%") do set "APP_ARGS=%%A"
if not defined APP_ARGS (
    "%SYS_PY%" "%HERE%\openzenless.py" gui
) else (
    "%SYS_PY%" "%HERE%\openzenless.py" %APP_ARGS%
)
exit /b %errorlevel%
