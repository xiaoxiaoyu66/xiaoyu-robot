@echo off
rem ============================================================
rem  小柚子进程守护（S5.5）：挂了 5 秒自己爬起来。
rem  由"任务计划程序"在登录时启动（install_autostart.py 注册）。
rem  主循环本身有自愈（单轮异常不退出），这层守护兜的是
rem  "程序整个退出/崩溃"的情况 —— 比如连续失败 5 次主动交棒。
rem ============================================================
cd /d "%~dp0\.."
if not exist logs mkdir logs

:loop
echo [%date% %time%] guardian: starting xiaoyu >> logs\guardian.log
py -3.11 -m xiaoyu >> logs\guardian.log 2>&1
echo [%date% %time%] guardian: exited with code %errorlevel%, restarting in 5s >> logs\guardian.log
timeout /t 5 /nobreak >nul
goto loop
