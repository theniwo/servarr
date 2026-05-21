import os
import re
import io
import requests
import asyncio
import json
import base64
import time
import html  # Needed to parse HTML entities like &#246;
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
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
    """Removes all special characters, spaces, and normalizes umlauts for loose matching."""
    if not title:
        return ""

    # Resolve any HTML encoding and convert to lowercase
    title = html.unescape(title).lower()

    # Normalize German umlauts to their base characters for uniform matching
    umlaut_map = {
        'ä': 'a', 'ö': 'o', 'ü': 'u', 'ß': 'ss',
        'ae': 'a', 'oe': 'o', 'ue': 'u'
    }
    for target, replacement in umlaut_map.items():
        title = title.replace(target, replacement)

    # Strip everything except alphanumeric characters
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
    if not maps:
        return None

    tmdb_id = str(movie.get("tmdbId") or "")
    if tmdb_id and tmdb_id in maps["tmdb"]:
        return maps["tmdb"][tmdb_id]

    imdb_id = str(movie.get("imdbId") or "")
    if imdb_id and imdb_id in maps["imdb"]:
        return maps["imdb"][imdb_id]

    radarr_title = movie.get("title")
    cleaned_radarr = clean_title(radarr_title)
    if cleaned_radarr and cleaned_radarr in maps["title"]:
        return maps["title"][cleaned_radarr]

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
            print(f"[JELLYFIN ADD] Successfully added \"{movie_title}\" to collection \"{collection_name}\"")
            return True
        else:
            print(f"[JELLYFIN ADD ERROR] Failed for \"{movie_title}\" to collection \"{collection_name}\" (Status: {res.status_code}) - {res.text}")
            return False

    except Exception as e:
        print(f"Add to collection error for \"{movie_title}\" / \"{collection_name}\":", str(e))
        return False


# -----------------------------
# REMOVE MOVIE FROM COLLECTION
# -----------------------------
def remove_movie_from_collection(collection_id, movie_id, movie_title, collection_name):
    """Removes a movie from a specific Jellyfin collection."""
    try:
        res = requests.delete(
            f"{JELLYFIN_URL}/Collections/{collection_id}/Items",
            headers=jellyfin_headers(),
            params={"Ids": movie_id},
            timeout=10
        )

        if res.status_code in [200, 204]:
            print(f"[JELLYFIN REMOVE] Successfully removed \"{movie_title}\" from collection \"{collection_name}\"")
            return True
        else:
            print(f"[JELLYFIN REMOVE ERROR] Failed for \"{movie_title}\" from collection \"{collection_name}\" (Status: {res.status_code}) - {res.text}")
            return False
    except Exception as e:
        print(f"Remove from collection error for \"{movie_title}\" / \"{collection_name}\":", str(e))
        return False


# -----------------------------
# JELLYFIN COLLECTION ITEM COUNT
# -----------------------------
def get_collection_item_count(collection_id):
    """Counts how many movies are currently inside a specific collection."""
    try:
        res = requests.get(
            f"{JELLYFIN_URL}/Items",
            headers=jellyfin_headers(),
            params={
                "ParentId": collection_id,
                "Recursive": "true",
                "IncludeItemTypes": "Movie"
            },
            timeout=10
        )
        if res.status_code == 200:
            return len(res.json().get("Items", []))
    except Exception as e:
        print(f"[CLEANUP ERROR] Failed to count items for collection {collection_id}: {e}")
    return 0


# -----------------------------
# DELETE EMPTY OR SINGLETON COLLECTIONS
# -----------------------------
def cleanup_orphan_collections():
    """Deletes collections from collection_map that contain 1 or 0 movies."""
    print("[CLEANUP] Checking for empty or single-movie collections...")
    try:
        res = requests.get(
            f"{JELLYFIN_URL}/Items",
            headers=jellyfin_headers(),
            params={
                "Recursive": "true",
                "IncludeItemTypes": "BoxSet"
            },
            timeout=10
        )
        res.raise_for_status()
        jellyfin_collections = res.json().get("Items", [])

        known_collection_names = set(collection_map.values())

        for col in jellyfin_collections:
            col_name = col.get("Name")
            col_id = col.get("Id")

            if col_name in known_collection_names:
                item_count = get_collection_item_count(col_id)
                if item_count <= 1:
                    print(f"[CLEANUP] Deleting collection '{col_name}' because it contains only {item_count} movie(s).")
                    del_res = requests.delete(
                        f"{JELLYFIN_URL}/Items/{col_id}",
                        headers=jellyfin_headers(),
                        timeout=10
                    )
                    if del_res.status_code not in [200, 204]:
                        print(f"[CLEANUP ERROR] Failed to delete collection '{col_name}': {del_res.text}")
    except Exception as e:
        print(f"[CLEANUP ERROR] Global collection cleanup failed: {e}")


# -----------------------------
# GENERATE GRID COVER (COLLAGE)
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

        poster_w, poster_h = 400, 600
        resized_images = [img.resize((poster_w, poster_h), PILImage.Resampling.LANCZOS) for img in images]

        if len(resized_images) >= 4:
            canvas = PILImage.new("RGB", (poster_w * 2, poster_h * 2))
            canvas.paste(resized_images[0], (0, 0))
            canvas.paste(resized_images[1], (poster_w, 0))
            canvas.paste(resized_images[2], (0, poster_h))
            canvas.paste(resized_images[3], (poster_w, poster_h))
        else:
            canvas = PILImage.new("RGB", (poster_w * 2, poster_h))
            canvas.paste(resized_images[0], (0, 0))
            canvas.paste(resized_images[1], (poster_w, 0))

        img_byte_arr = io.BytesIO()
        canvas.save(img_byte_arr, format='JPEG', quality=90)
        img_byte_arr.seek(0)

        base64_encoded = base64.b64encode(img_byte_arr.read()).decode("utf-8")

        upload_headers = jellyfin_headers().copy()
        upload_headers["Content-Type"] = "image/jpeg"

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
def process_movie(movie, radarr_tags, jellyfin_maps, enable_cleanup=False):
    tag_names = [
        radarr_tags[t].lower().strip()
        for t in movie.get("tags", [])
        if t in radarr_tags
    ]

    active_collections = [
        collection_map[t]
        for t in tag_names
        if t in collection_map
    ]

    movie_id = match_movie_to_jellyfin(movie, jellyfin_maps)
    movie_title = movie.get("title")

    result = {
        "movie": movie_title,
        "movie_id": movie_id,
        "added_to": [],
        "removed_from": []
    }

    if not movie_id:
        print(f"[SKIP] Movie not found in Jellyfin: {movie_title}")
        return result

    # 1. ADD to collections matching active tags
    for collection_name in active_collections:
        collection_id = get_or_create_collection(collection_name)
        if not collection_id:
            continue

        ok = add_movie_to_collection(collection_id, movie_id, movie_title, collection_name)
        if ok:
            result["added_to"].append(collection_name)
            generate_collection_collage(collection_id, collection_name)

    # 2. REMOVE from collections (only if enable_cleanup is True)
    if enable_cleanup:
        for tag_key, collection_name in collection_map.items():
            if collection_name not in active_collections:
                check_res = requests.get(
                    f"{JELLYFIN_URL}/Items",
                    headers=jellyfin_headers(),
                    params={"Recursive": "true", "IncludeItemTypes": "BoxSet", "SearchTerm": collection_name},
                    timeout=10
                )
                collection_id = None
                if check_res.status_code == 200:
                    for item in check_res.json().get("Items", []):
                        if item["Name"].lower() == collection_name.lower():
                            collection_id = item["Id"]
                            break

                if collection_id:
                    ok = remove_movie_from_collection(collection_id, movie_id, movie_title, collection_name)
                    if ok:
                        result["removed_from"].append(collection_name)
                        generate_collection_collage(collection_id, collection_name)

    return result


# -----------------------------
# JELLYFIN WEBHOOK
# -----------------------------
@app.post("/jellyfin")
async def jellyfin_webhook(request: Request):
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8").strip()

        if not body_str:
            print("[JELLYFIN ERROR] Received empty body")
            return {"status": "error", "message": "Empty body"}

        payload = json.loads(body_str)
    except Exception as e:
        print(f"[JELLYFIN ERROR] Failed to parse raw payload: {e}")
        return {"status": "error", "message": "Invalid JSON format"}

    event = payload.get("Event")

    if not event or "NotificationType" in event or event != "ItemAdded":
        return {"status": "ignored", "event": event}

    if payload.get("ItemType") != "Movie":
        return {"status": "ignored", "item_type": payload.get("ItemType")}

    jellyfin_name_raw = payload.get("Name", "Unknown")
    jellyfin_name = html.unescape(jellyfin_name_raw)  # Resolve HTML characters early

    jellyfin_year = payload.get("Year")
    provider_ids = payload.get("ProviderIds") or {}

    def clean_id(val):
        if not val or "ProviderIds" in str(val):
            return ""
        return str(val).strip()

    jellyfin_tmdb = clean_id(provider_ids.get("Tmdb"))
    jellyfin_imdb = clean_id(provider_ids.get("Imdb"))

    print(f"[JELLYFIN WEBHOOK] Processing '{jellyfin_name}' (TMDB: {jellyfin_tmdb} | IMDB: {jellyfin_imdb})")

    try:
        res = requests.get(
            f"{RADARR_URL}/api/v3/movie",
            headers={"X-Api-Key": RADARR_KEY},
            timeout=20
        )
        res.raise_for_status()

        radarr_movie = None
        jellyfin_title_clean = re.sub(r'\s*\(\d{4}\)\s*$', '', jellyfin_name)
        cleaned_jellyfin_title = clean_title(jellyfin_title_clean)

        for m in res.json():
            radarr_tmdb = str(m.get("tmdbId", "")).strip()
            radarr_imdb = str(m.get("imdbId", "")).strip()
            radarr_title = str(m.get("title", "")).strip()
            radarr_year = str(m.get("year", "")).strip()

            if jellyfin_tmdb and radarr_tmdb == jellyfin_tmdb:
                radarr_movie = m
                break
            if jellyfin_imdb and radarr_imdb == jellyfin_imdb:
                radarr_movie = m
                break

            cleaned_radarr_title = clean_title(radarr_title)
            if cleaned_radarr_title == cleaned_jellyfin_title:
                jelly_year_str = str(jellyfin_year or "").strip()
                if not jelly_year_str or jelly_year_str == "None" or radarr_year == jelly_year_str:
                    print(f"[FALLBACK MATCH] Found loose match via title for '{jellyfin_name}' (Radarr Year: {radarr_year})")
                    radarr_movie = m
                    break

        if not radarr_movie:
            print(f"[SKIP] No movie found in Radarr matching IDs or Title fallback for '{jellyfin_name}'")
            return {"status": "not_found_in_radarr"}

        print(f"[MATCH SUCCESS] Resolved '{jellyfin_name}' to Radarr Movie: '{radarr_movie.get('title')}'")

        radarr_tags = get_radarr_tags()
        jellyfin_maps = build_jellyfin_maps()

        # Safe add only (enable_cleanup=False), preventing unwanted pruning routines
        result = process_movie(radarr_movie, radarr_tags, jellyfin_maps, enable_cleanup=False)

        return {"status": "ok", "processed": result}

    except Exception as e:
        print(f"Error processing Jellyfin webhook: {e}")
        return {"status": "error", "message": str(e)}


# -----------------------------
# RADARR WEBHOOK
# -----------------------------
@app.post("/radarr")
async def radarr_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception as e:
        print(f"[RADARR ERROR] Failed to parse JSON: {e}")
        return {"status": "error", "message": "Invalid JSON"}

    event_type = payload.get("eventType")
    movie_data = payload.get("movie", {})
    movie_title = movie_data.get("title", "Unknown")
    tmdb_id = str(movie_data.get("tmdbId", ""))

    print(f"\n[RADARR TRIGGER] Event '{event_type}' received for: '{movie_title}'")

    if event_type in ["MovieDelete", "MovieFileDelete"]:
        print(f"[DELETE] Movie '{movie_title}' was deleted from Radarr. Syncing Jellyfin...")
        try:
            requests.post(f"{JELLYFIN_URL}/Library/Refresh", headers={"X-MediaBrowser-Token": JELLYFIN_KEY}, timeout=10)
        except Exception as e:
            print(f"[WARNING] Could not trigger Jellyfin scan: {e}")

        time.sleep(3)
        jellyfin_maps = build_jellyfin_maps()
        radarr_tags = get_radarr_tags()

        # Destructive context: retention cleanup permitted
        result = process_movie(movie_data, radarr_tags, jellyfin_maps, enable_cleanup=True)
        cleanup_orphan_collections()
        return {"status": "ok", "action": "deleted", "result": result}

    elif event_type in ["Download", "Upgrade", "MovieUpdate"]:
        if not tmdb_id:
            print("[SKIP] No TMDB ID provided by Radarr. Cannot sync.")
            return {"status": "missing_tmdb_id"}

        JELLYFIN_MOVIES_LIBRARY_ID = os.getenv("JELLYFIN_MOVIES_LIBRARY_ID", "f137a2dd21bbc1b99aa5c0f6bf02a805")

        if event_type in ["Download", "Upgrade"]:
            try:
                requests.post(
                    f"{JELLYFIN_URL}/Items/{JELLYFIN_MOVIES_LIBRARY_ID}/Refresh",
                    headers=jellyfin_headers(),
                    params={
                        "Recursive": "true",
                        "ImageRefreshMode": "Default",
                        "MetadataRefreshMode": "Default",
                        "ReplaceAllImages": "false",
                        "ReplaceAllMetadata": "false"
                    },
                    timeout=10
                )
            except Exception as e:
                print(f"[WARNING] Could not trigger targeted Jellyfin library scan: {e}")

        jellyfin_maps = None
        movie_found = False

        for attempt in range(15):
            try:
                jellyfin_maps = build_jellyfin_maps()
                if match_movie_to_jellyfin(movie_data, jellyfin_maps):
                    movie_found = True
                    break
            except Exception:
                pass
            time.sleep(3)

        if not movie_found:
            jellyfin_maps = build_jellyfin_maps()

        try:
            radarr_tags = get_radarr_tags()

            # Importing context: Additive only (enable_cleanup=False) to shield system against log floods
            result = process_movie(movie_data, radarr_tags, jellyfin_maps, enable_cleanup=False)
            return {"status": "ok", "action": "processed", "result": result}
        except Exception as e:
            print(f"[ERROR] Failed to process collection for '{movie_title}': {e}")
            return {"status": "error", "message": str(e)}

    else:
        return {"status": "ignored", "reason": f"Event type '{event_type}' not handled"}


# -----------------------------
# SINGLE MOVIE SYNC
# -----------------------------
@app.post("/sync/{radarr_movie_id}")
def sync_single_movie(radarr_movie_id: int):
    print(f"Starting targeted sync for Radarr Movie ID / TMDB ID: {radarr_movie_id}...")

    try:
        res = requests.get(
            f"{RADARR_URL}/api/v3/movie",
            headers={"X-Api-Key": RADARR_KEY},
            timeout=20
        )
        res.raise_for_status()
        movies = res.json()

        movie = next(
            (m for m in movies if str(m.get("id")) == str(radarr_movie_id) or str(m.get("tmdbId")) == str(radarr_movie_id)),
            None
        )

        if not movie:
            return {"status": "error", "message": f"Movie with ID {radarr_movie_id} not found in Radarr."}

        print(f"[SYNC] Processing movie: '{movie.get('title')}' (Internal ID: {movie.get('id')} | TMDB: {movie.get('tmdbId')})")

        radarr_tags = get_radarr_tags()
        jellyfin_maps = build_jellyfin_maps()

        # Intentional tag adjustment context: enable full pruning logic
        result = process_movie(movie, radarr_tags, jellyfin_maps, enable_cleanup=True)
        cleanup_orphan_collections()

        return {"status": "ok", "processed": result}

    except Exception as e:
        print(f"[ERROR] Failed targeted sync for ID {radarr_movie_id}: {e}")
        return {"status": "error", "message": str(e)}


# -----------------------------
# STREAMED FULLSCAN (ONLY PROGRESS BAR)
# -----------------------------
@app.post("/fullscan")
def fullscan(flood: bool = False):
    print(f"Starting full scan (Flood-Mode: {flood})...")

    def progress_generator():
        radarr_tags = get_radarr_tags()
        jellyfin_maps = build_jellyfin_maps()

        res = requests.get(
            f"{RADARR_URL}/api/v3/movie",
            headers={"X-Api-Key": RADARR_KEY},
            timeout=20
        )
        res.raise_for_status()

        movies = res.json()
        total_movies = len(movies)

        for index, movie in enumerate(movies, start=1):
            process_movie(movie, radarr_tags, jellyfin_maps, enable_cleanup=False)

            percent = (index / total_movies) * 100
            bar_length = 30
            filled_length = int(bar_length * index // total_movies)
            bar = '█' * filled_length + '-' * (bar_length - filled_length)

            yield f"\rProgress: |{bar}| {index}/{total_movies} Movies ({percent:.1f}%) \033[K"

            if not flood and index < total_movies:
                time.sleep(2)

        yield "\n[CLEANUP] Starting collection cleanup...\n"
        cleanup_orphan_collections()

        yield "Full scan completed successfully.\n"

    return StreamingResponse(progress_generator(), media_type="text/plain")
