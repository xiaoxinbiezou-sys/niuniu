@echo off
chcp 65001 >nul
title 停止故事创作工作室

echo 正在查找 studio.server 进程...
set FOUND=0
for /f "tokens=2 delims=," %%p in ('tasklist /fi "imagename eq python.exe" /fo csv /nh 2^>nul') do (
    wmic process where "ProcessId=%%~p" get CommandLine 2>nul | findstr /c:"studio.server" >nul
    if not errorlevel 1 (
        echo   结束 PID %%~p
        taskkill /pid %%~p /f >nul 2>&1
        set FOUND=1
    )
)

if "%FOUND%"=="1" (
    echo 已停止。
) else (
    echo 没有找到正在运行的 studio.server 进程。
)
echo.
pause
