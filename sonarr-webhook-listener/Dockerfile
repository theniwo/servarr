# Basis-Image mit Python
FROM python:3.11-slim

# Setze das Arbeitsverzeichnis
WORKDIR /app

# Kopiere die Abhängigkeitsdatei und installiere Flask & Requests
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiere das Webhook-Skript in den Container
COPY webhook_listener.py .

# Exponiere den Port für den Webhook
EXPOSE 5000

# Starte das Webhook-Skript
CMD ["python", "webhook_listener.py"]

