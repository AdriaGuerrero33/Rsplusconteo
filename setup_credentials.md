# Configuración de credenciales de Google

## 1. Crear un proyecto en Google Cloud Console

1. Ve a https://console.cloud.google.com/
2. Crea un nuevo proyecto (o usa uno existente)
3. Activa las siguientes APIs:
   - **Google Sheets API**
   - **Google Drive API**

## 2. Crear un Service Account

1. En la consola, ve a **IAM y administración → Cuentas de servicio**
2. Clic en **Crear cuenta de servicio**
3. Ponle un nombre (ej: `reviews-agent`)
4. En el paso de roles, asigna: **Editor** (o al menos *Sheets Editor*)
5. Clic en **Listo**

## 3. Descargar las credenciales JSON

1. Entra en la cuenta de servicio recién creada
2. Ve a la pestaña **Claves**
3. Clic en **Agregar clave → Crear clave nueva → JSON**
4. Descarga el archivo y guárdalo como `credentials.json` en la raíz del proyecto

> **Importante:** No subas este archivo a git. Está incluido en `.gitignore`.

## 4. Compartir la hoja de Google Sheets con el Service Account

1. Abre la hoja de Google Sheets
2. Clic en **Compartir**
3. Agrega el email del Service Account (tiene este formato: `nombre@proyecto.iam.gserviceaccount.com`)
4. Asigna rol de **Editor**

## 5. Configurar el archivo .env

Copia `.env.example` a `.env` y rellena los valores:

```bash
cp .env.example .env
```

```
GOOGLE_CREDENTIALS_PATH=credentials.json
SHEET_URL=https://docs.google.com/spreadsheets/d/TU_SHEET_ID/edit
SOURCE_SHEET_TAB=     # vacío = primera pestaña
RESULTS_TAB=Estado_Reseñas
MAX_CONCURRENT_CHECKS=3
DELAY_BETWEEN_CHECKS=2
```

## 6. Instalar dependencias

```bash
pip install -r requirements.txt
playwright install chromium
```

## 7. Ejecutar el agente

```bash
python main.py
```

O pasando el Sheet directamente:

```bash
python main.py --sheet "https://docs.google.com/spreadsheets/d/TU_ID/edit"
```

## Columnas que se escriben en la pestaña de resultados

| Columna | Descripción |
|---|---|
| (columnas originales) | Todas las columnas de la fila fuente |
| Estado | `ACTIVA` / `ELIMINADA` / `ERROR` |
| Detalle | Mensaje explicativo del resultado |
| URL_Verificada | La URL que se procesó |

Al final de la hoja se agrega un **resumen** con los totales.
