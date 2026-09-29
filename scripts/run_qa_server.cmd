@echo off
REM ===================================================================
REM  QA web page (homework A, step 4) - one-command launcher
REM
REM  Starts the Flask server in THIS console window.
REM  - Browser:  http://127.0.0.1:8765
REM  - Stop:     close this window or press Ctrl+C
REM              (also frees the ~2.5 GB the local model holds)
REM  - Progress is printed here AND appended to
REM    logs\qa_server_YYYY-MM-DD.log
REM
REM  Python: uses "python" from PATH (falls back to the "py" launcher).
REM  If neither is found, set PYTHON below to the full path of your
REM  python.exe.
REM
REM  NOTE: this file is intentionally ASCII-only. cmd.exe reads batch
REM  files with the OEM codepage (GBK on this machine), so UTF-8
REM  Chinese comments would come out garbled. Chinese output comes
REM  from the Python script itself (PYTHONIOENCODING=utf-8).
REM ===================================================================

setlocal
set "PYTHONIOENCODING=utf-8"

REM  Pick the Python interpreter: "python" from PATH, else "py".
set "PYTHON=python"
where python >nul 2>nul
if errorlevel 1 set "PYTHON=py"
where %PYTHON% >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Neither "python" nor "py" was found in PATH.
  echo         Install Python, or edit the PYTHON line in this file
  echo         to the full path of your python.exe.
  pause
  exit /b 1
)

REM %~dp0 = directory of this script (with trailing backslash)
cd /d "%~dp0.."

title QA Server - http://127.0.0.1:8765
"%PYTHON%" "%~dp0qa_server.py" %*

if errorlevel 1 (
  echo.
  echo [ERROR] Server exited abnormally. Check logs\qa_server_*.log
  pause
)
