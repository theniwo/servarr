import os
import re
import time
import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response, JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from contextlib import asynccontextmanager

# -----------------------------
# CONFIGURATION & DEFAULTS
# -----------------------------
RADARR_URL = os.getenv("RADARR_URL", "http://localhost:7878")
RADARR_KEY = os.getenv("RADARR_KEY", "")
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "http://localhost:8096")
JELLYFIN_KEY = os.getenv("JELLYFIN_KEY", "")
CRON_TIME = os.getenv("CRON_TIME", "0 3 * * *")

# -----------------------------
# HELPER FUNCTIONS
# -----------------------------
def jellyfin_headers():
    return {
        "Authorization": f'MediaBrowser Token="{JELLYFIN_KEY}"',
        "Accept": "application/json"
    }


def clean_title(title: str) -> str:
    if not title:
        return ""
    # Lowercase, remove special characters and extra spaces
    title_clean = title.lower()
    title_clean = re.sub(r'[^a-z0-9\s]', '', title_clean)
    return " ".join(title_clean.split())


def get_radarr_tags():
    try:
        res = requests.get(
            f"{RADARR_URL}/api/v3/tag",
            headers={"X-Api-Key": RADARR_KEY},
            timeout=10
        )
        res.raise_for_status()
        return {tag["label"].lower(): tag["id"] for tag in res.json()}
    except Exception as e:
        print(f"Failed to fetch tags from Radarr: {e}")
        return {}


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

    # 1. Match via TMDB ID
    tmdb_id = str(movie.get("tmdbId") or "")
    if tmdb_id and tmdb_id in maps["tmdb"]:
        return maps["tmdb"][tmdb_id]

    # 2. Match via IMDB ID
    imdb_id = str(movie.get("imdbId") or "")
    if imdb_id and imdb_id in maps["imdb"]:
        return maps["imdb"][imdb_id]

    # 3. Match via Title Lookup
    radarr_title = movie.get("title")
    cleaned_radarr = clean_title(radarr_title)
    if cleaned_radarr and cleaned_radarr in maps["title"]:
        return maps["title"][cleaned_radarr]

    # 4. Match via Folder Name (Fallback for mismatching titles / missing IDs)
    movie_path = movie.get("folderName") or movie.get("path") or ""
    if movie_path:
        folder_name = os.path.basename(os.path.normpath(movie_path)).lower()

        if folder_name in maps["folder"]:
            return maps["folder"][folder_name]

        # Direct token fallback verification
        for j_folder, j_id in maps["folder"].items():
            if folder_name == j_folder or folder_name in j_folder or j_folder in folder_name:
                print(f"[FOLDER DIRECT MATCH] Matched Radarr folder '{folder_name}' with Jellyfin folder '{j_folder}'")
                return j_id

    return None


# -----------------------------
# CORE LOGICS & SYNC WORKERS
# -----------------------------
def create_jellyfin_collection(name: str) -> str:
    try:
        res = requests.post(
            f"{JELLYFIN_URL}/Collections",
            headers=jellyfin_headers(),
            params={"Name": name},
            timeout=10
        )
        res.raise_for_status()
        return res.json().get("Id")
    except Exception as e:
        print(f"Failed to create collection '{name}': {e}")
        return ""


def get_or_create_collection(name: str) -> str:
    try:
        res = requests.get(
            f"{JELLYFIN_URL}/Items",
            headers=jellyfin_headers(),
            params={"IncludeItemTypes": "BoxSet", "Recursive": "true", "SearchTerm": name},
            timeout=10
        )
        res.raise_for_status()
        items = res.json().get("Items", [])
        for item in items:
            if item.get("Name", "").lower() == name.lower():
                return item.get("Id")
        return create_jellyfin_collection(name)
    except Exception as e:
        print(f"Error checking collection '{name}': {e}")
        return ""


def cleanup_orphan_collections():
    print("[CLEANUP] Searching for empty or single-movie collections...")
    try:
        res = requests.get(
            f"{JELLYFIN_URL}/Items",
            headers=jellyfin_headers(),
            params={"IncludeItemTypes": "BoxSet", "Recursive": "true"},
            timeout=20
        )
        res.raise_for_status()
        collections = res.json().get("Items", [])

        for col in collections:
            col_id = col.get("Id")
            col_name = col.get("Name")

            item_res = requests.get(
                f"{JELLYFIN_URL}/Items",
                headers=jellyfin_headers(),
                params={"ParentId": col_id, "Recursive": "true"},
                timeout=15
            )
            item_res.raise_for_status()
            child_count = len(item_res.json().get("Items", []))

            if child_count <= 1:
                print(f"[CLEANUP] Removing collection '{col_name}' (Contains {child_count} items)")
                del_res = requests.delete(f"{JELLYFIN_URL}/Items/{col_id}", headers=jellyfin_headers(), timeout=10)
                del_res.raise_for_status()
    except Exception as e:
        print(f"[CLEANUP ERROR] Failed during collection cleanup step: {e}")


def process_movie(movie, radarr_tags, jellyfin_maps, enable_cleanup=True):
    movie_title = movie.get("title")
    movie_tags = movie.get("tags", [])

    jelly_movie_id = match_movie_to_jellyfin(movie, jellyfin_maps)
    if not jelly_movie_id:
        print(f"[SKIP] Movie not found in Jellyfin: {movie_title}")
        return

    # Invert tags map to resolve IDs to Labels
    tag_id_to_label = {v: k for k, v in radarr_tags.items()}

    for tag_id, tag_label in tag_id_to_label.items():
        # Tag formatting validation
        if not (tag_label.startswith("c.") or tag_label.startswith("collection.")):
            continue

        clean_collection_name = tag_label.split(".", 1)[1].strip()
        collection_id = get_or_create_collection(clean_collection_name)

        if not collection_id:
            continue

        if tag_id in movie_tags:
            print(f"[ADD] Adding '{movie_title}' to collection '{clean_collection_name}'")
            try:
                add_res = requests.post(
                    f"{JELLYFIN_URL}/Collections/{collection_id}/Items",
                    headers=jellyfin_headers(),
                    params={"Ids": jelly_movie_id},
                    timeout=10
                )
                add_res.raise_for_status()
            except Exception as e:
                print(f"Failed to add item to collection: {e}")
        elif enable_cleanup:
            # Inline removal strategy for standalone targeted /sync updates
            try:
                requests.delete(
                    f"{JELLYFIN_URL}/Collections/{collection_id}/Items",
                    headers=jellyfin_headers(),
                    params={"Ids": jelly_movie_id},
                    timeout=10
                ).raise_for_status()
            except Exception:
                pass


def execute_rigorous_cleanup():
    print("[CLEANUP WORKER] Starting rigorous collection sync...")
    try:
        radarr_tags = get_radarr_tags()
        jellyfin_maps = build_jellyfin_maps()

        res = requests.get(
            f"{RADARR_URL}/api/v3/movie",
            headers={"X-Api-Key": RADARR_KEY},
            timeout=20
        )
        res.raise_for_status()
        radarr_movies = {str(m.get("tmdbId")): m for m in res.json() if m.get("tmdbId")}

        coll_res = requests.get(
            f"{JELLYFIN_URL}/Items",
            headers=jellyfin_headers(),
            params={"IncludeItemTypes": "BoxSet", "Recursive": "true"},
            timeout=20
        )
        coll_res.raise_for_status()
        collections = coll_res.json().get("Items", [])

        stats_removed = 0

        for collection in collections:
            coll_id = collection.get("Id")
            coll_name = collection.get("Name")

            target_tag_id = None
            for tag_name, tag_id in radarr_tags.items():
                if tag_name.lower() == coll_name.lower():
                    target_tag_id = tag_id
                    break

            if not target_tag_id:
                continue

            item_res = requests.get(
                f"{JELLYFIN_URL}/Items",
                headers=jellyfin_headers(),
                params={"ParentId": coll_id, "Recursive": "true", "IncludeItemTypes": "Movie"},
                timeout=20
            )
            item_res.raise_for_status()
            jelly_movies = item_res.json().get("Items", [])

            for j_movie in jelly_movies:
                j_movie_id = j_movie.get("Id")
                p_ids = j_movie.get("ProviderIds", {})
                j_tmdb_id = str(p_ids.get("Tmdb", ""))

                should_keep = False
                if j_tmdb_id in radarr_movies:
                    r_movie = radarr_movies[j_tmdb_id]
                    if target_tag_id in r_movie.get("tags", []):
                        should_keep = True

                if not should_keep:
                    print(f"[CLEANUP WORKER] Removing '{j_movie.get('Name')}' from collection '{coll_name}'")
                    try:
                        requests.delete(
                            f"{JELLYFIN_URL}/Collections/{coll_id}/Items",
                            headers=jellyfin_headers(),
                            params={"Ids": j_movie_id},
                            timeout=10
                        ).raise_for_status()
                        stats_removed += 1
                    except Exception as e:
                        print(f"[CLEANUP WORKER ERROR] Failed to remove item {j_movie_id}: {e}")

        cleanup_orphan_collections()
        print(f"[CLEANUP WORKER] Completed. Removed {stats_removed} misplaced movies.")
        return stats_removed
    except Exception as e:
        print(f"[CLEANUP WORKER ERROR] Execution failed: {e}")
        return 0


# -----------------------------
# BACKGROUND CRON SCHEDULER
# -----------------------------
def scheduled_fullscan():
    print("\n[CRON] Starting scheduled nightly full sync routine...")
    try:
        radarr_tags = get_radarr_tags()
        jellyfin_maps = build_jellyfin_maps()

        res = requests.get(
            f"{RADARR_URL}/api/v3/movie",
            headers={"X-Api-Key": RADARR_KEY},
            timeout=20
        )
        res.raise_for_status()
        movies = res.json()

        print(f"[CRON] Phase 1: Syncing {len(movies)} movies (adding missing)...")
        for movie in movies:
            process_movie(movie, radarr_tags, jellyfin_maps, enable_cleanup=False)

        print("[CRON] Phase 2: Running rigorous cleanup for removed tags/movies...")
        removed_count = execute_rigorous_cleanup()

        print(f"[CRON] Scheduled nightly routine completed. Evicted {removed_count} movies.\n")
    except Exception as e:
        print(f"[CRON ERROR] Scheduled routine failed: {e}")


def parse_cron_variable(cron_string: str):
    if not cron_string:
        return None

    cron_clean = cron_string.strip().lower()
    if cron_clean in ["false", "disabled", "0", "none"]:
        return None

    alias_map = {
        "@hourly":   "0 * * * *",
        "@daily":    "0 0 * * *",
        "@midnight": "0 0 * * *",
        "@weekly":   "0 0 * * 0",
        "@monthly":  "1 0 0 * *",
        "@yearly":   "0 0 1 1 *",
        "@annually": "0 0 1 1 *"
    }

    if cron_clean in alias_map:
        cron_clean = alias_map[cron_clean]

    fields = cron_clean.split()
    if len(fields) != 5:
        print(f"[CRON ERROR] Invalid cron expression format: '{cron_string}'. Expected 5 fields.")
        return None

    try:
        return CronTrigger(
            minute=fields[0],
            hour=fields[1],
            day=fields[2],
            month=fields[3],
            day_of_week=fields[4]
        )
    except Exception as e:
        print(f"[CRON ERROR] Failed to parse cron fields '{cron_string}': {e}")
        return None


scheduler = BackgroundScheduler()
trigger = parse_cron_variable(CRON_TIME)

if trigger:
    scheduler.add_job(scheduled_fullscan, trigger=trigger)
    print(f"[CRON CONFIG] Scheduled background sync active with expression: '{CRON_TIME}'")
else:
    print("[CRON CONFIG] Background scheduler is DISABLED via environment configuration.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if trigger:
        print("[STARTUP] Launching background cron scheduler...")
        scheduler.start()
    else:
        print("[STARTUP] Skipping scheduler startup (disabled).")
    yield
    if trigger and scheduler.running:
        print("[SHUTDOWN] Shutting down background cron scheduler...")
        scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


# -----------------------------
# HTTP ENDPOINTS & WEBHOOKS
# -----------------------------
@app.post("/radarr")
async def radarr_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    event_type = payload.get("eventType")
    movie_data = payload.get("movie", {})
    movie_id = movie_data.get("id")

    if event_type == "Test":
        return {"status": "success", "message": "Test event received"}

    if movie_id:
        print(f"Received Radarr event '{event_type}' for Movie ID: {movie_id}. Queueing targeted sync.")
        background_tasks.add_task(trigger_targeted_sync, movie_id)
        return {"status": "accepted", "message": "Sync task queued"}

    return {"status": "ignored", "message": "Event configuration bypassed"}


@app.post("/sync/{movie_id}")
async def manual_targeted_sync(movie_id: int):
    print(f"Starting manual targeted sync for Radarr Movie ID: {movie_id}...")
    result = trigger_targeted_sync(movie_id)
    return {"status": "success", "processed": result}


@app.post("/cleanup", status_code=200)
async def manual_rigorous_cleanup():
    removed_count = execute_rigorous_cleanup()
    return {"status": "success", "removed_movies_count": removed_count}


def trigger_targeted_sync(movie_id: int):
    try:
        radarr_tags = get_radarr_tags()
        jellyfin_maps = build_jellyfin_maps()

        res = requests.get(
            f"{RADARR_URL}/api/v3/movie/{movie_id}",
            headers={"X-Api-Key": RADARR_KEY},
            timeout=10
        )
        res.raise_for_status()
        movie = res.json()

        process_movie(movie, radarr_tags, jellyfin_maps, enable_cleanup=True)
        cleanup_orphan_collections()

        return {"movie": movie.get("title"), "movie_id": movie_id}
    except Exception as e:
        print(f"[SYNC ERROR] Targeted synchronization execution failed: {e}")
        return {"movie": "Unknown", "movie_id": movie_id, "error": str(e)}


# -----------------------------
# CATCH-ALL FOR BOT SCANS (SILENT 404)
# -----------------------------
@app.api_route("/{path_name:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def catch_all(request: Request, path_name: str):
    bot_keywords = [".env", "wp-admin", "xmlrpc", "config", "setup", "php", "actuator", ".json", ".aws"]

    if any(keyword in path_name.lower() for keyword in bot_keywords) or path_name == "":
        return Response(status_code=404)

    print(f"[404] Unknown route accessed: {path_name}")
    return JSONResponse(status_code=404, content={"detail": "Not Found"})
