# yt-dlp wrapper API (Docker Only)

FastAPI service around `yt-dlp`, intended to run only via Docker Compose.

## Prerequisites

- Docker
- Docker Compose

## Run

```bash
cd /Users/anikeshthakur/Documents/yt-downloader
docker compose up -d --build
```

Health check:

```bash
curl http://localhost:8000/health
```

OpenAPI docs:

- [http://localhost:8000/docs](http://localhost:8000/docs)

## Docker config

`docker-compose.yml` already includes:

- downloads mount: `${DOWNLOADS_HOST_DIR:-./downloads}:/downloads`
- host path passthrough mount: `${HOST_PATH_SOURCE_PREFIX:-/Users}:/hostfs`
- Pi-friendly limits and app tuning envs

Useful env vars you can override:

- `DOWNLOADS_HOST_DIR` default `./downloads`
- `HOST_PATH_SOURCE_PREFIX` default `/Users` (set `/home` on many Linux systems)
- `LOG_LEVEL` default `INFO`
- `MAX_ACTIVE_JOBS` default `2`
- `YTDLP_CONCURRENT_FRAGMENTS` default `1`
- `FFMPEG_THREADS` default `1`
- `CONTAINER_CPUS` default `2.0`
- `CONTAINER_MEM_LIMIT` default `2g`
- `CONTAINER_PIDS_LIMIT` default `256`

Example:

```bash
export DOWNLOADS_HOST_DIR=/Users/anikeshthakur/Downloads
export HOST_PATH_SOURCE_PREFIX=/Users
docker compose up -d --build
```

## API behavior

`POST /api/formats`
- Input: `url`
- Output: available combined qualities from `480p`, `720p`, `1080p`, `1440p`, `2160p`

`POST /api/download`
- Input:
  - `url`
  - `format.quality` one of `480p | 720p | 1080p | 1440p | 2160p`
  - optional `format.ext` (default `mp4`)
  - optional `filename` (defaults to video title)
  - optional `download_path`
- Behavior:
  - no `download_path`: streams file to client
  - with `download_path`: saves on host via mounted path mapping

## Example requests

Get formats:

```bash
curl -X POST http://localhost:8000/api/formats \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.youtube.com/watch?v=b-gjLgT4SUQ"}'
```

Stream download to local curl output:

```bash
curl -X POST http://localhost:8000/api/download \
  -H "Content-Type: application/json" \
  -o test-video.mp4 \
  -d '{
    "url": "https://www.youtube.com/watch?v=b-gjLgT4SUQ",
    "format": { "quality": "720p", "ext": "mp4" }
  }'
```

Save to host absolute path:

```bash
curl -X POST http://localhost:8000/api/download \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=b-gjLgT4SUQ",
    "download_path": "/Users/anikeshthakur/Downloads",
    "format": { "quality": "720p", "ext": "mp4" }
  }'
```

## Logs

```bash
docker logs -f yt-downloader
```
