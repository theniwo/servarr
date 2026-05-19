import os
import requests

from fastapi import FastAPI, Request
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

RADARR_URL = os.getenv("RADARR_URL")
RADARR_KEY = os.getenv("RADARR_KEY")

JELLYFIN_URL = os.getenv("JELLYFIN_URL")
JELLYFIN_KEY = os.getenv("JELLYFIN_KEY")

collection_map = {}

for key, value in os.environ.items():
    if key.startswith("TAG_"):
        tag = key.replace("TAG_", "").lower().strip()
        collection_map[tag] = value.strip()

print("Collection map loaded:", collection_map)


# -----------------------------
# RADARR
# -----------------------------
def get_radarr_tags():
    res = requests.get(
        f"{RADARR_URL}/api/v3/tag",
        headers={"X-Api-Key": RADARR_KEY},
    )
    res.raise_for_status()

    return {
        t["id"]: t["label"].lower().strip()
        for t in res.json()
    }


# -----------------------------
# JELLYFIN HELPERS
# -----------------------------
def jellyfin_headers():
    return {
        "X-Emby-Token": JELLYFIN_KEY,
        "Content-Type": "application/json"
    }


def find_jellyfin_movie(movie_path):
    res = requests.get(
        f"{JELLYFIN_URL}/Items",
        headers=jellyfin_headers(),
        params={
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "Fields": "Path"
        }
    )

    if res.status_code != 200:
        print("Jellyfin error:", res.text)
        return None

    data = res.json()

    norm_path = os.path.normpath(movie_path)

    for item in data.get("Items", []):
        if not item.get("Path"):
            continue

        if os.path.normpath(item["Path"]).startswith(norm_path):
            return item["Id"]

    return None


def get_or_create_collection(name):
    # 1. suchen
    res = requests.get(
        f"{JELLYFIN_URL}/Items",
        headers=jellyfin_headers(),
        params={
            "Recursive": "true",
            "IncludeItemTypes": "BoxSet",
            "SearchTerm": name
        }
    )

    if res.status_code == 200:
        items = res.json().get("Items", [])
        for item in items:
            if item["Name"].lower() == name.lower():
                return item["Id"]

    # 2. erstellen
    print(f"Creating collection: {name}")

    res = requests.post(
        f"{JELLYFIN_URL}/Collections",
        headers=jellyfin_headers(),
        json={
            "Name": name,
            "IsLocked": False
        }
    )

    if res.status_code not in [200, 201]:
        print("Failed to create collection:", res.text)
        return None

    return res.json().get("Id")


def add_movie_to_collection(collection_id, movie_id):
    # Collection laden
    res = requests.get(
        f"{JELLYFIN_URL}/Items/{collection_id}",
        headers=jellyfin_headers()
    )

    if res.status_code != 200:
        return False

    collection = res.json()
    existing = collection.get("LinkedChildren", [])

    if movie_id in [m.get("Id") for m in existing]:
        return True  # schon drin

    # hinzufügen
    res = requests.post(
        f"{JELLYFIN_URL}/Collections/{collection_id}/Items",
        headers=jellyfin_headers(),
        json={"Ids": [movie_id]}
    )

    return res.status_code in [200, 204]


# -----------------------------
# CORE LOGIC
# -----------------------------
def process_movie(movie):
    radarr_tags = get_radarr_tags()

    tag_names = [
        radarr_tags[t].lower().strip()
        for t in movie.get("tags", [])
        if t in radarr_tags
    ]

    collections = [
        collection_map[t]
        for t in tag_names
        if t in collection_map
    ]

    movie_path = movie.get("folderName") or movie.get("path")
    movie_id = find_jellyfin_movie(movie_path)

    result = {
        "movie": movie.get("title"),
        "collections": []
    }

    if not movie_id:
        return result

    for collection_name in collections:
        collection_id = get_or_create_collection(collection_name)

        if collection_id:
            add_movie_to_collection(collection_id, movie_id)
            result["collections"].append(collection_name)

    return result


# -----------------------------
# RADARR WEBHOOK
# -----------------------------
@app.post("/radarr")
async def radarr_webhook(request: Request):
    payload = await request.json()
    event = payload.get("eventType")

    if event == "Test":
        return {"status": "ok", "message": "test received"}

    movie = payload.get("movie")
    if not movie:
        return {"status": "ignored"}

    return {
        "status": "ok",
        "processed": process_movie(movie)
    }


# -----------------------------
# FULLSCAN
# -----------------------------
@app.post("/fullscan")
async def fullscan():
    res = requests.get(
        f"{RADARR_URL}/api/v3/movie",
        headers={"X-Api-Key": RADARR_KEY},
    )
    res.raise_for_status()

    processed = []

    for movie in res.json():
        processed.append(process_movie(movie))

    return {
        "status": "ok",
        "processed": processed
    }
