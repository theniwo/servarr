# Jellyfin & Radarr Collection Sync Worker

A lightweight Python worker built on **FastAPI** that automatically organizes movies into Jellyfin collections (Boxsets) using tags received via Radarr and Jellyfin webhooks. Additionally, it dynamically generates a 2x2 grid cover (collage) using the artwork of the movies inside the collection whenever a change occurs.

## Features

- **Targeted Library Refresh:** Instead of triggering a slow, global library scan that might lock up on corrupted ISOs or massive music folders, this worker instructs Jellyfin to refresh only the specific movie library using its unique `ItemId`.
- **Dynamic Polling:** Upon receiving a download event from Radarr, the worker actively polls Jellyfin's API (up to 45 seconds) until the movie is fully indexed.
- **Robust Title Fallback:** If the Jellyfin webhook fires before metadata compilation completes (resulting in missing provider IDs or missing years), a fuzzy title-matching algorithm ensures the movie is still accurately matched against Radarr.
- **Automated Collage Generation:** Automatically builds and uploads a sleek 2x2 poster collage using the top four movies of a collection as soon as a movie is added.

## Prerequisites

- Python 3.10+
- Required dependencies: `fastapi`, `uvicorn`, `requests`, `python-dotenv`, `pillow`
- Or just use the [Dockerfile](Dockerfile)
- A running instance of Jellyfin and Radarr

## Installation & Setup

1. Clone or copy the script/repo into your project directory.
2. Create a `.env` file in the same directory:

```ini
# API Connections
RADARR_URL=[http://192.168.1.10:7878](http://192.168.1.10:7878)
RADARR_KEY=your_radarr_api_key

JELLYFIN_URL=[http://192.168.1.10:8096](http://192.168.1.10:8096)
JELLYFIN_KEY=your_jellyfin_api_key

# The specific ItemId of your Jellyfin Movie Library
JELLYFIN_MOVIES_LIBRARY_ID=

# Tag-to-Collection Mapping (TAG_radarrtag=Jellyfin Collection Name)
TAG_biography=Biographical Movies
TAG_marvel=Marvel Cinematic Universe
TAG_oscar=Oscar Winners
```

## How to Find Your JELLYFIN_MOVIES_LIBRARY_ID
Run the following command in your terminal using curl and jq to extract the ItemId of your movie library (e.g., named "Movies" or "Filme") directly from Jellyfin:

```
curl -s "http://YOUR-JELLYFIN-IP:8096/Library/VirtualFolders?api_key=YOUR_JELLYFIN_KEY" | jq -r '.[] | select(.Name == "Movies") | .ItemId'
```

## Running the Application
Start the worker using an ASGI server such as uvicorn:

```
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Docker Deployment

Since you are building the Docker image yourself, you can use the following configuration to containerize the application.

### 1. Create a `Dockerfile`
Place a file named `Dockerfile` in your project root directory next to `main.py`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8787"]

```

### 2. Create a requirements.txt
Ensure your requirements.txt contains the necessary libraries:
```
fastapi==0.110.0
uvicorn==0.28.0
requests==2.31.0
python-dotenv==1.0.1
Pillow==10.2.0
```

### 3. Build and Run the Image
Build your custom local Docker image:

```
docker build -t jellyfin-radarr-sync:latest .
```

Run the container using environment variables from your `.env` file:

```
docker run -d \
  --name jellyfin-radarr-sync \
  -p 8787:8787 \
  --env-file .env \
  --restart unless-stopped \
  jellyfin-radarr-sync:latest
```

Or use it in your compose stack

## Webhook Configuration
1. Configure Radarr
Navigate to Settings -> Connect -> + Add.

Select Webhook.

Name: Collection Sync Worker

Triggers: Check On Download, On Upgrade, and On Movie Delete.

URL: http://YOUR-WORKER-IP:8000/radarr

Method: POST

2. Configure Jellyfin
Install the Webhook plugin from the official Jellyfin repository if you haven't already.

Go to the Dashboard -> Webhooks -> Add Webhook.

Server URL: http://YOUR-WORKER-IP:8096/jellyfin

Item Type: Movie

Hook Events: Check Item Added.



## API Endpoints
POST /radarr - Handles incoming Radarr notifications (triggers the targeted scan and starts polling).

POST /jellyfin - Handles Jellyfin "ItemAdded" events to map and finalize collection assignments.

POST /fullscan - Iterates through the entire Radarr database for retrospective syncing. Append ?flood=true to the URL to disable the built-in 2-second rate-limiting delay between items.

