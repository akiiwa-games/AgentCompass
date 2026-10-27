@echo off
python -X utf8 "%~dp0run_dot_task.py" %*
exit /b %ERRORLEVEL%
