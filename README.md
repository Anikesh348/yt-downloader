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
YTDLP_FILE_DOWNLOAD_TIMEOUT_S=7200
YTDLP_CONCURRENT_FRAGMENTS=1
FFMPEG_THREADS=1
HEIGHT_NEAR_TOLERANCE=24
YTDLP_JS_RUNTIMES=node
YTDLP_REMOTE_COMPONENTS=ejs:github
# Safer default YouTube extractor profile (4K best-effort without PO token):
YTDLP_EXTRACTOR_ARGS=youtube:player_client=default,-ios
# Optional: separate extractor args for download/preflight commands.
# By default this inherits YTDLP_EXTRACTOR_ARGS when omitted or left empty.
# Set to "none" only if you intentionally want no extractor args:
# YTDLP_DOWNLOAD_EXTRACTOR_ARGS=
# Optional: PO token extractor args for protected streams (enables exact high qualities where available):
# YTDLP_PO_TOKEN_ARGS=youtube:po_token=android.gvs+<TOKEN>
# Optional advanced mode (only with valid PO token): include missing_pot formats.
# Example:
# YTDLP_EXTRACTOR_ARGS=youtube:player_client=android,web;formats=missing_pot
# YTDLP_PO_TOKEN_ARGS=youtube:po_token=android.gvs+<TOKEN>
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
- `HEIGHT_NEAR_TOLERANCE` default `24`
- `YTDLP_JS_RUNTIMES` default `node`
- `YTDLP_REMOTE_COMPONENTS` default `ejs:github`
- `YTDLP_EXTRACTOR_ARGS` default `youtube:player_client=default,-ios`
- `YTDLP_DOWNLOAD_EXTRACTOR_ARGS` defaults to `YTDLP_EXTRACTOR_ARGS` when omitted/empty (set `none` to disable for download/preflight path)
- `YTDLP_PO_TOKEN_ARGS` default empty (optional; required for some token-protected high-quality formats)
- `YTDLP_FILE_DOWNLOAD_TIMEOUT_S` default `7200`
- `CONTAINER_CPUS` default `2.0`
- `CONTAINER_MEM_LIMIT` default `2g`
- `CONTAINER_PIDS_LIMIT` default `256`

## API behavior

`POST /api/formats`
- Input: `url`
- Output: available combined qualities from `360p`, `480p`, `720p`, `1080p`, `1440p`, `2160p`
- Near heights are rounded to the closest supported label within `HEIGHT_NEAR_TOLERANCE` pixels (for example, `718 -> 720p`, `1078 -> 1080p` with default tolerance `24`)

`POST /api/download`
- Input:
  - `videoId`
  - `format.quality` one of `360p | 480p | 720p | 1080p | 1440p | 2160p`
  - optional `format.ext` (default `mp4`)
  - optional `filename` (defaults to video title)
  - optional `download_path`
  - optional `progress_updates` (default `false`, works only with `download_path`)
- Behavior:
  - no `download_path`: streams file to client
  - with `download_path` + `progress_updates=false`: starts background download and returns immediately with `202` + `status=started`
  - with `download_path` + `progress_updates=true`: returns Server-Sent Events (`text/event-stream`) with ongoing status updates (`status`, `phase`, `progress_percent`, `eta`, `speed`)
  - download now uses runtime descending selection (`requested -> next lower -> ... -> 360p`) with `--check-formats`, so the service always tries highest quality first and auto-falls back when unavailable
  - for `download_path` modes, final `selected_quality` is resolved from the downloaded file dimensions (not just preflight metadata)
  - when the final quality is lower than requested and no PO token is configured, fallback detail includes guidance to configure `YTDLP_PO_TOKEN_ARGS`

`GET /api/download/status/{videoId}`
- Output: current in-memory status for that `videoId` (`starting`, `downloading`, `postprocessing`, `downloaded`, `failed`, or `not_started`)

`GET /api/download/status/stream/{videoId}`
- Output: SSE stream of status updates for that `videoId` (events: `progress`, `complete`, `error`)

`GET /api/download/running`
- Output: lightweight runtime state for active yt-dlp download/post-process jobs (`running`, `active_downloads`)

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
    "videoId": "b-gjLgT4SUQ",
    "format": { "quality": "720p", "ext": "mp4" }
  }'
```

Save to host absolute path:

```bash
curl -X POST http://localhost:8000/api/download \
  -H "Content-Type: application/json" \
  -d '{
    "videoId": "b-gjLgT4SUQ",
    "download_path": "/Users/anikeshthakur/Downloads/yt",
    "format": { "quality": "720p", "ext": "mp4" }
  }'
```

This request returns immediately with a `202` response and `status: "started"`.  
Use `GET /api/download/status/{videoId}` to poll completion/failure.

Save with live progress updates (SSE):

```bash
curl -N -X POST http://localhost:8000/api/download \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "videoId": "b-gjLgT4SUQ",
    "download_path": "/Users/anikeshthakur/Downloads/yt",
    "progress_updates": true,
    "format": { "quality": "720p", "ext": "mp4" }
  }'
```

Check download status:

```bash
curl http://localhost:8000/api/download/status/b-gjLgT4SUQ
```

Stream live status updates (SSE):

```bash
curl -N -H "Accept: text/event-stream" \
  http://localhost:8000/api/download/status/stream/b-gjLgT4SUQ
```

Check if yt-dlp is currently running download/post-process:

```bash
curl http://localhost:8000/api/download/running
```

SSE events emitted:

- `event: progress` while downloading/post-processing
- `event: complete` when done
- `event: error` on failure

## Logs

```bash
docker logs -f yt-downloader
```
