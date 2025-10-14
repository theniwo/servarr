import os
import requests
from flask import Flask, request

app = Flask(__name__)

# Hole die Umgebungsvariablen für Sonarr
SONARR_URL = os.getenv("SONARR_URL", "http://sonarr:8989")
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")

def add_new_season(tvdb_id):
    """Sendet eine Anfrage an Sonarr, um eine neue Staffel hinzuzufügen."""
    headers = {"X-Api-Key": SONARR_API_KEY}
    sonarr_series_url = f"{SONARR_URL}/api/v3/series"

    # Serien-Infos von Sonarr abrufen
    response = requests.get(sonarr_series_url, headers=headers)
    if response.status_code != 200:
        print(f"Fehler beim Abrufen der Serien: {response.text}")
        return False

    series = response.json()
    for show in series:
        if show["tvdbId"] == tvdb_id:
            print(f"Serie gefunden: {show['title']}")

            # Neue Staffel zu den gewählten Staffeln hinzufügen
            show["seasons"].append({"seasonNumber": max(s["seasonNumber"] for s in show["seasons"]) + 1})
            update_response = requests.put(sonarr_series_url, json=show, headers=headers)

            if update_response.status_code == 200:
                print("Neue Staffel erfolgreich hinzugefügt.")
                return True
            else:
                print(f"Fehler beim Hinzufügen der Staffel: {update_response.text}")
                return False
    print("Serie nicht gefunden.")
    return False

@app.route("/webhook", methods=["POST"])
def jellyfin_webhook():
    """Empfängt Webhooks von Jellyfin und triggert Sonarr."""
    data = request.json
    print("Webhook erhalten:", data)

    # Prüfen, ob die Episode zu Ende geschaut wurde
    if data.get("Event") == "PlaybackStopped" and data.get("Item", {}).get("Type") == "Episode":
        tvdb_id = data.get("Item", {}).get("SeriesId")
        if tvdb_id:
            add_new_season(tvdb_id)

    return "", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

