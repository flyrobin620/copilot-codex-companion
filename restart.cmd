@echo off
where py >nul 2>&1
if errorlevel 1 goto usepython
py -3 "%~dp0manage.py" restart %*
goto finish
:usepython
python "%~dp0manage.py" restart %*
:finish
set compat_exit=%errorlevel%
pause
exit /b %compat_exit%
