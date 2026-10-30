@echo off
rem Newma Chat launcher for Windows (double-click or run from cmd)
setlocal
where python >nul 2>nul
if %errorlevel%==0 (
  python "%~dp0run.py" %*
  goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
  py "%~dp0run.py" %*
  goto :eof
)
echo Python not found. Install from https://www.python.org/ then retry.
exit /b 1
