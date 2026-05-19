import express from "express";
import axios from "axios";

const app = express();
app.use(express.json());

// =====================
// CONFIG
// =====================
const RADARR_URL = process.env.RADARR_URL;
const RADARR_API_KEY = process.env.RADARR_API_KEY;

// =====================
// LOAD TAG MAPPING (ENV)
// TAG_SUBMARINE=submarine
// TAG_WAR=war
// =====================
function loadTagMap() {
  const map = {};

  for (const [key, value] of Object.entries(process.env)) {
    if (!key.startsWith("TAG_")) continue;

    const tagKey = key.slice(4).toLowerCase(); // TAG_SUBMARINE -> submarine
    const collection = (value || tagKey).toLowerCase();

    map[tagKey] = collection;
  }

  return map;
}

// =====================
// RADARR TAGS (ID -> LABEL)
// =====================
async function fetchRadarrTags() {
  const res = await axios.get(`${RADARR_URL}/api/v3/tag`, {
    headers: { "X-Api-Key": RADARR_API_KEY },
  });

  const tagMap = {};
  for (const tag of res.data) {
    tagMap[tag.id] = tag.label.toLowerCase();
  }

  return tagMap;
}

// =====================
// RADARR MOVIES
// =====================
async function fetchMovies() {
  const res = await axios.get(`${RADARR_URL}/api/v3/movie`, {
    headers: { "X-Api-Key": RADARR_API_KEY },
  });

  return res.data;
}

// =====================
// MAIN LOGIC
// =====================
app.post("/fullscan", async (req, res) => {
  try {
    const tagMapEnv = loadTagMap();
    const radarrTagMap = await fetchRadarrTags();
    const movies = await fetchMovies();

    const processed = [];

    for (const movie of movies) {
      const movieTagNames = (movie.tags || [])
        .map((id) => radarrTagMap[id])
        .filter(Boolean)
        .map((t) => t.toLowerCase());

      const collections = [];

      for (const tag of movieTagNames) {
        if (tagMapEnv[tag]) {
          collections.push(tagMapEnv[tag]);
        }
      }

      // dedupe
      const uniqueCollections = [...new Set(collections)];

      processed.push({
        movie: movie.title,
        collections: uniqueCollections,
      });
    }

    res.json({
      status: "ok",
      processed,
    });
  } catch (err) {
    console.error(err);
    res.status(500).json({
      status: "error",
      message: err.message,
    });
  }
});

// =====================
app.listen(8787, () => {
  console.log("Worker running on :8787");
});
