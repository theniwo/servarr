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


# -----------------------------
# TAG → COLLECTION MAP
# -----------------------------
def normalize(s: str) -> str:
    return s.strip().lower().replace(" ", "-")


collection_map = {}

for key, value in os.environ.items():
    if key.startswith("TAG_"):
        tag = normalize(key.replace("TAG_", ""))
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
        t["id"]: normalize(t["label"])
        for t in res.json()
    }


# -----------------------------
# JELLYFIN
# -----------------------------
def jellyfin_headers():
    return {
        "X-Emby-Token": JELLYFIN_KEY,
        "Content-Type": "application/json"
    }


def find_jellyfin_movie(movie_path):
    """
    Best-effort lookup: NEVER blocks pipeline
    """
    try:
        res = requests.get(
            f"{JELLYFIN_URL}/Items",
            headers=jellyfin_headers(),
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "Fields": "Path"
            },
            timeout=10
        )

        if res.status_code != 200:
            print("Jellyfin error:", res.text)
            return None

        data = res.json()

        norm_path = os.path.normpath(movie_path)

        for item in data.get("Items", []):
            path = item.get("Path")
            if not path:
                continue

            if os.path.normpath(path).startswith(norm_path):
                return item["Id"]

    except Exception as e:
        print("Jellyfin exception:", str(e))

    return None


def get_or_create_collection(name):
    try:
        res = requests.get(
            f"{JELLYFIN_URL}/Items",
            headers=jellyfin_headers(),
            params={
                "Recursive": "true",
                "IncludeItemTypes": "BoxSet",
                "SearchTerm": name
            },
            timeout=10
        )

        if res.status_code == 200:
            items = res.json().get("Items", [])
            for item in items:
                if item["Name"].lower() == name.lower():
                    return item["Id"]

        print(f"Creating collection: {name}")

        res = requests.post(
            f"{JELLYFIN_URL}/Collections",
            headers=jellyfin_headers(),
            json={
                "Name": name,
                "IsLocked": False
            },
            timeout=10
        )

        if res.status_code in [200, 201]:
            return res.json().get("Id")

    except Exception as e:
        print("Collection error:", str(e))

    return None


def add_movie_to_collection(collection_id, movie_id):
    try:
        res = requests.post(
            f"{JELLYFIN_URL}/Collections/{collection_id}/Items",
            headers=jellyfin_headers(),
            json={"Ids": [movie_id]},
            timeout=10
        )

        return res.status_code in [200, 204]

    except Exception as e:
        print("Add to collection error:", str(e))
        return False


# -----------------------------
# CORE LOGIC
# -----------------------------
def process_movie(movie):
    radarr_tags = get_radarr_tags()

    tag_ids = movie.get("tagIds") or movie.get("tags") or []

    tag_names = [
        radarr_tags[t]
        for t in tag_ids
        if t in radarr_tags
    ]

    # normalize tags for mapping
    tag_names = [normalize(t) for t in tag_names]

    collections = [
        collection_map[t]
        for t in tag_names
        if t in collection_map
    ]

    movie_path = movie.get("folderName") or movie.get("path")
    movie_id = find_jellyfin_movie(movie_path)

    result = {
        "movie": movie.get("title"),
        "collections": collections
    }

    if not collections:
        return result

    for collection_name in collections:
        collection_id = get_or_create_collection(collection_name)

        if collection_id and movie_id:
            add_movie_to_collection(collection_id, movie_id)

    return result


# -----------------------------
# RADARR WEBHOOK
# -----------------------------
@app.post("/radarr")
async def radarr_webhook(request: Request):
    payload = await request.json()
    event = payload.get("eventType")

    if event == "Test":
        return {"status": "ok"}

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
@app.get("/fullscan")
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
