#!/usr/bin/env python3
import sys
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("RADARR_API_KEY")

RADARR_URL = os.getenv("RADARR_URL")
TAG_NAME = "submarine"
GENRE_NAME = "Submarine"


def get_tag_id():
    tags = requests.get(
        f"{RADARR_URL}/api/v3/tag",
        headers={"X-Api-Key": API_KEY}
    ).json()

    for t in tags:
        if t["label"].lower() == TAG_NAME.lower():
            return t["id"]
    return None


# 👉 nur EINMAL beim Start laden
TAG_ID = get_tag_id()


def process_movie(movie_id):
    movie = requests.get(
        f"{RADARR_URL}/api/v3/movie/{movie_id}",
        headers={"X-Api-Key": API_KEY}
    ).json()

    if not TAG_ID or TAG_ID not in movie.get("tags", []):
        return

    nfo_file = Path(movie["path"]) / "movie.nfo"
    if not nfo_file.exists():
        return

    tree = ET.parse(nfo_file)
    root = tree.getroot()

    genres = [g.text for g in root.findall("genre")]

    if GENRE_NAME not in genres:
        genre = ET.Element("genre")
        genre.text = GENRE_NAME
        root.append(genre)

        tree.write(nfo_file, encoding="utf-8", xml_declaration=True)
        print(f"[OK] Updated {movie['title']}")


if __name__ == "__main__":
    movie_id = sys.argv[1]
    process_movie(movie_id)
