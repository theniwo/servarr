#!/bin/bash
# Healthcheck script to check a service inside the Docker container
URL="127.0.0.1"  # Replace with the appropriate URL or IP:PORT
PORT="8096"
CONTAINER_NAME="servarr-jellyfin-1"
HEALTH_STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER_NAME" 2>/dev/null)
ID=""
# Perform the health check
#if curl --silent --fail --max-time 5 "$URL" > /dev/null; then
if nc -zv "$URL" "$PORT" &> /dev/null; then
  curl -fsS -m 10 --retry 5 -o /dev/null https://hc-ping.com/$ID &>/dev/null
  echo "Service is reachable"
  #exit 0  # Healthy
else
  curl -fsS -m 10 --retry 5 -o /dev/null https://hc-ping.com/$ID/1 &>/dev/null
  echo "Service is unreachable"
  exit 1  # Unhealthy
fi

if  [ "$HEALTH_STATUS" == "unhealthy"  ] &> /dev/null; then
  curl -fsS -m 10 --retry 5 -o /dev/null https://hc-ping.com/$ID/1 &>/dev/null
  echo "Container is unhealthy"
  exit 1  # Unhealthy
fi
if  [ "$HEALTH_STATUS" == "healthy"  ] &> /dev/null; then
  curl -fsS -m 10 --retry 5 -o /dev/null https://hc-ping.com/$ID &>/dev/null
  echo "Container is healthy"
  exit 0  # Healthy
fi

