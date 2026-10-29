@echo off
where py >nul 2>&1
if errorlevel 1 goto usepython
py -3 "%~dp0manage.py" uninstall %*
goto finish
:usepython
python "%~dp0manage.py" uninstall %*
:finish
set compat_exit=%errorlevel%
pause
exit /b %compat_exit%
