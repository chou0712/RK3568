@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0" || (
  echo.
  echo 无法进入程序目录：%~dp0
  pause
  exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo 未找到 Python。请先安装 Python 3，并将 Python 加入 PATH。
  pause
  exit /b 1
)

python -c "import tkinter; import satellite_tracker.app" >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python 环境或程序依赖检查失败。
  echo 请运行“安装依赖.bat”，并确认当前 Python 包含 Tkinter。
  echo.
  python -c "import tkinter; import satellite_tracker.app"
  pause
  exit /b 1
)

python "%~dp0main.py" %*
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" (
  echo.
  echo 程序启动失败，退出代码：%APP_EXIT_CODE%
  echo 请检查上面的错误信息。
  pause
)
exit /b %APP_EXIT_CODE%
