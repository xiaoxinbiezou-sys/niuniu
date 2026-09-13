@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 故事创作工作室

echo ============================================
echo   故事创作工作室
echo ============================================
echo.

if not exist ".python\python.exe" (
    echo [错误] 找不到 .python\python.exe
    echo        请在项目根目录运行这个脚本。
    echo.
    pause
    exit /b 1
)

rem 端口已被占用就不再重复启动（避免第二个实例抢端口失败）
netstat -ano | findstr /r /c:"TCP.*:8000 .*LISTENING" >nul
if %errorlevel%==0 (
    echo [提示] 8000 端口已经在运行，直接打开浏览器。
    echo        如果打不开，说明是别的程序占用了端口，请先关掉它。
    echo.
) else (
    echo [1/2] 正在启动服务器...
    start "" /min ".python\python.exe" -m studio.server --port 8000
    echo       等待启动...
    timeout /t 8 /nobreak >nul
)

echo [2/2] 打开浏览器 http://127.0.0.1:8000
start "" "http://127.0.0.1:8000"
echo.
echo 服务器已在本窗口之外运行。
echo 关闭服务器：在任务管理器结束 python.exe，或运行 停止服务.bat
echo.
pause
