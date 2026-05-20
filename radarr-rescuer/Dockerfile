FROM python:3.11-slim

# Installiere benötigte System-Abhängigkeiten (falls benötigt)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Installiere den MySQL-Treiber für Python
RUN pip install --no-cache-dir mysql-connector-python

# Setze das Arbeitsverzeichnis im Container
WORKDIR /app

# Kopiere das Migrations-Skript in den Container
COPY migrate_settings.py .

# Starte das Skript standardmäßig
CMD ["python", "migrate_settings.py"]
