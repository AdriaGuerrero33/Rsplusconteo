@echo off
echo ============================================
echo  Instalando Verificador de Resenas Google
echo ============================================
echo.

echo [1/3] Instalando librerias Python...
py -m pip install gspread google-auth playwright fastapi uvicorn python-dotenv asyncio-throttle
if errorlevel 1 (
    echo.
    echo ERROR: Fallo al instalar librerias.
    echo Asegurate de tener Python instalado desde python.org
    pause
    exit /b 1
)

echo.
echo [2/3] Instalando navegador Chromium...
py -m playwright install chromium
if errorlevel 1 (
    echo ERROR: Fallo al instalar Chromium.
    pause
    exit /b 1
)

echo.
echo [3/3] Copiando configuracion...
if not exist .env (
    copy .env.example .env
)

echo.
echo ============================================
echo  Instalacion completada correctamente!
echo  Ahora ejecuta: iniciar.bat
echo ============================================
pause
