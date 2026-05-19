import os
import re
import io
import requests
import asyncio
import json
import base64
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from PIL import Image as PILImage

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
# UTILS
# -----------------------------
def clean_title(title):
    """Removes all special characters, spaces, and accents for loose matching."""
    if not title:
        return ""
    title = title.lower()
    title = re.sub(r'[^a-z0-9]', '', title)
    return title


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
# JELLYFIN MAP BUILDERS
# -----------------------------
def build_jellyfin_maps(search_term=None):
    params = {
        "Recursive": "true",
        "IncludeItemTypes": "Movie",
        "Fields": "Path,ProviderIds,Name,OriginalTitle"
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
        return {"tmdb": {}, "imdb": {}, "folder": {}, "title": {}}

    tmdb_map = {}
    imdb_map = {}
    folder_map = {}
    title_map = {}

    for item in res.json().get("Items", []):
        j_id = item.get("Id")
        p_ids = item.get("ProviderIds", {})
        path = item.get("Path")
        name = item.get("Name")
        orig_title = item.get("OriginalTitle")

        if "Tmdb" in p_ids and p_ids["Tmdb"]:
            tmdb_map[str(p_ids["Tmdb"])] = j_id
        if "Imdb" in p_ids and p_ids["Imdb"]:
            imdb_map[str(p_ids["Imdb"])] = j_id

        if path:
            folder_name = os.path.basename(os.path.dirname(path)).lower()
            folder_map[folder_name] = j_id

            file_name, _ = os.path.splitext(os.path.basename(path))
            folder_map[file_name.lower()] = j_id

        if name:
            title_map[clean_title(name)] = j_id
        if orig_title:
            title_map[clean_title(orig_title)] = j_id

    return {
        "tmdb": tmdb_map,
        "imdb": imdb_map,
        "folder": folder_map,
        "title": title_map
    }


def match_movie_to_jellyfin(movie, maps):
    # 1. Try matching by TMDB ID
    tmdb_id = str(movie.get("tmdbId") or "")
    if tmdb_id and tmdb_id in maps["tmdb"]:
        return maps["tmdb"][tmdb_id]

    # 2. Try matching by IMDb ID
    imdb_id = str(movie.get("imdbId") or "")
    if imdb_id and imdb_id in maps["imdb"]:
        return maps["imdb"][imdb_id]

    # 3. Try matching by Cleaned Title
    radarr_title = movie.get("title")
    cleaned_radarr = clean_title(radarr_title)
    if cleaned_radarr and cleaned_radarr in maps["title"]:
        return maps["title"][cleaned_radarr]

    # 4. Try matching by folder base name or file name
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
        params={"Name": name},
        timeout=10
    )

    if res.status_code not in [200, 201]:
        print(f"Failed to create collection: (Status: {res.status_code}) - {res.text}")
        return None

    try:
        return res.json().get("Id")
    except Exception:
        return None


# -----------------------------
# ADD MOVIE TO COLLECTION
# -----------------------------
def add_movie_to_collection(collection_id, movie_id, movie_title, collection_name):
    try:
        res = requests.post(
            f"{JELLYFIN_URL}/Collections/{collection_id}/Items",
            headers=jellyfin_headers(),
            params={"Ids": movie_id},
            timeout=10
        )

        if res.status_code in [200, 204]:
            print(f"[JELLYFIN ADD] Successfully added \"{movie_title}\" to collection \"{collection_name}\" (Status: {res.status_code})")
            return True
        else:
            print(f"[JELLYFIN ADD ERROR] Failed for \"{movie_title}\" to collection \"{collection_name}\" (Status: {res.status_code}) - {res.text}")
            return False

    except Exception as e:
        print(f"Add to collection error for \"{movie_title}\" / \"{collection_name}\":", str(e))
        return False


# -----------------------------
# GENERATE GRID COVER (COLLAGE - FIXED)
# -----------------------------
def generate_collection_collage(collection_id, collection_name):
    """Fetches movies from the collection, builds a 2x2 grid, and uploads it via Base64."""
    try:
        res = requests.get(
            f"{JELLYFIN_URL}/Items",
            headers=jellyfin_headers(),
            params={
                "ParentId": collection_id,
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "Fields": "PrimaryImageTag"
            },
            timeout=10
        )
        res.raise_for_status()
        items = res.json().get("Items", [])

        if len(items) < 2:
            print(f"[POSTER] Not enough movies ({len(items)}) in '{collection_name}' for a collage yet.")
            return

        # Use up to 4 movies for a clean 2x2 grid
        movies_to_use = items[:4]
        images = []

        for movie in movies_to_use:
            m_id = movie["Id"]
            img_res = requests.get(f"{JELLYFIN_URL}/Items/{m_id}/Images/Primary", timeout=10)
            if img_res.status_code == 200:
                try:
                    images.append(PILImage.open(io.BytesIO(img_res.content)))
                except Exception as img_err:
                    print(f"[POSTER WARNING] Skipping broken image for item {m_id}: {img_err}")

        if not images:
            return

        # Standard Jellyfin movie poster aspect ratio (approx. 2:3)
        poster_w, poster_h = 400, 600
        resized_images = [img.resize((poster_w, poster_h), PILImage.Resampling.LANCZOS) for img in images]

        # Assemble the canvas grid based on available image count
        if len(resized_images) >= 4:
            # 2x2 Grid
            canvas = PILImage.new("RGB", (poster_w * 2, poster_h * 2))
            canvas.paste(resized_images[0], (0, 0))
            canvas.paste(resized_images[1], (poster_w, 0))
            canvas.paste(resized_images[2], (0, poster_h))
            canvas.paste(resized_images[3], (poster_w, poster_h))
        else:
            # 2x1 Grid (Side-by-side fallback)
            canvas = PILImage.new("RGB", (poster_w * 2, poster_h))
            canvas.paste(resized_images[0], (0, 0))
            canvas.paste(resized_images[1], (poster_w, 0))

        # Save image to a memory stream buffer
        img_byte_arr = io.BytesIO()
        canvas.save(img_byte_arr, format='JPEG', quality=90)
        img_byte_arr.seek(0)

        # Encode binary data to Base64 string
        base64_encoded = base64.b64encode(img_byte_arr.read()).decode("utf-8")

        # Jellyfin expects a POST to /Images/Primary with the API Key also in the URL parameters
        # to guarantee authentication for multi-part/string payloads.
		# -----------------------------
        # FIXED UPLOAD SECTION
        # -----------------------------
        # Copy headers to avoid modifying global headers for other requests
        upload_headers = jellyfin_headers().copy()
        upload_headers["Content-Type"] = "image/jpeg"  # <-- Fix: Jellyfin tracks the MIME type via this header

        upload_res = requests.post(
            f"{JELLYFIN_URL}/Items/{collection_id}/Images/Primary",
            headers=upload_headers,
            params={"api_key": JELLYFIN_KEY},
            data=base64_encoded,
            timeout=15
        )

        if upload_res.status_code in [200, 204]:
            print(f"[POSTER] Successfully updated grid cover for collection '{collection_name}'")
        else:
            print(f"[POSTER ERROR] Failed to upload collage: (Status: {upload_res.status_code}) - {upload_res.text}")

    except Exception as e:
        print(f"[POSTER ERROR] Failed to create collage for {collection_name}: {e}")

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
    movie_title = movie.get("title")

    result = {
        "movie": movie_title,
        "movie_id": movie_id,
        "collections": []
    }

    if not movie_id:
        print(f"[SKIP] Movie not found in Jellyfin: {movie_title}")
        return result

    for collection_name in collections:
        collection_id = get_or_create_collection(collection_name)
        if not collection_id:
            continue

        ok = add_movie_to_collection(collection_id, movie_id, movie_title, collection_name)
        if ok:
            result["collections"].append(collection_name)
            # Generate or update the grid cover artwork
            generate_collection_collage(collection_id, collection_name)

    return result


# -----------------------------
# JELLYFIN WEBHOOK
# -----------------------------
@app.post("/jellyfin")
def jellyfin_webhook(request: Request, payload: dict = None):
    if payload is None:
        try:
            body = asyncio.run(request.body())
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            print(f"[JELLYFIN ERROR] Failed to parse raw payload: {e}")
            return {"status": "error", "message": "Invalid JSON payload"}

    event = payload.get("Event")

    if not event or "NotificationType" in event or event != "ItemAdded":
        return {"status": "ignored", "event": event}

    if payload.get("ItemType") != "Movie":
        return {"status": "ignored", "item_type": payload.get("ItemType")}

    movie_title = payload.get("Name")
    if not movie_title or "Name" in movie_title:
        return {"status": "ignored", "reason": "Missing or template movie name"}

    print(f"[JELLYFIN WEBHOOK] New movie added to library: {movie_title}")

    try:
        res = requests.get(
            f"{RADARR_URL}/api/v3/movie",
            headers={"X-Api-Key": RADARR_KEY},
            timeout=20
        )
        res.raise_for_status()

        radarr_movie = None
        provider_ids = payload.get("ProviderIds") or {}

        def clean_id(val):
            if not val or "ProviderIds" in str(val):
                return ""
            return str(val).strip()

        jellyfin_tmdb = clean_id(provider_ids.get("Tmdb"))
        jellyfin_imdb = clean_id(provider_ids.get("Imdb"))

        for m in res.json():
            if jellyfin_tmdb and str(m.get("tmdbId")) == jellyfin_tmdb:
                radarr_movie = m
                break
            if jellyfin_imdb and str(m.get("imdbId")) == jellyfin_imdb:
                radarr_movie = m
                break
            if clean_title(m.get("title")) == clean_title(movie_title):
                radarr_movie = m
                break

        if not radarr_movie:
            print(f"[SKIP] Movie \"{movie_title}\" not found in Radarr.")
            return {"status": "not_found_in_radarr"}

        radarr_tags = get_radarr_tags()
        jellyfin_maps = build_jellyfin_maps(search_term=movie_title)

        return {
            "status": "ok",
            "processed": process_movie(radarr_movie, radarr_tags, jellyfin_maps)
        }

    except Exception as e:
        print(f"Error processing Jellyfin webhook: {e}")
        return {"status": "error", "message": str(e)}


# -----------------------------
# RADARR WEBHOOK (FALLBACK)
# -----------------------------
@app.post("/radarr")
def radarr_webhook(payload: dict):
    event = payload.get("eventType")

    if event == "Test":
        return {"status": "ok", "message": "test received"}

    movie = payload.get("movie")
    if not movie:
        return {"status": "ignored"}

    radarr_tags = get_radarr_tags()
    jellyfin_maps = build_jellyfin_maps(search_term=movie.get("title"))

    return {
        "status": "ok",
        "processed": process_movie(movie, radarr_tags, jellyfin_maps)
    }


# -----------------------------
# FULLSCAN
# -----------------------------
@app.post("/fullscan")
def fullscan():
    print("Starting full scan...")

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
