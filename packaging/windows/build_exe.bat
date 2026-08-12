@echo off
chcp 65001 >nul
title 打包 分身 exe
cd /d %~dp0\..\..

echo ============================================
echo  打包「分身」为 Windows exe（免装 Python）
echo  需要 Windows + Python 3.10+，约 5 分钟
echo ============================================

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY (
  echo [错误] 未找到 Python，请先安装并勾选 Add to PATH
  pause & exit /b 1
)

echo [1/4] 安装打包工具...
%PY% -m pip install pyinstaller -q

echo [2/4] 运行打包...
%PY% -m PyInstaller --clean packaging/windows/fenshen.spec -y

echo [3/4] 产出检查...
if not exist "dist\分身.exe" (
  echo [错误] 打包失败，查看 build\ 日志
  pause & exit /b 1
)

echo [4/4] 打发行 zip...
powershell -Command "Compress-Archive -Force -Path 'dist\分身.exe' -DestinationPath 'dist\分身-Windows-exe.zip'"
echo.
echo ✅ 完成！发行包: dist\分身-Windows-exe.zip
echo    把它上传到官网，用户下载后双击「分身.exe」即用（无需安装 Python）
pause
