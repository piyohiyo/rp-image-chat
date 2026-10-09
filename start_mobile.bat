@echo off
setlocal
set "RPCHAT_HOST=lan"
python "%~dp0server.py"
pause
