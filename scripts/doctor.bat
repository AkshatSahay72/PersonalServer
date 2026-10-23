@echo off
rem ==============================================================================
rem PersonalServer - Unified Diagnostic & Recovery Engine (Windows Batch Wrapper)
rem ==============================================================================
setlocal

set "SCRIPT_DIR=%~dp0"
set "BASE_DIR=%SCRIPT_DIR%.."

rem Locate Python binary
where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PYTHON_BIN=python"
) else (
    where py >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        set "PYTHON_BIN=py -3"
    ) else (
        echo Error: Python 3 was not found in PATH. Please install Python or add it to PATH. 1>&2
        exit /b 2
    )
)

rem Translate convenience commands (fix, repair, recover)
set "ARGS="
:arg_loop
if "%~1"=="" goto run_cmd
if /i "%~1"=="fix" (
    set "ARGS=%ARGS% --repair"
) else if /i "%~1"=="repair" (
    set "ARGS=%ARGS% --repair"
) else if /i "%~1"=="recover" (
    set "ARGS=%ARGS% --repair"
) else if /i "%~1"=="dry-run" (
    set "ARGS=%ARGS% --dry-run"
) else (
    set "ARGS=%ARGS% %1"
)
shift
goto arg_loop

:run_cmd
%PYTHON_BIN% "%SCRIPT_DIR%doctor.py" %ARGS%
set "EXIT_CODE=%ERRORLEVEL%"

exit /b %EXIT_CODE%
