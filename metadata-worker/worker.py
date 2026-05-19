import os
import requests
from fastapi import FastAPI, Request
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# -----------------------------
# ENV
# -----------------------------
RADARR_URL = os.getenv("RADARR_URL")
RADARR_KEY = os.getenv("RADARR_KEY")

JELLYFIN_URL = os.getenv("JELLYFIN_URL")
JELLYFIN_KEY = os.getenv("JELLYFIN_KEY")

# -----------------------------
# TAG → COLLECTION MAPPING
# -----------------------------
collection_map = {}

for key, value in os.environ.items():
    if key.startswith("TAG_"):
        tag = key.replace("TAG_", "").lower().strip()
        collection_map[tag] = value.strip()

print("Collection map loaded:", collection_map)


# -----------------------------
# HEADERS
# -----------------------------
def jellyfin_headers():
    return {
        "X-Emby-Token": JELLYFIN_KEY,
        "Content-Type": "application/json"
    }


# -----------------------------
# RADARR TAGS
# -----------------------------
def get_radarr_tags():
    res = requests.get(
        f"{RADARR_URL}/api/v3/tag",
        headers={"X-Api-Key": RADARR_KEY},
        timeout=10
    )
    res.raise_for_status()

    return {
        t["id"]: t["label"].lower().strip()
        for t in res.json()
    }


# -----------------------------
# FIND MOVIE IN JELLYFIN
# -----------------------------
def find_jellyfin_movie(movie_path):
    res = requests.get(
        f"{JELLYFIN_URL}/Items",
        headers=jellyfin_headers(),
        params={
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "Fields": "Path"
        },
        timeout=20
    )

    if res.status_code != 200:
        print("Jellyfin error:", res.text)
        return None

    norm_path = os.path.normpath(movie_path or "")

    for item in res.json().get("Items", []):
        if not item.get("Path"):
            continue

        if os.path.normpath(item["Path"]).startswith(norm_path):
            return item["Id"]

    return None


# -----------------------------
# GET OR CREATE COLLECTION
# -----------------------------
def get_or_create_collection(name):
    # search
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
        for item in res.json().get("Items", []):
            if item["Name"].lower() == name.lower():
                return item["Id"]

    # create
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

    if res.status_code not in [200, 201]:
        print("Failed to create collection:", res.text)
        return None

    return res.json().get("Id")


# -----------------------------
# ADD MOVIE TO COLLECTION (FIXED)
# -----------------------------
def add_movie_to_collection(collection_id, movie_id):
    try:
        res = requests.post(
            f"{JELLYFIN_URL}/Collections/{collection_id}/Items/Add",
            headers=jellyfin_headers(),
            json={"Ids": [movie_id]},
            timeout=10
        )

        print(f"[JELLYFIN ADD] {res.status_code} {res.text}")

        return res.status_code in [200, 204]

    except Exception as e:
        print("Add to collection error:", str(e))
        return False


# -----------------------------
# OPTIONAL: SET COLLECTION POSTER (HOOK)
# -----------------------------
def set_collection_poster(collection_id, image_url=None):
    """
    Optional hook.
    You can later plug TMDB or Radarr artwork here.
    """
    if not image_url:
        return

    try:
        res = requests.post(
            f"{JELLYFIN_URL}/Items/{collection_id}/Images/Primary",
            headers=jellyfin_headers(),
            json={"ImageUrl": image_url},
            timeout=10
        )

        print(f"[POSTER] {res.status_code} {res.text}")

    except Exception as e:
        print("Poster error:", str(e))


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
        "movie_id": movie_id,
        "collections": []
    }

    if not movie_id:
        print(f"[SKIP] Movie not found in Jellyfin: {movie.get('title')}")
        return result

    for collection_name in collections:
        collection_id = get_or_create_collection(collection_name)

        if not collection_id:
            continue

        ok = add_movie_to_collection(collection_id, movie_id)

        if ok:
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
        timeout=20
    )

    res.raise_for_status()

    processed = []

    for movie in res.json():
        processed.append(process_movie(movie))

    return {
        "status": "ok",
        "processed": processed
    }
