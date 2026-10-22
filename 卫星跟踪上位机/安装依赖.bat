@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "D:\AppDownload\pip-cache" mkdir "D:\AppDownload\pip-cache"
python -m pip install --cache-dir "D:\AppDownload\pip-cache" -r requirements.txt
if errorlevel 1 (
  echo.
  echo 依赖安装失败，请检查 Python 和网络设置。
  pause
  exit /b 1
)
echo.
echo 依赖安装完成。
pause
