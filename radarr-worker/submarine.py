import sys
import requests
import os
from dotenv import load_dotenv

load_dotenv()

RADARR_URL = os.getenv("RADARR_URL")
RADARR_KEY = os.getenv("RADARR_API_KEY")

TAG_NAME = "submarine"


def is_test_event(movie_id):
    # Radarr sendet beim Test oft keine echte Movie ID
    return movie_id.lower() == "test"


def get_movie(movie_id):
    return requests.get(
        f"{RADARR_URL}/api/v3/movie/{movie_id}",
        headers={"X-Api-Key": RADARR_KEY}
    ).json()


def get_tag_id():
    tags = requests.get(
        f"{RADARR_URL}/api/v3/tag",
        headers={"X-Api-Key": RADARR_KEY}
    ).json()

    for t in tags:
        if t["label"].lower() == TAG_NAME:
            return t["id"]
    return None


TAG_ID = get_tag_id()


def process(movie_id):
    movie = get_movie(movie_id)

    if TAG_ID not in movie.get("tags", []):
        return

    print(f"[OK] Would process: {movie['title']}")


if __name__ == "__main__":
    movie_id = sys.argv[1]

    # 🧪 TEST HANDLING
    if movie_id.lower() == "test":
        print("[TEST] Radarr hook test received – OK")
        sys.exit(0)

    process(movie_id)
