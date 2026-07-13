@echo off
REM Veille IA - lancement sous Windows (double-cliquer ce fichier)
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python est introuvable. Installez-le depuis https://www.python.org/downloads/
    echo puis cochez "Add python.exe to PATH" pendant l'installation.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creation de l'environnement virtuel...
    python -m venv .venv
)

echo Installation / mise a jour des dependances...
.venv\Scripts\python -m pip install -q -r requirements.txt

echo.
echo Veille IA demarre sur http://127.0.0.1:8000  (Ctrl+C pour arreter)
start "" http://127.0.0.1:8000
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
