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
# JELLYFIN MAP BUILDERS (FIXED & OPTIMIZED)
# -----------------------------
def build_jellyfin_maps(search_term=None):
    """
    Fetches movies from Jellyfin and builds lookup maps for fast matching.
    If search_term is provided, it narrows down the scope (used for single webhooks).
    """
    params = {
        "Recursive": "true",
        "IncludeItemTypes": "Movie",
        "Fields": "Path,ProviderIds"
    }
    if search_term:
        params["SearchTerm"] = search_term

    try:
        res = requests.get(
            f"{JELLYFIN_URL}/Items",
            headers=jellyfin_headers(),
            params=params,
            timeout=30
        )
        res.raise_for_status()
    except Exception as e:
        print(f"Failed to fetch items from Jellyfin: {e}")
        return {"tmdb": {}, "imdb": {}, "folder": {}}

    tmdb_map = {}
    imdb_map = {}
    folder_map = {}

    for item in res.json().get("Items", []):
        j_id = item.get("Id")
        p_ids = item.get("ProviderIds", {})
        path = item.get("Path")

        if "Tmdb" in p_ids:
            tmdb_map[str(p_ids["Tmdb"])] = j_id
        if "Imdb" in p_ids:
            imdb_map[str(p_ids["Imdb"])] = j_id

        if path:
            # Fallback: Match by the directory leaf name (e.g., "American Beauty (1999)")
            folder_name = os.path.basename(os.path.dirname(path)).lower()
            folder_map[folder_name] = j_id

    return {"tmdb": tmdb_map, "imdb": imdb_map, "folder": folder_map}


def match_movie_to_jellyfin(movie, maps):
    """Matches a Radarr movie object against pre-built Jellyfin maps."""
    # 1. Try matching by TMDB ID
    tmdb_id = str(movie.get("tmdbId") or "")
    if tmdb_id and tmdb_id in maps["tmdb"]:
        return maps["tmdb"][tmdb_id]

    # 2. Try matching by IMDb ID
    imdb_id = str(movie.get("imdbId") or "")
    if imdb_id and imdb_id in maps["imdb"]:
        return maps["imdb"][imdb_id]

    # 3. Try matching by folder base name (handles different Docker mount roots)
    movie_path = movie.get("folderName") or movie.get("path") or ""
    if movie_path:
        folder_name = os.path.basename(os.path.normpath(movie_path)).lower()
        if folder_name in maps["folder"]:
            return maps["folder"][folder_name]

    return None


# -----------------------------
# GET OR CREATE COLLECTION
# -----------------------------
def get_or_create_collection(name):
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

    print(f"Creating collection: {name}")
    res = requests.post(
        f"{JELLYFIN_URL}/Collections",
        headers=jellyfin_headers(),
        json={"Name": name, "IsLocked": False},
        timeout=10
    )

    if res.status_code not in [200, 201]:
        print("Failed to create collection:", res.text)
        return None

    return res.json().get("Id")


# -----------------------------
# ADD MOVIE TO COLLECTION
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
# CORE LOGIC
# -----------------------------
def process_movie(movie, radarr_tags, jellyfin_maps):
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

    movie_id = match_movie_to_jellyfin(movie, jellyfin_maps)

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
# RADARR WEBHOOK (CHANGED TO SYNC 'def')
# -----------------------------
@app.post("/radarr")
def radarr_webhook(payload: dict):
    event = payload.get("eventType")

    if event == "Test":
        return {"status": "ok", "message": "test received"}

    movie = payload.get("movie")
    if not movie:
        return {"status": "ignored"}

    # Optimization: Build small targeted map for this specific movie title
    radarr_tags = get_radarr_tags()
    jellyfin_maps = build_jellyfin_maps(search_term=movie.get("title"))

    return {
        "status": "ok",
        "processed": process_movie(movie, radarr_tags, jellyfin_maps)
    }


# -----------------------------
# FULLSCAN (CHANGED TO SYNC 'def')
# -----------------------------
@app.post("/fullscan")
def fullscan():
    print("Starting full scan...")

    # Pre-fetch and cache everything once
    radarr_tags = get_radarr_tags()
    jellyfin_maps = build_jellyfin_maps()

    res = requests.get(
        f"{RADARR_URL}/api/v3/movie",
        headers={"X-Api-Key": RADARR_KEY},
        timeout=20
    )
    res.raise_for_status()

    processed = []
    for movie in res.json():
        processed.append(process_movie(movie, radarr_tags, jellyfin_maps))

    return {
        "status": "ok",
        "processed": processed
    }
