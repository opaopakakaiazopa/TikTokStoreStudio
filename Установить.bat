@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 or newer was not found. Install it and enable Add Python to PATH.
  goto end_error
)
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 (
    echo Failed to create the Python environment.
    goto end_error
  )
)
".venv\Scripts\python.exe" install.py
if errorlevel 1 (
  echo Setup failed. Check Python and your internet connection.
  goto end_error
) else (
  echo Setup complete. Run the Studio launcher in this folder.
)
pause
exit /b 0

:end_error
pause
exit /b 1