@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .env (
  copy bot\env.example .env >nul
  echo Впишите ключи в файл .env и запустите снова.
  notepad .env
  exit /b
)
if not exist .venv (
  echo Первый запуск: ставлю библиотеки...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -q -r bot\requirements.txt
)
:loop
echo Бот запущен. Не закрывайте это окно.
.venv\Scripts\python -m bot.app
echo Бот остановился, перезапуск через 10 секунд...
timeout /t 10 >nul
goto loop
