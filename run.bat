@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist venv (
    echo Создание виртуального окружения...
    python -m venv venv
    call venv\Scripts\activate
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate
)

python orchestrator.py
pause