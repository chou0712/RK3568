@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "SCRIPT=%~dp0turntable_control.py"

where python.exe >nul 2>nul
if errorlevel 1 goto check_py
python.exe -c "import tkinter as t; r=t.Tk(); r.withdraw(); r.destroy()" >nul 2>nul
if not errorlevel 1 goto run_python

:check_py
where py.exe >nul 2>nul
if errorlevel 1 goto no_python
py.exe -3 -c "import tkinter as t; r=t.Tk(); r.withdraw(); r.destroy()" >nul 2>nul
if not errorlevel 1 goto run_py
goto no_python

:run_python
python.exe "%SCRIPT%"
goto check_result

:run_py
py.exe -3 "%SCRIPT%"
goto check_result

:check_result
if errorlevel 1 goto run_failed
exit /b 0

:no_python
echo 未找到 Python 3。
echo 请安装 Python 3.10 或更高版本，并勾选 Add Python to PATH。
pause
exit /b 1

:run_failed
echo.
echo 转台控制程序启动失败，错误码：%errorlevel%
echo 请保留本窗口中的错误信息。
pause
exit /b 1
