FROM python:3.11-slim

# Certificados CA necesarios para descargar Chromium durante el build
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instala solo Chromium + sus dependencias del sistema
# Mucho más ligero que la imagen base playwright (~500 MB vs ~3 GB)
RUN python -m playwright install --with-deps chromium

COPY . .

EXPOSE 8000

CMD ["python", "app.py"]
