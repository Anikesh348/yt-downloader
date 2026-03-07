from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore, Thread
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, model_validator

TARGET_HEIGHTS = (480, 720, 1080, 1440, 2160)
SUPPORTED_QUALITIES = tuple(f"{height}p" for height in TARGET_HEIGHTS)


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum:
        return minimum
    return value


def env_float(name: str, default: float, minimum: float = 0.1) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < minimum:
        return minimum
    return value


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("yt_dlp_api")

MAX_ACTIVE_JOBS = env_int("MAX_ACTIVE_JOBS", 2, minimum=1)
JOB_ACQUIRE_TIMEOUT_S = env_float("JOB_ACQUIRE_TIMEOUT_S", 2.0, minimum=0.1)
YTDLP_METADATA_TIMEOUT_S = env_int("YTDLP_METADATA_TIMEOUT_S", 60, minimum=5)
YTDLP_TITLE_TIMEOUT_S = env_int("YTDLP_TITLE_TIMEOUT_S", 30, minimum=5)
YTDLP_FILE_DOWNLOAD_TIMEOUT_S = env_int("YTDLP_FILE_DOWNLOAD_TIMEOUT_S", 7200, minimum=60)
YTDLP_CONCURRENT_FRAGMENTS = env_int("YTDLP_CONCURRENT_FRAGMENTS", 1, minimum=1)
FFMPEG_THREADS = env_int("FFMPEG_THREADS", 1, minimum=1)
STREAM_CHUNK_SIZE = env_int("STREAM_CHUNK_SIZE_KB", 256, minimum=32) * 1024
JOB_SEMAPHORE = BoundedSemaphore(MAX_ACTIVE_JOBS)


app = FastAPI(
    title="yt-dlp wrapper API",
    version="1.0.0",
    description="HTTP wrapper around the yt-dlp CLI for format discovery and download streaming.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@contextmanager
def yt_dlp_slot(operation: str, url: str | None = None) -> Iterator[None]:
    acquired = JOB_SEMAPHORE.acquire(timeout=JOB_ACQUIRE_TIMEOUT_S)
    if not acquired:
        logger.warning("busy op=%s url=%s", operation, url or "-")
        raise HTTPException(status_code=429, detail="Server is busy. Please retry shortly.")
    started = time.monotonic()
    logger.info("job_start op=%s url=%s", operation, url or "-")
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        JOB_SEMAPHORE.release()
        logger.info("job_end op=%s elapsed_s=%.3f", operation, elapsed)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next: Any) -> Response:
    started = time.monotonic()
    client_host = request.client.host if request.client else "-"
    logger.info("request_start method=%s path=%s client=%s", request.method, request.url.path, client_host)
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request_failed method=%s path=%s", request.method, request.url.path)
        raise
    elapsed = time.monotonic() - started
    logger.info(
        "request_end method=%s path=%s status=%s elapsed_s=%.3f",
        request.method,
        request.url.path,
        response.status_code,
        elapsed,
    )
    return response


@app.on_event("startup")
def log_startup_config() -> None:
    logger.info(
        "startup_config max_jobs=%s job_timeout_s=%s metadata_timeout_s=%s title_timeout_s=%s "
        "download_timeout_s=%s fragments=%s ffmpeg_threads=%s chunk_kb=%s",
        MAX_ACTIVE_JOBS,
        JOB_ACQUIRE_TIMEOUT_S,
        YTDLP_METADATA_TIMEOUT_S,
        YTDLP_TITLE_TIMEOUT_S,
        YTDLP_FILE_DOWNLOAD_TIMEOUT_S,
        YTDLP_CONCURRENT_FRAGMENTS,
        FFMPEG_THREADS,
        STREAM_CHUNK_SIZE // 1024,
    )


def yt_dlp_binary() -> str:
    configured = os.getenv("YT_DLP_BINARY", "").strip()
    if configured:
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            logger.debug("yt_dlp_binary source=env path=%s", configured)
            return configured
        if os.path.isdir(configured):
            detail = f"YT_DLP_BINARY points to a directory, not a file: {configured}"
        elif not os.path.exists(configured):
            detail = f"YT_DLP_BINARY path does not exist inside the container: {configured}"
        else:
            detail = f"YT_DLP_BINARY points to a non-executable path: {configured}"
        raise HTTPException(
            status_code=500,
            detail=detail,
        )

    binary = shutil.which("yt-dlp")
    if binary:
        logger.debug("yt_dlp_binary source=path path=%s", binary)
        return binary

    raise HTTPException(status_code=500, detail="yt-dlp is not installed or not on PATH.")


def run_yt_dlp(cmd: list[str], timeout_s: int, operation: str) -> subprocess.CompletedProcess[str]:
    logger.info("yt_dlp_exec op=%s timeout_s=%s", operation, timeout_s)
    logger.debug("yt_dlp_cmd op=%s cmd=%s", operation, cmd)
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("yt_dlp_timeout op=%s timeout_s=%s", operation, timeout_s)
        raise HTTPException(status_code=504, detail=f"{operation} timed out.") from exc
    logger.info("yt_dlp_exit op=%s code=%s", operation, completed.returncode)
    return completed


def build_download_cmd(url: str, selector: str, output: str, extension: str, allow_merge: bool = True) -> list[str]:
    cmd = [
        yt_dlp_binary(),
        "--no-playlist",
        "--no-progress",
        "--newline",
        "--concurrent-fragments",
        str(YTDLP_CONCURRENT_FRAGMENTS),
        "-f",
        selector,
    ]
    if allow_merge and extension != "bin":
        cmd.extend(
            [
                "--merge-output-format",
                extension,
                "--postprocessor-args",
                f"ffmpeg:-threads {FFMPEG_THREADS}",
            ]
        )
    cmd.extend(["-o", output, url])
    return cmd


def read_text_stream(stream: Any, sink: list[str]) -> None:
    if stream is None:
        return
    try:
        for line in iter(stream.readline, b""):
            if not line:
                break
            sink.append(line.decode("utf-8", errors="replace"))
    finally:
        stream.close()


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "download"


def quoted_filename(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', r"\"")
    return f'attachment; filename="{escaped}"'


def fetch_video_title(url: str) -> str | None:
    cmd = [
        yt_dlp_binary(),
        "--no-playlist",
        "--skip-download",
        "--print",
        "title",
        url,
    ]
    try:
        completed = run_yt_dlp(cmd, YTDLP_TITLE_TIMEOUT_S, "title_lookup")
    except HTTPException:
        logger.warning("title_lookup_timeout url=%s", url)
        return None
    if completed.returncode != 0:
        logger.warning("title_lookup_failed url=%s detail=%s", url, (completed.stderr or "").strip()[:200])
        return None
    title = (completed.stdout or "").strip().splitlines()
    if not title:
        return None
    normalized = title[0].strip()
    logger.info("title_lookup_success url=%s title=%s", url, normalized[:120])
    return normalized or None


def has_audio_track(item: dict[str, Any]) -> bool:
    return item.get("acodec") not in (None, "none")


def has_video_track(item: dict[str, Any]) -> bool:
    return item.get("vcodec") not in (None, "none")


def parse_height(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        int_value = int(value)
        return int_value if int_value > 0 else None
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            int_value = int(cleaned)
            return int_value if int_value > 0 else None
    return None


def build_resolution_selector(height: int) -> str:
    return (
        f"bestvideo*[height={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo*[height={height}]+bestaudio/"
        f"best[height={height}][ext=mp4]/"
        f"best[height={height}]"
    )


def quality_to_selector(quality: str) -> str:
    if quality not in SUPPORTED_QUALITIES:
        raise ValueError(f"Unsupported quality '{quality}'. Supported: {', '.join(SUPPORTED_QUALITIES)}")
    height = int(quality[:-1])
    return build_resolution_selector(height)


def combined_download_options(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_formats = [item for item in formats if isinstance(item, dict)]
    logger.info("formats_normalized count=%s", len(normalized_formats))
    audio_available = any(has_audio_track(item) for item in normalized_formats)
    if not audio_available:
        logger.info("formats_no_audio")
        return []

    progressive_heights = {
        parsed_height
        for item in normalized_formats
        if has_audio_track(item)
        and has_video_track(item)
        and (parsed_height := parse_height(item.get("height"))) is not None
    }
    video_heights = {
        parsed_height
        for item in normalized_formats
        if has_video_track(item)
        and (parsed_height := parse_height(item.get("height"))) is not None
    }

    options: list[dict[str, Any]] = []
    for height in TARGET_HEIGHTS:
        if height not in video_heights and height not in progressive_heights:
            continue
        options.append(
            {
                "label": f"{height}p",
                "quality": f"{height}p",
                "height": height,
                "ext": "mp4",
                "audio_combined": True,
                "request_format": {
                    "quality": f"{height}p",
                    "ext": "mp4",
                },
            }
        )
    logger.info("formats_filtered count=%s", len(options))
    return options


class UrlPayload(BaseModel):
    url: str = Field(..., min_length=1)


class RequestedFormat(BaseModel):
    quality: str
    ext: str | None = None

    @model_validator(mode="after")
    def validate_quality(self) -> "RequestedFormat":
        normalized = self.quality.strip().lower()
        if normalized not in SUPPORTED_QUALITIES:
            raise ValueError(f"Unsupported quality. Use one of: {', '.join(SUPPORTED_QUALITIES)}")
        self.quality = normalized
        return self

    @property
    def selector(self) -> str:
        return quality_to_selector(self.quality)


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=1)
    format: RequestedFormat
    filename: str | None = None
    download_path: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    yt_dlp_binary()
    logger.info("health_ok")
    return {"status": "ok"}


@app.post("/api/formats")
def list_formats(payload: UrlPayload) -> JSONResponse:
    logger.info("formats_request url=%s", payload.url)
    cmd = [
        yt_dlp_binary(),
        "--dump-single-json",
        "--no-playlist",
        payload.url,
    ]
    with yt_dlp_slot("formats", payload.url):
        completed = run_yt_dlp(cmd, YTDLP_METADATA_TIMEOUT_S, "formats")

    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "yt-dlp failed."
        logger.warning("formats_failed url=%s detail=%s", payload.url, message[:250])
        raise HTTPException(status_code=400, detail=message)

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="yt-dlp returned invalid JSON.") from exc

    raw_formats = data.get("formats")
    if not isinstance(raw_formats, list):
        raw_formats = []
    formats = combined_download_options(raw_formats)
    response = {
        "id": data.get("id"),
        "title": data.get("title"),
        "duration": data.get("duration"),
        "webpage_url": data.get("webpage_url"),
        "thumbnail": data.get("thumbnail"),
        "uploader": data.get("uploader"),
        "extractor": data.get("extractor"),
        "formats": formats,
    }
    logger.info("formats_success url=%s returned=%s", payload.url, len(formats))
    return JSONResponse(response)


class DownloadStream:
    def __init__(self, cmd: list[str], url: str) -> None:
        self.stderr_buffer: list[str] = []
        self.url = url
        self.slot_acquired = JOB_SEMAPHORE.acquire(timeout=JOB_ACQUIRE_TIMEOUT_S)
        if not self.slot_acquired:
            logger.warning("busy op=download_stream url=%s", url)
            raise HTTPException(status_code=429, detail="Server is busy. Please retry shortly.")
        self.started = time.monotonic()
        self.bytes_streamed = 0
        self.closed = False
        logger.info("job_start op=download_stream url=%s", url)
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                text=False,
            )
        except Exception:
            JOB_SEMAPHORE.release()
            self.slot_acquired = False
            logger.exception("stream_spawn_failed url=%s", url)
            raise
        logger.info("stream_spawned pid=%s url=%s", self.process.pid, url)
        self.stderr_thread = Thread(
            target=read_text_stream,
            args=(self.process.stderr, self.stderr_buffer),
            daemon=True,
        )
        self.stderr_thread.start()

    def prime(self) -> bytes:
        if self.process.stdout is None:
            self.cleanup()
            raise HTTPException(status_code=500, detail="yt-dlp did not expose stdout.")

        first_chunk = self.process.stdout.read(STREAM_CHUNK_SIZE)
        if first_chunk:
            self.bytes_streamed += len(first_chunk)
            return first_chunk

        if self.process.poll() is None:
            self.process.wait()
        return_code = self.process.returncode
        self.stderr_thread.join(timeout=1)
        detail = "".join(self.stderr_buffer).strip() or "yt-dlp download failed."
        logger.warning("stream_prime_failed url=%s code=%s detail=%s", self.url, return_code, detail[:250])
        status_code = 400 if return_code != 0 else 204
        self.cleanup()
        raise HTTPException(status_code=status_code, detail=detail)

    def iterate(self, first_chunk: bytes) -> Iterator[bytes]:
        try:
            yield first_chunk

            if self.process.stdout is None:
                return

            while True:
                chunk = self.process.stdout.read(STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                self.bytes_streamed += len(chunk)
                yield chunk
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.poll() is None:
            self.process.wait()
        self.stderr_thread.join(timeout=1)
        elapsed = time.monotonic() - self.started
        logger.info(
            "stream_end url=%s code=%s bytes=%s elapsed_s=%.3f",
            self.url,
            self.process.returncode,
            self.bytes_streamed,
            elapsed,
        )
        if self.slot_acquired:
            JOB_SEMAPHORE.release()
            logger.info("job_end op=download_stream elapsed_s=%.3f", elapsed)


def resolve_output_path(download_path: str, filename: str) -> Path:
    target = Path(download_path).expanduser()
    raw_target = download_path

    if target.exists() and target.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        logger.info("resolve_output_path mode=directory target=%s", target)
        return target / filename

    if raw_target.endswith(os.sep):
        target.mkdir(parents=True, exist_ok=True)
        logger.info("resolve_output_path mode=directory_hint target=%s", target)
        return target / filename

    if target.suffix:
        logger.info("resolve_output_path mode=file target=%s", target)
        return target

    logger.info("resolve_output_path mode=directory_default target=%s", target)
    return target / filename


def map_host_path_for_container(output_path: Path) -> Path:
    source_prefix = os.getenv("HOST_PATH_SOURCE_PREFIX", "").strip()
    mount_prefix = os.getenv("HOST_PATH_MOUNT_PREFIX", "").strip()
    if not source_prefix or not mount_prefix:
        logger.info("path_map_skip reason=env_not_set output=%s", output_path)
        return output_path
    if not output_path.is_absolute():
        logger.info("path_map_skip reason=relative output=%s", output_path)
        return output_path

    source_root = Path(source_prefix)
    mount_root = Path(mount_prefix)
    try:
        relative_path = output_path.relative_to(source_root)
    except ValueError:
        logger.info("path_map_skip reason=outside_prefix output=%s source_prefix=%s", output_path, source_root)
        return output_path
    mapped = mount_root / relative_path
    logger.info("path_map_success source=%s mapped=%s", output_path, mapped)
    return mapped


@app.post("/api/download", response_model=None)
def download_video(payload: DownloadRequest) -> Response:
    logger.info(
        "download_request url=%s mode=%s",
        payload.url,
        "save" if payload.download_path else "stream",
    )
    extension = payload.format.ext or "mp4"
    stem = payload.filename
    if not stem:
        with yt_dlp_slot("title_lookup", payload.url):
            stem = fetch_video_title(payload.url) or "download"
    filename = f"{sanitize_filename(stem)}.{sanitize_filename(extension)}"
    logger.info("download_filename resolved=%s", filename)
    logger.info("download_quality quality=%s", payload.format.quality)

    if payload.download_path:
        user_output_path = resolve_output_path(payload.download_path, filename)
        container_output_path = map_host_path_for_container(user_output_path)
        container_output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = build_download_cmd(
            url=payload.url,
            selector=payload.format.selector,
            output=str(container_output_path),
            extension=extension,
            allow_merge=True,
        )

        with yt_dlp_slot("download_file", payload.url):
            completed = run_yt_dlp(cmd, YTDLP_FILE_DOWNLOAD_TIMEOUT_S, "download_file")
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "yt-dlp download failed."
            logger.warning("download_file_failed url=%s detail=%s", payload.url, message[:250])
            raise HTTPException(status_code=400, detail=message)

        logger.info(
            "download_file_success url=%s user_output=%s container_output=%s",
            payload.url,
            user_output_path,
            container_output_path,
        )
        return JSONResponse(
            {
                "status": "downloaded",
                "download_path": str(user_output_path),
                "container_download_path": str(container_output_path),
                "filename": user_output_path.name,
            }
        )

    cmd = build_download_cmd(
        url=payload.url,
        selector=payload.format.selector,
        output="-",
        extension=extension,
        allow_merge=False,
    )

    stream = DownloadStream(cmd, payload.url)
    first_chunk = stream.prime()

    headers = {
        "Content-Disposition": quoted_filename(filename),
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }
    logger.info("download_stream_start url=%s filename=%s", payload.url, filename)

    return StreamingResponse(
        stream.iterate(first_chunk),
        media_type="application/octet-stream",
        headers=headers,
    )
