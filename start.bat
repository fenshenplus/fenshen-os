@echo off
chcp 65001 >nul
title 分身 v5.4 · 桌面托管（Windows）
cd /d %~dp0

echo ============================================
echo  分身 v5.4 · Windows 启动器
echo  端口 8002 · 浏览器将自动打开
echo  局域网访问: FENSHEN_ALLOW_LAN=1 见 README
echo ============================================
echo.

rem 选择 Python 解释器（python 或 py launcher）
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [错误] 未找到 Python。请先安装 Python 3.10+：
  echo   官网 https://www.python.org/downloads/windows/  安装时勾选 "Add to PATH"
  pause
  exit /b 1
)

echo [1/2] 检查依赖...
%PY% -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo       首次运行，正在安装依赖（约 1 分钟）...
  %PY% -m pip install -r requirements-dist.txt
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

echo [2/2] 启动分身（127.0.0.1:8002）...
start "" http://127.0.0.1:8002
%PY% -m uvicorn backend.main:app --app-dir "%~dp0" --host 127.0.0.1 --port 8002

echo.
echo 分身已退出。按任意键关闭窗口。
pause >nul
