@echo off
setlocal EnableExtensions

for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"

setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1

set "EXIT_CODE=0"
set "PAUSE_ON_ERROR=0"
if "%~1"=="" if not defined CI if not defined FCS_NO_PAUSE call :detect_double_click

set "MODE=mock"
set "NEED_DEV_DEPS=0"
set "EXTRA_ARGS="

set "ARG1=%~1"
if not defined ARG1 goto :after_mode_parse

if /i "%~1"=="help" goto :show_help
if /i "%~1"=="-h" goto :show_help
if /i "%~1"=="--help" goto :show_help
if "%~1"=="/?" goto :show_help

if /i "%~1"=="mock" (
    set "MODE=mock"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="serve" (
    set "MODE=mock"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="run" (
    set "MODE=mock"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="live" (
    set "MODE=live"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="--live" (
    set "MODE=live"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="pytest" (
    set "MODE=pytest"
    set "NEED_DEV_DEPS=1"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="test" (
    set "MODE=pytest"
    set "NEED_DEV_DEPS=1"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="tests" (
    set "MODE=pytest"
    set "NEED_DEV_DEPS=1"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="--pytest" (
    set "MODE=pytest"
    set "NEED_DEV_DEPS=1"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="lint" (
    set "MODE=lint"
    set "NEED_DEV_DEPS=1"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="ruff" (
    set "MODE=lint"
    set "NEED_DEV_DEPS=1"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="check" (
    set "MODE=check"
    set "NEED_DEV_DEPS=1"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="ci" (
    set "MODE=check"
    set "NEED_DEV_DEPS=1"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="setup" (
    set "MODE=setup"
    set "NEED_DEV_DEPS=1"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="deps" (
    set "MODE=setup"
    set "NEED_DEV_DEPS=1"
    shift
    goto :after_mode_parse
)
if /i "%~1"=="install-deps" (
    set "MODE=setup"
    set "NEED_DEV_DEPS=1"
    shift
    goto :after_mode_parse
)

:after_mode_parse
:collect_args
if "%~1"=="" goto :args_done
set "EXTRA_ARGS=!EXTRA_ARGS! %1"
shift
goto :collect_args
:args_done

call :find_msys2
if not defined MSYS2_ROOT goto :msys2_not_found

set "PACMAN=!MSYS2_ROOT!\usr\bin\pacman.exe"
set "UCRT64_BIN=!MSYS2_ROOT!\ucrt64\bin"
set "UCRT64_PYTHON=!UCRT64_BIN!\python.exe"
set "VENV_DIR=!REPO_ROOT!\.venv-ucrt64"

set "MSYSTEM=UCRT64"
set "MSYSTEM_PREFIX=/ucrt64"
set "MSYSTEM_CARCH=x86_64"
set "MSYS2_PATH_TYPE=inherit"
set "PYTHONHOME="
set "VIRTUAL_ENV="
set "PATH=!UCRT64_BIN!;!MSYS2_ROOT!\usr\bin;!PATH!"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if defined PYTHONPATH set "PYTHONPATH=!REPO_ROOT!\contents\code;!PYTHONPATH!"
if not defined PYTHONPATH set "PYTHONPATH=!REPO_ROOT!\contents\code"

echo [UCRT64] MSYS2 root : !MSYS2_ROOT!
echo [UCRT64] Repository : !REPO_ROOT!

rem ----------------------------------------------------------------------------
rem Stage A: Ensure required MSYS2 UCRT64 pacman packages are installed
rem ----------------------------------------------------------------------------
set "PACMAN_PKGS=mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-python-pip mingw-w64-ucrt-x86_64-python-setuptools mingw-w64-ucrt-x86_64-python-wheel mingw-w64-ucrt-x86_64-python-fastapi mingw-w64-ucrt-x86_64-uvicorn mingw-w64-ucrt-x86_64-python-pydantic mingw-w64-ucrt-x86_64-python-websockets mingw-w64-ucrt-x86_64-python-pyserial"
if "!NEED_DEV_DEPS!"=="1" (
    set "PACMAN_PKGS=!PACMAN_PKGS! mingw-w64-ucrt-x86_64-python-pytest mingw-w64-ucrt-x86_64-python-pytest-asyncio mingw-w64-ucrt-x86_64-python-httpx mingw-w64-ucrt-x86_64-ruff"
)

"!PACMAN!" -Q !PACMAN_PKGS! >nul 2>&1
if not errorlevel 1 goto :pacman_done

set "MISSING_PKGS="
for %%P in (!PACMAN_PKGS!) do (
    "!PACMAN!" -Q %%P >nul 2>&1
    if errorlevel 1 set "MISSING_PKGS=!MISSING_PKGS! %%P"
)

if defined MISSING_PKGS (
    echo [DEPS] Missing UCRT64 pacman packages:!MISSING_PKGS!
    if exist "!MSYS2_ROOT!\var\lib\pacman\sync\ucrt64.db" (
        echo [DEPS] Installing missing packages via pacman...
        "!PACMAN!" -S --needed --noconfirm !MISSING_PKGS!
        if errorlevel 1 (
            echo [DEPS] Refreshing pacman package database and retrying...
            "!PACMAN!" -Sy --needed --noconfirm !MISSING_PKGS!
        )
    ) else (
        echo [DEPS] Initializing pacman database and installing missing packages...
        "!PACMAN!" -Sy --needed --noconfirm !MISSING_PKGS!
    )
    if errorlevel 1 (
        echo [ERROR] Failed to install required MSYS2 UCRT64 packages.
        echo         If your MSYS2 installation is outdated, open an MSYS2 UCRT64 shell,
        echo         run "pacman -Syu", restart MSYS2 if prompted, and re-run this script.
        goto :fail
    )
)

:pacman_done
if not exist "!UCRT64_PYTHON!" (
    echo [ERROR] UCRT64 Python interpreter not found at "!UCRT64_PYTHON!".
    goto :fail
)

rem ----------------------------------------------------------------------------
rem Stage B: Create or validate .venv-ucrt64 (--system-site-packages)
rem ----------------------------------------------------------------------------
call :resolve_venv_python
set "VENV_OK=0"
if defined VENV_PYTHON call :validate_venv

if not "!VENV_OK!"=="1" (
    if exist "!VENV_DIR!" (
        echo [DEPS] Recreating virtual environment "!VENV_DIR!"...
        rmdir /s /q "!VENV_DIR!"
        if exist "!VENV_DIR!" (
            echo [ERROR] Could not remove stale virtual environment "!VENV_DIR!". Ensure no Python process is locking it.
            goto :fail
        )
    ) else (
        echo [DEPS] Creating UCRT64 virtual environment at "!VENV_DIR!"...
    )
    "!UCRT64_PYTHON!" -m venv --system-site-packages --without-pip "!VENV_DIR!"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment at "!VENV_DIR!".
        goto :fail
    )
    call :resolve_venv_python
    if not defined VENV_PYTHON (
        echo [ERROR] Virtual environment was created at "!VENV_DIR!" but python.exe was not found in bin\ or Scripts\.
        goto :fail
    )
)

"!VENV_PYTHON!" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [DEPS] Bootstrapping pip inside "!VENV_DIR!"...
    "!VENV_PYTHON!" -m ensurepip --default-pip
    if errorlevel 1 (
        echo [ERROR] Failed to bootstrap pip inside "!VENV_DIR!".
        goto :fail
    )
)

rem ----------------------------------------------------------------------------
rem Stage C: Sync any remaining requirements.txt packages via pip
rem ----------------------------------------------------------------------------
set "REQ_FILE=!REPO_ROOT!\requirements.txt"
if not exist "!REQ_FILE!" goto :reqs_done

set "MISSING_FILE=%TEMP%\fcs_missing_reqs_%RANDOM%_%RANDOM%.txt"
"!VENV_PYTHON!" -c "import importlib.metadata as m, os, pathlib, re; reqs = [l.split('#', 1)[0].strip() for l in pathlib.Path(os.environ['REQ_FILE']).read_text(encoding='utf-8').splitlines()]; miss = [s for s in reqs if s and not s.startswith('-') and re.match(r'[A-Za-z0-9._-]+', s) and not any(m.distributions(name=re.match(r'[A-Za-z0-9._-]+', s).group(0)))]; print('\n'.join(miss), end='\n' if miss else '')" > "!MISSING_FILE!"
if errorlevel 1 (
    if exist "!MISSING_FILE!" del /f /q "!MISSING_FILE!" >nul 2>&1
    echo [ERROR] Failed to inspect Python packages against "!REQ_FILE!".
    goto :fail
)

set "PIP_SYNC_FAILED=0"
for /f "usebackq delims=" %%R in ("!MISSING_FILE!") do call :install_pip_req "%%R"
if exist "!MISSING_FILE!" del /f /q "!MISSING_FILE!" >nul 2>&1
if "!PIP_SYNC_FAILED!"=="1" goto :fail

:reqs_done
"!VENV_PYTHON!" -c "import fastapi, uvicorn, pydantic, websockets, serial" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Core runtime module import check failed ^(fastapi, uvicorn, pydantic, websockets, serial^).
    goto :fail
)

cd /d "!REPO_ROOT!"
if errorlevel 1 (
    echo [ERROR] Could not change directory to "!REPO_ROOT!".
    goto :fail
)

if /i "!MODE!"=="mock" goto :run_mock
if /i "!MODE!"=="live" goto :run_live
if /i "!MODE!"=="pytest" goto :run_pytest
if /i "!MODE!"=="lint" goto :run_lint
if /i "!MODE!"=="check" goto :run_check
if /i "!MODE!"=="setup" goto :run_setup

echo [ERROR] Unknown mode: !MODE!
goto :fail

:run_mock
if not defined FCS_HOST set "FCS_HOST=127.0.0.1"
if not defined FCS_PORT set "FCS_PORT=8080"
set "PY_VER=unknown"
for /f "usebackq delims=" %%V in (`"!VENV_PYTHON!" --version 2^>^&1`) do set "PY_VER=%%V"
echo.
echo ========================================================================
echo   Flow-Control-System-Server ^(UCRT64 MOCK SIMULATION^)
echo   Python   : !PY_VER! ^(!VENV_PYTHON!^)
echo   HTTP API : http://!FCS_HOST!:!FCS_PORT!/docs
echo   WebSocket: ws://!FCS_HOST!:!FCS_PORT!/ws
echo   Tip      : Pass --host 0.0.0.0 to bind all interfaces, or use
echo              pytest ^| lint ^| check ^| setup ^| live ^| help
echo   Press Ctrl+C to stop the server.
echo ========================================================================
echo.
"!VENV_PYTHON!" -u "!REPO_ROOT!\contents\code\main.py" --mock!EXTRA_ARGS!
set "EXIT_CODE=!ERRORLEVEL!"
goto :finish

:run_live
if not defined FCS_HOST set "FCS_HOST=127.0.0.1"
if not defined FCS_PORT set "FCS_PORT=8080"
set "PY_VER=unknown"
for /f "usebackq delims=" %%V in (`"!VENV_PYTHON!" --version 2^>^&1`) do set "PY_VER=%%V"
echo.
echo ========================================================================
echo   Flow-Control-System-Server ^(UCRT64 LIVE / HARDWARE AUTO-DETECT^)
echo   Python   : !PY_VER! ^(!VENV_PYTHON!^)
echo   HTTP API : http://!FCS_HOST!:!FCS_PORT!/docs
echo   WebSocket: ws://!FCS_HOST!:!FCS_PORT!/ws
echo   Tip      : Pass --serial-port COM6 or --host 0.0.0.0 as needed.
echo   Press Ctrl+C to stop the server.
echo ========================================================================
echo.
"!VENV_PYTHON!" -u "!REPO_ROOT!\contents\code\main.py"!EXTRA_ARGS!
set "EXIT_CODE=!ERRORLEVEL!"
goto :finish

:run_pytest
echo [TEST] Running pytest suite...
"!VENV_PYTHON!" -m pytest -v!EXTRA_ARGS!
set "EXIT_CODE=!ERRORLEVEL!"
goto :finish

:run_lint
echo [LINT] Running ruff check on contents/code and tests...
if exist "!UCRT64_BIN!\ruff.exe" (
    "!UCRT64_BIN!\ruff.exe" check contents/code tests!EXTRA_ARGS!
) else (
    ruff check contents/code tests!EXTRA_ARGS!
)
set "EXIT_CODE=!ERRORLEVEL!"
goto :finish

:run_check
echo [CHECK] Step 1/2: Running ruff check on contents/code and tests...
if exist "!UCRT64_BIN!\ruff.exe" (
    "!UCRT64_BIN!\ruff.exe" check contents/code tests
) else (
    ruff check contents/code tests
)
if errorlevel 1 (
    set "EXIT_CODE=!ERRORLEVEL!"
    goto :finish
)
echo [CHECK] Step 2/2: Running pytest suite...
"!VENV_PYTHON!" -m pytest -v!EXTRA_ARGS!
set "EXIT_CODE=!ERRORLEVEL!"
goto :finish

:run_setup
echo.
echo [SETUP] Environment and dependencies verified:
"!VENV_PYTHON!" -c "import importlib.metadata as m, sys; pkgs = ['fastapi', 'uvicorn', 'pydantic', 'websockets', 'pyserial', 'LabJackPython', 'pytest', 'pytest-asyncio', 'httpx']; print('Python:', sys.version.split()[0], '(' + sys.executable + ')'); [print(' ', p.ljust(16), m.version(p) if any(m.distributions(name=p)) else ('optional - not installed' if p == 'LabJackPython' else 'MISSING')) for p in pkgs]"
if exist "!UCRT64_BIN!\ruff.exe" (
    "!UCRT64_BIN!\ruff.exe" --version
) else (
    ruff --version 2>nul
)
set "EXIT_CODE=0"
goto :finish

:detect_double_click
if not defined CMDCMDLINE goto :eof
set "_CMD=!CMDCMDLINE:"=!"
if /i not "!_CMD: /c =!"=="!_CMD!" set "PAUSE_ON_ERROR=1"
if /i not "!_CMD:/c =!"=="!_CMD!" set "PAUSE_ON_ERROR=1"
goto :eof

:find_msys2
if defined MSYS2_ROOT (
    if exist "!MSYS2_ROOT!\usr\bin\pacman.exe" goto :eof
    set "MSYS2_ROOT="
)
for %%D in (
    "C:\msys64"
    "D:\msys64"
    "%USERPROFILE%\msys64"
    "%LOCALAPPDATA%\msys64"
    "C:\tools\msys64"
    "%USERPROFILE%\scoop\apps\msys2\current"
) do (
    if not defined MSYS2_ROOT if exist "%%~D\usr\bin\pacman.exe" set "MSYS2_ROOT=%%~D"
)
if defined MSYS2_ROOT goto :eof
for /f "usebackq delims=" %%P in (`where pacman.exe 2^>nul`) do call :check_pacman_bin "%%~dpP"
goto :eof

:check_pacman_bin
if defined MSYS2_ROOT goto :eof
for %%D in ("%~1..\..") do set "_CAND=%%~fD"
if exist "!_CAND!\usr\bin\pacman.exe" set "MSYS2_ROOT=!_CAND!"
goto :eof

:resolve_venv_python
set "VENV_PYTHON="
if exist "!VENV_DIR!\bin\python.exe" set "VENV_PYTHON=!VENV_DIR!\bin\python.exe"
if not defined VENV_PYTHON if exist "!VENV_DIR!\Scripts\python.exe" set "VENV_PYTHON=!VENV_DIR!\Scripts\python.exe"
goto :eof

:validate_venv
"!VENV_PYTHON!" -c "import os, pathlib, subprocess, sys; cfg = pathlib.Path(sys.prefix, 'pyvenv.cfg'); ok = cfg.is_file() and 'include-system-site-packages = true' in cfg.read_text(encoding='utf-8').lower(); base = subprocess.check_output([os.environ['UCRT64_PYTHON'], '-c', 'import sys; print(sys.version_info[:2])'], text=True).strip(); sys.exit(0 if ok and base == str(sys.version_info[:2]) and sys.prefix not in (sys.base_prefix, '') else 1)" >nul 2>&1
if not errorlevel 1 set "VENV_OK=1"
goto :eof

:install_pip_req
set "REQ_SPEC=%~1"
if not defined REQ_SPEC goto :eof
echo [DEPS] Installing Python package "!REQ_SPEC!"...
"!VENV_PYTHON!" -m pip install --prefer-binary --no-build-isolation "!REQ_SPEC!"
if not errorlevel 1 goto :eof
echo [DEPS] Retrying "!REQ_SPEC!" with standard pip build...
"!VENV_PYTHON!" -m pip install --prefer-binary "!REQ_SPEC!"
if not errorlevel 1 goto :eof
if exist "!REPO_ROOT!\tools\vendor\wheels" (
    echo [DEPS] Retrying "!REQ_SPEC!" from local wheel cache...
    "!VENV_PYTHON!" -m pip install --no-index --find-links "!REPO_ROOT!\tools\vendor\wheels" --no-deps "!REQ_SPEC!"
    if not errorlevel 1 goto :eof
)
if /i not "!REQ_SPEC:labjackpython=!"=="!REQ_SPEC!" (
    echo [WARN] Optional package "!REQ_SPEC!" could not be installed. Mock mode and tests will run without LabJack hardware support.
    exit /b 0
)
echo [ERROR] Failed to install required Python package "!REQ_SPEC!".
set "PIP_SYNC_FAILED=1"
exit /b 1

:show_help
echo Flow-Control-System-Server - Windows MSYS2 UCRT64 Test ^& Server Launcher
echo.
echo Usage:
echo   %~nx0 [mode] [extra arguments...]
echo.
echo Modes:
echo   mock          Start server in full mock mode ^(default; forwards extra flags to main.py^)
echo   live          Start server with hardware auto-detect ^(e.g. --serial-port COM6^)
echo   pytest        Run pytest -v ^(forwards extra flags to pytest^)
echo   lint          Run ruff check contents/code tests
echo   check         Run ruff check and pytest -v
echo   setup         Install and verify UCRT64 pacman + .venv-ucrt64 dependencies, then exit
echo   help          Show this help message
echo.
echo Examples:
echo   %~nx0
echo   %~nx0 --port 9000 --safe
echo   %~nx0 live --serial-port COM6
echo   %~nx0 pytest -k test_phase1
echo   %~nx0 check
echo   %~nx0 setup
echo.
echo Environment Variables:
echo   MSYS2_ROOT    Override MSYS2 install directory ^(default: auto-detected^)
echo   FCS_HOST      Default bind host for mock/live modes ^(default: 127.0.0.1^)
echo   FCS_PORT      Default bind port for mock/live modes ^(default: 8080^)
echo   FCS_NO_PAUSE  Set to 1 to skip pause on error when launched via double-click
set "EXIT_CODE=0"
goto :exit_now

:msys2_not_found
echo [ERROR] Could not locate an MSYS2 installation ^(expected usr\bin\pacman.exe^).
echo.
echo Searched:
echo   - %%MSYS2_ROOT%%
echo   - C:\msys64
echo   - D:\msys64
echo   - %USERPROFILE%\msys64
echo   - %LOCALAPPDATA%\msys64
echo   - C:\tools\msys64
echo   - %USERPROFILE%\scoop\apps\msys2\current
echo   - PATH ^(where pacman.exe^)
echo.
echo Install MSYS2 on Windows ^(e.g. "winget install --id MSYS2.MSYS2 -e" or from https://www.msys2.org/^)
echo or set MSYS2_ROOT=^<path-to-msys64^> before running this script.

:fail
set "EXIT_CODE=1"

:finish
if "!EXIT_CODE!"=="0" goto :exit_now
if "!EXIT_CODE!"=="-1073741510" goto :exit_now
if "!PAUSE_ON_ERROR!"=="1" (
    echo.
    echo [ERROR] Script exited with code !EXIT_CODE!.
    pause
)

:exit_now
endlocal & exit /b %EXIT_CODE%
