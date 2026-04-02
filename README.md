# Verificador de Reseñas de Google Maps

Agente que lee una hoja de Google Sheets con enlaces de reseñas y comprueba cuáles siguen activas y cuáles han sido eliminadas.

## Instalación en Windows

### Paso 1 — Descarga el proyecto
Descarga el ZIP desde GitHub:
- Clic en botón verde **Code** → **Download ZIP**
- Extrae la carpeta en tu escritorio

### Paso 2 — Añade tus credenciales
Copia tu archivo `credentials.json` dentro de la carpeta del proyecto.

### Paso 3 — Instala todo
Haz doble clic en **`instalar.bat`**

Espera a que termine (puede tardar 2-3 minutos).

### Paso 4 — Inicia la aplicación
Haz doble clic en **`iniciar.bat`**

Se abrirá automáticamente el navegador en `http://localhost:8000`

## Uso
1. Pega la URL de tu Google Sheet
2. Clic en **Comprobar**
3. Ve el progreso en tiempo real
4. Al terminar, clic en **Ver en Google Sheets** para ver los resultados

## Requisitos
- Python 3.10 o superior
- El Google Sheet debe estar compartido con: `rsplusagente@minerun-3df52.iam.gserviceaccount.com`
