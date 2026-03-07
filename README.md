# yt-dlp wrapper API (Docker Only)

FastAPI service around `yt-dlp`, intended to run only via Docker Compose.

## Prerequisites

- Docker
- Docker Compose

## Run

Create `.env` in the project root with the following values:

```bash
cat > .env <<'EOF'
# Host path mapping (required for saving to host paths via /api/download)
HOST_PATH_SOURCE_PREFIX=/Users
HOST_PATH_MOUNT_PREFIX=/hostfs

# App behavior
LOG_LEVEL=INFO
MAX_ACTIVE_JOBS=2
JOB_ACQUIRE_TIMEOUT_S=2.0
YTDLP_METADATA_TIMEOUT_S=60
YTDLP_TITLE_TIMEOUT_S=30
YTDLP_FILE_DOWNLOAD_TIMEOUT_S=7200
YTDLP_CONCURRENT_FRAGMENTS=1
FFMPEG_THREADS=1
STREAM_CHUNK_SIZE_KB=256

# Container resource limits
CONTAINER_CPUS=2.0
CONTAINER_MEM_LIMIT=2g
CONTAINER_PIDS_LIMIT=256

# Optional override (inside container path)
# YT_DLP_BINARY=/usr/local/bin/yt-dlp
EOF
```

Set host mapping for your OS:

- macOS default: `HOST_PATH_SOURCE_PREFIX=/Users`
- Linux default: `HOST_PATH_SOURCE_PREFIX=/home`
- `HOST_PATH_MOUNT_PREFIX` can stay `/hostfs` (default in this project)

How this mapping works:

- Compose mounts `${HOST_PATH_SOURCE_PREFIX}:${HOST_PATH_MOUNT_PREFIX}`
- If API request uses `download_path` under source prefix, app rewrites it for container writes
- Example: `/Users/anikeshthakur/Downloads` becomes `/hostfs/anikeshthakur/Downloads` inside container
- If `download_path` is outside `HOST_PATH_SOURCE_PREFIX`, mapping is skipped

Then start:

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

`docker-compose.yml` loads values from `.env` (`env_file: .env`).

Recommended tunables:

- `MAX_ACTIVE_JOBS` default `2`
- `YTDLP_CONCURRENT_FRAGMENTS` default `1`
- `FFMPEG_THREADS` default `1`
- `YTDLP_FILE_DOWNLOAD_TIMEOUT_S` default `7200`
- `CONTAINER_CPUS` default `2.0`
- `CONTAINER_MEM_LIMIT` default `2g`
- `CONTAINER_PIDS_LIMIT` default `256`

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
    "download_path": "/Users/anikeshthakur/Downloads/yt",
    "format": { "quality": "720p", "ext": "mp4" }
  }'
```

## Logs

```bash
docker logs -f yt-downloader
```
