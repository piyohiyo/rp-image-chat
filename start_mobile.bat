@echo off
setlocal
python --version >nul 2>&1
if errorlevel 1 (
  echo Python が見つかりません。python.org から Python 3.10 以上を入れてください。
  echo インストールの最初の画面で「Add python.exe to PATH」にチェックを入れます。
  pause
  exit /b 1
)
echo スマホ用（同じ Wi-Fi の機器から開ける）で起動します。自宅など信頼できる Wi-Fi だけで使ってください。
echo このウィンドウは、アプリを使っている間は閉じないでください。
set "RPCHAT_HOST=lan"
python "%~dp0server.py"
pause
