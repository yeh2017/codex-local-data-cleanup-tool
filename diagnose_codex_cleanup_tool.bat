@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "APP_EXE=%~dp0Codex Local Data Cleanup Tool.exe"
set "LOG_DIR=%LOCALAPPDATA%\CodexLocalCleanupTool\logs"
set "STARTUP_LOG=%LOG_DIR%\startup.log"

echo Codex 本地数据清理工具诊断启动 / Diagnostic startup
echo 系统版本 / Windows version:
ver
echo 系统架构 / Architecture: %PROCESSOR_ARCHITECTURE%
echo 程序路径 / Application: %APP_EXE%
echo 启动日志 / Startup log: %STARTUP_LOG%
echo.

if not exist "%APP_EXE%" (
    echo [错误 / Error] 独立程序不存在，请保持整个发布文件夹完整。 Keep the entire application folder together.
    set "FAIL_CODE=2"
    goto diagnostic_failure
)

echo 正在执行内置启动检查 / Running built-in startup check...
start "" /wait "%APP_EXE%" --startup-check
set "FAIL_CODE=%ERRORLEVEL%"
if not "%FAIL_CODE%"=="0" goto diagnostic_failure

echo 启动检查通过，正在打开主界面 / Startup check passed. Opening the application...
start "" "%APP_EXE%"
exit /b 0

:diagnostic_failure
echo.
echo [错误 / Error] 工具启动失败 / Startup failed. Code: %FAIL_CODE%
echo 启动日志 / Startup log: %STARTUP_LOG%
if exist "%STARTUP_LOG%" (
    choice /c VO /n /m "按 V 查看日志，按 O 退出 / V: view log, O: exit: "
    if errorlevel 2 goto diagnostic_pause
    start "" notepad.exe "%STARTUP_LOG%"
)
:diagnostic_pause
pause
exit /b %FAIL_CODE%
