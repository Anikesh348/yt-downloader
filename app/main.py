from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore, Lock, Thread
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, model_validator

TARGET_HEIGHTS = (360, 480, 720, 1080, 1440, 2160)
SUPPORTED_QUALITIES = tuple(f"{height}p" for height in TARGET_HEIGHTS)
ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
DOWNLOAD_PROGRESS_WITH_ETA_RE = re.compile(
    r"^\[download\]\s+(?P<percent>\d+(?:\.\d+)?)%\s+of\s+(?P<total>.+?)\s+at\s+(?P<speed>.+?)\s+ETA\s+(?P<eta>\S+)(?:\s+.*)?$"
)
DOWNLOAD_PROGRESS_COMPLETE_RE = re.compile(
    r"^\[download\]\s+(?P<percent>\d+(?:\.\d+)?)%\s+of\s+(?P<total>.+?)\s+in\s+(?P<elapsed>\S+)(?:\s+.*)?$"
)
DOWNLOAD_TEMPLATE_PROGRESS_RE = re.compile(
    r"^\[progress\]\s*(?P<percent>[^|]+)\|(?P<total>[^|]+)\|(?P<speed>[^|]+)\|(?P<eta>[^|]+)$"
)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")


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
YTDLP_FILE_DOWNLOAD_TIMEOUT_S = env_int("YTDLP_FILE_DOWNLOAD_TIMEOUT_S", 7200, minimum=60)
YTDLP_CONCURRENT_FRAGMENTS = env_int("YTDLP_CONCURRENT_FRAGMENTS", 1, minimum=1)
FFMPEG_THREADS = env_int("FFMPEG_THREADS", 1, minimum=1)
HEIGHT_NEAR_TOLERANCE = env_int("HEIGHT_NEAR_TOLERANCE", 24, minimum=0)
YTDLP_JS_RUNTIMES = os.getenv("YTDLP_JS_RUNTIMES", "node").strip()
YTDLP_REMOTE_COMPONENTS = os.getenv("YTDLP_REMOTE_COMPONENTS", "ejs:github").strip()
YTDLP_EXTRACTOR_ARGS = os.getenv(
    "YTDLP_EXTRACTOR_ARGS",
    "youtube:player_client=default,-ios",
).strip()
_download_extractor_args_env = os.getenv("YTDLP_DOWNLOAD_EXTRACTOR_ARGS")
_download_extractor_args_clean = (_download_extractor_args_env or "").strip()
if _download_extractor_args_clean.lower() == "none":
    YTDLP_DOWNLOAD_EXTRACTOR_ARGS = ""
else:
    YTDLP_DOWNLOAD_EXTRACTOR_ARGS = _download_extractor_args_clean or YTDLP_EXTRACTOR_ARGS
YTDLP_PO_TOKEN_ARGS = os.getenv("YTDLP_PO_TOKEN_ARGS", "").strip()
STREAM_CHUNK_SIZE = env_int("STREAM_CHUNK_SIZE_KB", 256, minimum=32) * 1024
JOB_SEMAPHORE = BoundedSemaphore(MAX_ACTIVE_JOBS)
DOWNLOAD_STATUS_LOCK = Lock()
DOWNLOAD_STATUS_BY_VIDEO_ID: dict[str, dict[str, Any]] = {}
ACTIVE_DOWNLOAD_LOCK = Lock()
ACTIVE_DOWNLOAD_COUNT = 0
YOUTUBE_WATCH_BASE_URL = os.getenv("YOUTUBE_WATCH_BASE_URL", "https://www.youtube.com/watch?v=")
STATUS_STREAM_POLL_INTERVAL_S = env_float("STATUS_STREAM_POLL_INTERVAL_S", 0.5, minimum=0.1)
STATUS_STREAM_KEEPALIVE_S = env_float("STATUS_STREAM_KEEPALIVE_S", 15.0, minimum=1.0)


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
        "startup_config max_jobs=%s job_timeout_s=%s metadata_timeout_s=%s "
        "download_timeout_s=%s fragments=%s ffmpeg_threads=%s near_tolerance=%s chunk_kb=%s js_runtimes=%s "
        "remote_components=%s extractor_args=%s download_extractor_args=%s po_token_args=%s",
        MAX_ACTIVE_JOBS,
        JOB_ACQUIRE_TIMEOUT_S,
        YTDLP_METADATA_TIMEOUT_S,
        YTDLP_FILE_DOWNLOAD_TIMEOUT_S,
        YTDLP_CONCURRENT_FRAGMENTS,
        FFMPEG_THREADS,
        HEIGHT_NEAR_TOLERANCE,
        STREAM_CHUNK_SIZE // 1024,
        YTDLP_JS_RUNTIMES or "-",
        YTDLP_REMOTE_COMPONENTS or "-",
        YTDLP_EXTRACTOR_ARGS or "-",
        YTDLP_DOWNLOAD_EXTRACTOR_ARGS or "-",
        "configured" if YTDLP_PO_TOKEN_ARGS else "-",
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


def yt_dlp_runtime_args(operation: str = "default") -> list[str]:
    args: list[str] = []
    if YTDLP_JS_RUNTIMES:
        args.extend(["--js-runtimes", YTDLP_JS_RUNTIMES])
    if YTDLP_REMOTE_COMPONENTS:
        args.extend(["--remote-components", YTDLP_REMOTE_COMPONENTS])
    extractor_args = YTDLP_DOWNLOAD_EXTRACTOR_ARGS if operation.startswith("download") else YTDLP_EXTRACTOR_ARGS
    if (
        operation.startswith("download")
        and extractor_args
        and "formats=missing_pot" in extractor_args.lower()
        and not YTDLP_PO_TOKEN_ARGS
    ):
        logger.info(
            "download_extractor_args_override reason=missing_pot_without_po_token override=%s",
            "youtube:player_client=default,-ios",
        )
        extractor_args = "youtube:player_client=default,-ios"
    if extractor_args:
        args.extend(["--extractor-args", extractor_args])
    if YTDLP_PO_TOKEN_ARGS:
        args.extend(["--extractor-args", YTDLP_PO_TOKEN_ARGS])
    return args


def build_download_cmd(
    url: str,
    selector: str,
    output: str,
    extension: str,
    allow_merge: bool = True,
    show_progress: bool = False,
) -> list[str]:
    cmd = [
        yt_dlp_binary(),
        *yt_dlp_runtime_args("download"),
        "--no-playlist",
        "--newline",
        "--concurrent-fragments",
        str(YTDLP_CONCURRENT_FRAGMENTS),
        "--check-formats",
        "-f",
        selector,
    ]
    if show_progress:
        cmd.extend(
            [
                "--progress",
                "--progress-template",
                "download:[progress] %(progress._percent_str)s|%(progress._total_bytes_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
            ]
        )
    else:
        cmd.append("--no-progress")
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


def normalize_progress_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.upper() in {"NA", "N/A", "UNKNOWN"}:
        return None
    return cleaned


def parse_download_status_line(line: str) -> dict[str, Any] | None:
    stripped = ANSI_ESCAPE_RE.sub("", line).replace("\r", "").strip()
    if not stripped:
        return None
    template_match = DOWNLOAD_TEMPLATE_PROGRESS_RE.match(stripped)
    if template_match:
        percent_text = normalize_progress_value(template_match.group("percent")) or "0"
        percent_match = re.search(r"\d+(?:\.\d+)?", percent_text)
        progress_percent = float(percent_match.group(0)) if percent_match else 0.0
        payload: dict[str, Any] = {
            "status": "downloading",
            "phase": "downloading",
            "progress_percent": progress_percent,
        }
        total_size = normalize_progress_value(template_match.group("total"))
        speed = normalize_progress_value(template_match.group("speed"))
        eta = normalize_progress_value(template_match.group("eta"))
        if total_size is not None:
            payload["total_size"] = total_size
        if speed is not None:
            payload["speed"] = speed
        if eta is not None:
            payload["eta"] = eta
        return payload
    if stripped.startswith("[download] Destination:"):
        return {"status": "downloading", "phase": "preparing"}
    if stripped.startswith("[Merger]"):
        return {"status": "postprocessing", "phase": "postprocessing"}

    progress_match = DOWNLOAD_PROGRESS_WITH_ETA_RE.match(stripped)
    if progress_match:
        payload: dict[str, Any] = {
            "status": "downloading",
            "phase": "downloading",
            "progress_percent": float(progress_match.group("percent")),
        }
        total_size = normalize_progress_value(progress_match.group("total"))
        speed = normalize_progress_value(progress_match.group("speed"))
        eta = normalize_progress_value(progress_match.group("eta"))
        if total_size is not None:
            payload["total_size"] = total_size
        if speed is not None:
            payload["speed"] = speed
        if eta is not None:
            payload["eta"] = eta
        return payload

    complete_match = DOWNLOAD_PROGRESS_COMPLETE_RE.match(stripped)
    if complete_match:
        payload = {
            "status": "downloading",
            "phase": "downloading",
            "progress_percent": float(complete_match.group("percent")),
        }
        total_size = normalize_progress_value(complete_match.group("total"))
        elapsed = normalize_progress_value(complete_match.group("elapsed"))
        if total_size is not None:
            payload["total_size"] = total_size
        if elapsed is not None:
            payload["elapsed"] = elapsed
        return payload
    return None


def encode_sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def sanitize_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    possessive_fixed = re.sub(r"(?<=\w)['’](?=\w)", "", ascii_only)
    # Treat separator + single "s" + separator as a likely apostrophe-s artifact.
    possessive_fixed = re.sub(r"(?<=\w)(?:\s|_|-)s(?=(?:\s|_|-))", "s", possessive_fixed)
    collapsed = re.sub(r"[\s_-]+", " ", possessive_fixed)
    cleaned = re.sub(r"[^A-Za-z0-9. ]+", " ", collapsed)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .")
    return cleaned or "download"


def sanitize_extension(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", value).lower().strip()
    return cleaned or "mp4"


def normalize_video_id(video_id: str) -> str:
    normalized = video_id.strip()
    if not VIDEO_ID_RE.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="Invalid videoId. Expected a YouTube video id.")
    return normalized


def video_id_to_url(video_id: str) -> str:
    return f"{YOUTUBE_WATCH_BASE_URL}{normalize_video_id(video_id)}"


def update_download_status(video_id: str, **updates: Any) -> None:
    with DOWNLOAD_STATUS_LOCK:
        current = DOWNLOAD_STATUS_BY_VIDEO_ID.get(video_id, {"videoId": video_id})
        current.update(updates)
        current["videoId"] = video_id
        current["updated_at"] = time.time_ns()
        DOWNLOAD_STATUS_BY_VIDEO_ID[video_id] = current


def read_download_status(video_id: str) -> dict[str, Any] | None:
    with DOWNLOAD_STATUS_LOCK:
        current = DOWNLOAD_STATUS_BY_VIDEO_ID.get(video_id)
        if current is None:
            return None
        return dict(current)


def start_active_download() -> None:
    global ACTIVE_DOWNLOAD_COUNT
    with ACTIVE_DOWNLOAD_LOCK:
        ACTIVE_DOWNLOAD_COUNT += 1


def finish_active_download() -> None:
    global ACTIVE_DOWNLOAD_COUNT
    with ACTIVE_DOWNLOAD_LOCK:
        ACTIVE_DOWNLOAD_COUNT = max(0, ACTIVE_DOWNLOAD_COUNT - 1)


def get_active_download_count() -> int:
    with ACTIVE_DOWNLOAD_LOCK:
        return ACTIVE_DOWNLOAD_COUNT


def update_status_from_yt_dlp_line(video_id: str, url: str, line: str) -> None:
    parsed = parse_download_status_line(line)
    if parsed:
        update_download_status(video_id, **parsed, url=url)
        return
    cleaned = ANSI_ESCAPE_RE.sub("", line).replace("\r", "").strip()
    if cleaned.startswith("[download]"):
        update_download_status(
            video_id,
            status="downloading",
            phase="downloading",
            message=cleaned,
            url=url,
        )


def iter_status_sse(video_id: str) -> Iterator[str]:
    last_updated_at: int | None = None
    last_emit_time = 0.0
    while True:
        current = read_download_status(video_id)
        if current is None:
            yield encode_sse_event(
                "error",
                {
                    "videoId": video_id,
                    "status": "not_started",
                    "phase": "idle",
                    "detail": "No download status available for this videoId.",
                },
            )
            return

        updated_at = current.get("updated_at")
        now = time.monotonic()
        if updated_at != last_updated_at:
            status = str(current.get("status", "downloading"))
            event = "progress"
            if status == "downloaded":
                event = "complete"
            elif status == "failed":
                event = "error"
            yield encode_sse_event(event, current)
            last_updated_at = updated_at if isinstance(updated_at, int) else last_updated_at
            last_emit_time = now
            if status in {"downloaded", "failed"}:
                return
        elif now - last_emit_time >= STATUS_STREAM_KEEPALIVE_S:
            # SSE comment-style keepalive to keep proxies/connections warm.
            yield ": keepalive\n\n"
            last_emit_time = now

        time.sleep(STATUS_STREAM_POLL_INTERVAL_S)


def start_background_file_download(
    video_id: str,
    url: str,
    cmd: list[str],
    user_output_path: Path,
    container_output_path: Path,
    requested_quality: str,
    selected_quality: str,
    fallback_detail: str | None,
) -> None:
    def worker() -> None:
        start_active_download()
        update_download_status(
            video_id,
            status="downloading",
            phase="downloading",
            progress_percent=0.0,
            filename=user_output_path.name,
            download_path=str(user_output_path),
            container_download_path=str(container_output_path),
            requested_quality=requested_quality,
            selected_quality=selected_quality,
            quality_fallback=fallback_detail,
            url=url,
        )
        try:
            with yt_dlp_slot("download_file", url):
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    text=False,
                )
                stderr_buffer: list[str] = []
                stdout_tail: list[str] = []

                def consume_stdout(stream: Any) -> None:
                    if stream is None:
                        return
                    try:
                        for raw in iter(stream.readline, b""):
                            if not raw:
                                break
                            line = raw.decode("utf-8", errors="replace")
                            update_status_from_yt_dlp_line(video_id, url, line)
                            cleaned = ANSI_ESCAPE_RE.sub("", line).replace("\r", "").strip()
                            if cleaned:
                                stdout_tail.append(cleaned)
                                if len(stdout_tail) > 25:
                                    stdout_tail.pop(0)
                    finally:
                        stream.close()

                stdout_thread = Thread(target=consume_stdout, args=(process.stdout,), daemon=True)
                stderr_thread = Thread(target=read_text_stream, args=(process.stderr, stderr_buffer), daemon=True)
                stdout_thread.start()
                stderr_thread.start()

                try:
                    process.wait(timeout=YTDLP_FILE_DOWNLOAD_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    update_download_status(
                        video_id,
                        status="failed",
                        phase="failed",
                        detail="download_file timed out.",
                        url=url,
                    )
                    logger.warning("download_file_timeout url=%s timeout_s=%s", url, YTDLP_FILE_DOWNLOAD_TIMEOUT_S)
                    return
                finally:
                    stdout_thread.join(timeout=1)
                    stderr_thread.join(timeout=1)

            if process.returncode != 0:
                message = "".join(stderr_buffer).strip() or (stdout_tail[-1] if stdout_tail else "yt-dlp download failed.")
                logger.warning("download_file_failed url=%s detail=%s", url, message[:250])
                update_download_status(
                    video_id,
                    status="failed",
                    phase="failed",
                    detail=message,
                    url=url,
                )
                return

            quality_ok, resolved_quality, actual_height, detail = validate_downloaded_quality(
                container_output_path,
                requested_quality,
            )
            if not quality_ok:
                logger.warning(
                    "download_file_quality_mismatch url=%s requested=%s selected=%s resolved=%s actual_height=%s detail=%s",
                    url,
                    requested_quality,
                    selected_quality,
                    resolved_quality or "-",
                    actual_height,
                    detail[:250] if isinstance(detail, str) else "-",
                )
                update_download_status(
                    video_id,
                    status="failed",
                    phase="failed",
                    detail=detail or "Downloaded quality verification failed.",
                    requested_quality=requested_quality,
                    selected_quality=selected_quality,
                    quality_fallback=fallback_detail,
                    resolved_height=actual_height,
                    filename=user_output_path.name,
                    download_path=str(user_output_path),
                    container_download_path=str(container_output_path),
                    url=url,
                )
                return

            final_quality = resolved_quality or selected_quality
            final_fallback_detail = fallback_detail_for_requested_quality(requested_quality, final_quality)

            logger.info(
                "download_file_success url=%s user_output=%s container_output=%s requested=%s selected=%s actual_height=%s",
                url,
                user_output_path,
                container_output_path,
                requested_quality,
                final_quality,
                actual_height,
            )
            update_download_status(
                video_id,
                status="downloaded",
                phase="completed",
                progress_percent=100.0,
                eta="00:00",
                detail=None,
                filename=user_output_path.name,
                download_path=str(user_output_path),
                container_download_path=str(container_output_path),
                requested_quality=requested_quality,
                selected_quality=final_quality,
                quality_fallback=final_fallback_detail,
                resolved_height=actual_height,
                url=url,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else "yt-dlp download failed."
            logger.warning("download_file_exception url=%s detail=%s", url, str(detail)[:250])
            update_download_status(
                video_id,
                status="failed",
                phase="failed",
                detail=detail,
                url=url,
            )
        except Exception:
            logger.exception("download_file_unhandled_exception url=%s", url)
            update_download_status(
                video_id,
                status="failed",
                phase="failed",
                detail="Unexpected internal error while downloading.",
                url=url,
            )
        finally:
            finish_active_download()

    thread = Thread(target=worker, daemon=True, name=f"download-{video_id[:12]}")
    thread.start()


def quoted_filename(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', r"\"")
    return f'attachment; filename="{escaped}"'


def has_audio_track(item: dict[str, Any]) -> bool:
    return item.get("acodec") not in (None, "none")


def has_video_track(item: dict[str, Any]) -> bool:
    return item.get("vcodec") not in (None, "none")


def parse_dimension(value: Any) -> int | None:
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


def is_missing_pot_format(item: dict[str, Any]) -> bool:
    for key in ("format_note", "format"):
        value = item.get(key)
        if isinstance(value, str) and "MISSING POT" in value.upper():
            return True
    return False


def has_resolved_media_url(item: dict[str, Any]) -> bool:
    if not (has_video_track(item) or has_audio_track(item)):
        return True
    value = item.get("url")
    return isinstance(value, str) and bool(value.strip())


def filter_downloadable_media_formats(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    dropped_missing_pot = 0
    dropped_no_url = 0
    for item in formats:
        if not isinstance(item, dict):
            continue
        if is_missing_pot_format(item):
            dropped_missing_pot += 1
            continue
        if not has_resolved_media_url(item):
            dropped_no_url += 1
            continue
        filtered.append(item)
    logger.info(
        "formats_downloadable_filter kept=%s dropped_missing_pot=%s dropped_no_url=%s",
        len(filtered),
        dropped_missing_pot,
        dropped_no_url,
    )
    return filtered


def long_side_target(height: int) -> int:
    return max(1, round(height * 16 / 9))


def long_side_tolerance() -> int:
    return max(1, round(HEIGHT_NEAR_TOLERANCE * 16 / 9))


def is_dimension_match(target: int, available: int, tolerance: int) -> bool:
    return abs(available - target) <= tolerance


def format_matches_quality(target_height: int, width: int | None, height: int | None) -> bool:
    target_long_side = long_side_target(target_height)
    target_long_tolerance = long_side_tolerance()
    for dimension in (width, height):
        if dimension is None:
            continue
        if is_dimension_match(target_height, dimension, HEIGHT_NEAR_TOLERANCE):
            return True
        if is_dimension_match(target_long_side, dimension, target_long_tolerance):
            return True
    return False


def dimension_bounds(target: int, tolerance: int) -> tuple[int, int]:
    return max(1, target - tolerance), target + tolerance


def add_selector_part(parts: list[str], part: str) -> None:
    if part not in parts:
        parts.append(part)


def build_resolution_selector(height: int) -> str:
    target_long_side = long_side_target(height)
    target_long_tolerance = long_side_tolerance()
    direct_min, direct_max = dimension_bounds(height, HEIGHT_NEAR_TOLERANCE)
    long_min, long_max = dimension_bounds(target_long_side, target_long_tolerance)
    dimension_ranges = (
        ("height", direct_min, direct_max),
        ("width", direct_min, direct_max),
        ("height", long_min, long_max),
        ("width", long_min, long_max),
    )

    parts: list[str] = []
    for field, min_value, max_value in dimension_ranges:
        filter_expr = f"[{field}>={min_value}][{field}<={max_value}]"
        add_selector_part(parts, f"best{filter_expr}[ext=mp4]")
    for field, min_value, max_value in dimension_ranges:
        filter_expr = f"[{field}>={min_value}][{field}<={max_value}]"
        add_selector_part(parts, f"best{filter_expr}")
    for field, min_value, max_value in dimension_ranges:
        filter_expr = f"[{field}>={min_value}][{field}<={max_value}]"
        add_selector_part(parts, f"bestvideo*{filter_expr}[ext=mp4]+bestaudio[ext=m4a]")
    for field, min_value, max_value in dimension_ranges:
        filter_expr = f"[{field}>={min_value}][{field}<={max_value}]"
        add_selector_part(parts, f"bestvideo*{filter_expr}+bestaudio")
    return "/".join(parts)


def quality_to_selector(quality: str) -> str:
    if quality not in SUPPORTED_QUALITIES:
        raise ValueError(f"Unsupported quality '{quality}'. Supported: {', '.join(SUPPORTED_QUALITIES)}")
    height = int(quality[:-1])
    return build_resolution_selector(height)


def supported_fallback_qualities(requested_quality: str) -> list[str]:
    requested_height = int(requested_quality[:-1])
    heights = [height for height in TARGET_HEIGHTS if height <= requested_height]
    heights.sort(reverse=True)
    return [f"{height}p" for height in heights]


def build_quality_fallback_selector(requested_quality: str) -> str:
    selectors = [quality_to_selector(quality) for quality in supported_fallback_qualities(requested_quality)]
    return "/".join(selectors)


def quality_from_dimensions(width: int | None, height: int | None) -> str | None:
    matches = [target for target in TARGET_HEIGHTS if format_matches_quality(target, width, height)]
    if not matches:
        return None
    return f"{max(matches)}p"


def fallback_detail_for_requested_quality(requested_quality: str, selected_quality: str) -> str | None:
    if selected_quality == requested_quality:
        return None
    detail = (
        f"Requested {requested_quality} is currently unavailable from the provider; "
        f"falling back to {selected_quality}."
    )
    if int(selected_quality[:-1]) < int(requested_quality[:-1]) and not YTDLP_PO_TOKEN_ARGS:
        guidance = (
            "If exact higher quality is required, configure YTDLP_PO_TOKEN_ARGS "
            "(for example: youtube:po_token=android.gvs+<TOKEN>)."
        )
        detail = f"{detail} {guidance}"
    return detail


def probe_video_dimensions(path: Path) -> tuple[int | None, int | None]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(path),
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    if completed.returncode != 0:
        return None, None
    first_line = (completed.stdout or "").strip().splitlines()
    if not first_line:
        return None, None
    raw_width, _, raw_height = first_line[0].strip().partition("x")
    width = parse_dimension(raw_width)
    height = parse_dimension(raw_height)
    return width, height


def validate_downloaded_quality(path: Path, requested_quality: str) -> tuple[bool, str | None, int | None, str | None]:
    actual_width, actual_height = probe_video_dimensions(path)
    if actual_width is None and actual_height is None:
        return (
            False,
            None,
            None,
            f"Downloaded file was created but resolution could not be verified for requested {requested_quality}.",
        )
    selected_quality = quality_from_dimensions(actual_width, actual_height)
    if selected_quality is None:
        resolved_width = "unknown" if actual_width is None else str(actual_width)
        resolved_height = "unknown" if actual_height is None else str(actual_height)
        return (
            False,
            None,
            actual_height,
            f"Downloaded video resolution {resolved_width}x{resolved_height} is outside supported quality bands.",
        )
    return True, selected_quality, actual_height, None


def combined_download_options(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_formats = [item for item in formats if isinstance(item, dict)]
    logger.info("formats_normalized count=%s", len(normalized_formats))
    audio_available = any(has_audio_track(item) for item in normalized_formats)
    if not audio_available:
        logger.info("formats_no_audio")
        return []

    video_dimensions: set[tuple[int | None, int | None]] = set()
    for item in normalized_formats:
        if not has_video_track(item):
            continue
        width = parse_dimension(item.get("width"))
        height = parse_dimension(item.get("height"))
        if width is None and height is None:
            continue
        video_dimensions.add((width, height))
    logger.info(
        "formats_video_dimensions values=%s near_tolerance=%s long_tolerance=%s",
        sorted(video_dimensions),
        HEIGHT_NEAR_TOLERANCE,
        long_side_tolerance(),
    )

    options: list[dict[str, Any]] = []
    for height in TARGET_HEIGHTS:
        if not any(
            format_matches_quality(height, available_width, available_height)
            for available_width, available_height in video_dimensions
        ):
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


def parse_metadata_formats(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_formats = data.get("formats")
    if not isinstance(raw_formats, list):
        return []
    return [item for item in raw_formats if isinstance(item, dict)]


def fetch_video_metadata(url: str, operation: str) -> dict[str, Any]:
    runtime_args = yt_dlp_runtime_args(operation)
    cmd = [
        yt_dlp_binary(),
        *runtime_args,
        "--dump-single-json",
        "--no-playlist",
        url,
    ]
    with yt_dlp_slot(operation, url):
        completed = run_yt_dlp(cmd, YTDLP_METADATA_TIMEOUT_S, operation)

    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "yt-dlp failed."
        logger.warning("%s_failed url=%s detail=%s", operation, url, message[:250])
        raise HTTPException(status_code=400, detail=message)
    warning_text = completed.stderr.strip()
    if warning_text:
        logger.warning("%s_warnings url=%s detail=%s", operation, url, warning_text[:500])

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="yt-dlp returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="yt-dlp returned unexpected metadata payload.")
    return data


class UrlPayload(BaseModel):
    url: str = Field(..., min_length=1)


class VideoIdPayload(BaseModel):
    video_id: str = Field(..., alias="videoId", min_length=1)
    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_video_id(self) -> "VideoIdPayload":
        self.video_id = normalize_video_id(self.video_id)
        return self


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


class DownloadRequest(VideoIdPayload):
    format: RequestedFormat
    filename: str | None = None
    download_path: str | None = None
    progress_updates: bool = False


@app.get("/health")
def health() -> dict[str, str]:
    yt_dlp_binary()
    logger.info("health_ok")
    return {"status": "ok"}


@app.post("/api/formats")
def list_formats(payload: UrlPayload) -> JSONResponse:
    logger.info("formats_request url=%s", payload.url)
    data = fetch_video_metadata(payload.url, "formats")
    raw_formats = parse_metadata_formats(data)
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
    def __init__(self, cmd: list[str], url: str, video_id: str) -> None:
        self.stderr_buffer: list[str] = []
        self.url = url
        self.video_id = video_id
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
        start_active_download()
        logger.info("stream_spawned pid=%s url=%s", self.process.pid, url)
        update_download_status(
            self.video_id,
            status="starting",
            phase="starting",
            progress_percent=0.0,
            url=self.url,
        )
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
        update_download_status(
            self.video_id,
            status="failed",
            phase="failed",
            detail=detail,
            url=self.url,
        )
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
        if self.process.returncode == 0:
            update_download_status(
                self.video_id,
                status="downloaded",
                phase="completed",
                progress_percent=100.0,
                eta="00:00",
                detail=None,
                url=self.url,
            )
        else:
            detail = "".join(self.stderr_buffer).strip() or "yt-dlp download failed."
            update_download_status(
                self.video_id,
                status="failed",
                phase="failed",
                detail=detail,
                url=self.url,
            )
        if self.slot_acquired:
            JOB_SEMAPHORE.release()
            logger.info("job_end op=download_stream elapsed_s=%.3f", elapsed)
        finish_active_download()


class DownloadStatusStream:
    def __init__(
        self,
        cmd: list[str],
        url: str,
        video_id: str,
        user_output_path: Path,
        container_output_path: Path,
        requested_quality: str,
        selected_quality: str,
        fallback_detail: str | None,
    ) -> None:
        self.url = url
        self.video_id = video_id
        self.user_output_path = user_output_path
        self.container_output_path = container_output_path
        self.requested_quality = requested_quality
        self.selected_quality = selected_quality
        self.fallback_detail = fallback_detail
        self.slot_acquired = JOB_SEMAPHORE.acquire(timeout=JOB_ACQUIRE_TIMEOUT_S)
        if not self.slot_acquired:
            logger.warning("busy op=download_status_stream url=%s", url)
            raise HTTPException(status_code=429, detail="Server is busy. Please retry shortly.")
        self.started = time.monotonic()
        self.closed = False
        self.stderr_tail: list[str] = []
        self.stderr_buffer: list[str] = []
        self.state: dict[str, Any] = {
            "status": "starting",
            "phase": "starting",
            "progress_percent": 0.0,
            "eta": None,
            "speed": None,
            "videoId": video_id,
            "filename": user_output_path.name,
            "download_path": str(user_output_path),
            "container_download_path": str(container_output_path),
            "requested_quality": requested_quality,
            "selected_quality": selected_quality,
            "quality_fallback": fallback_detail,
        }
        logger.info("job_start op=download_status_stream url=%s", url)
        update_download_status(video_id, **self.state, url=url)
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
            logger.exception("status_stream_spawn_failed url=%s", url)
            raise
        start_active_download()
        self.stderr_thread = Thread(
            target=read_text_stream,
            args=(self.process.stderr, self.stderr_buffer),
            daemon=True,
        )
        self.stderr_thread.start()
        logger.info("status_stream_spawned pid=%s url=%s", self.process.pid, url)

    def iter_stream_lines(self, stream: Any) -> Iterator[str]:
        if stream is None:
            return
        pending = ""
        while True:
            chunk = stream.read(1024)
            if not chunk:
                break
            pending += chunk.decode("utf-8", errors="replace")
            parts = re.split(r"[\r\n]+", pending)
            pending = parts.pop() if parts else ""
            for part in parts:
                line = part.strip()
                if line:
                    yield line
        tail = pending.strip()
        if tail:
            yield tail

    def iterate(self) -> Iterator[str]:
        try:
            yield encode_sse_event("progress", self.state)
            if self.process.stdout is None:
                self.state.update({"status": "failed", "phase": "failed", "detail": "yt-dlp stdout is unavailable."})
                update_download_status(self.video_id, **self.state, url=self.url)
                yield encode_sse_event("error", self.state)
                return

            for line in self.iter_stream_lines(self.process.stdout):
                self.stderr_tail.append(line)
                if len(self.stderr_tail) > 25:
                    self.stderr_tail.pop(0)

                parsed = parse_download_status_line(line)
                if parsed:
                    self.state.update(parsed)
                    update_download_status(self.video_id, **self.state, url=self.url)
                    yield encode_sse_event("progress", self.state)
                    continue

                cleaned = ANSI_ESCAPE_RE.sub("", line).replace("\r", "").strip()
                if cleaned.startswith("[download]"):
                    self.state.update(
                        {
                            "status": "downloading",
                            "phase": "downloading",
                            "message": cleaned,
                        }
                    )
                    update_download_status(self.video_id, **self.state, url=self.url)
                    yield encode_sse_event("progress", self.state)

            if self.process.poll() is None:
                self.process.wait()
            self.stderr_thread.join(timeout=1)
            if self.process.returncode == 0:
                quality_ok, resolved_quality, actual_height, detail = validate_downloaded_quality(
                    self.container_output_path,
                    self.requested_quality,
                )
                if not quality_ok:
                    self.state.update(
                        {
                            "status": "failed",
                            "phase": "failed",
                            "detail": detail or "Downloaded quality verification failed.",
                            "resolved_height": actual_height,
                        }
                    )
                    update_download_status(self.video_id, **self.state, url=self.url)
                    yield encode_sse_event("error", self.state)
                    return
                final_quality = resolved_quality or self.selected_quality
                final_fallback_detail = fallback_detail_for_requested_quality(self.requested_quality, final_quality)
                self.state.update(
                    {
                        "status": "downloaded",
                        "phase": "completed",
                        "progress_percent": 100.0,
                        "eta": "00:00",
                        "detail": None,
                        "selected_quality": final_quality,
                        "quality_fallback": final_fallback_detail,
                        "resolved_height": actual_height,
                    }
                )
                update_download_status(self.video_id, **self.state, url=self.url)
                yield encode_sse_event("complete", self.state)
                return

            message = "yt-dlp download failed."
            error_lines = self.stderr_buffer + self.stderr_tail
            for line in reversed(error_lines):
                if line.startswith("ERROR:"):
                    message = line
                    break
            if message == "yt-dlp download failed." and error_lines:
                message = error_lines[-1]
            self.state.update({"status": "failed", "phase": "failed", "detail": message})
            update_download_status(self.video_id, **self.state, url=self.url)
            yield encode_sse_event("error", self.state)
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()
        if self.process.poll() is None:
            self.process.wait()
        self.stderr_thread.join(timeout=1)
        elapsed = time.monotonic() - self.started
        if self.slot_acquired:
            JOB_SEMAPHORE.release()
            logger.info("job_end op=download_status_stream elapsed_s=%.3f", elapsed)
        finish_active_download()


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
    video_id = payload.video_id
    url = video_id_to_url(video_id)
    logger.info(
        "download_request video_id=%s url=%s mode=%s",
        video_id,
        url,
        "save" if payload.download_path else "stream",
    )
    metadata = fetch_video_metadata(url, "download_preflight")
    preflight_formats = parse_metadata_formats(metadata)
    downloadable_formats = filter_downloadable_media_formats(preflight_formats)
    downloadable_options = combined_download_options(downloadable_formats)
    available_qualities = {
        str(option.get("quality"))
        for option in downloadable_options
        if isinstance(option.get("quality"), str)
    }
    selected_quality = payload.format.quality
    fallback_detail = None
    candidate_qualities = supported_fallback_qualities(payload.format.quality)
    selected_selector = build_quality_fallback_selector(payload.format.quality)

    extension = payload.format.ext or "mp4"
    stem = payload.filename
    if not stem:
        title = metadata.get("title")
        if isinstance(title, str):
            stem = title.strip()
        if not stem:
            stem = f"video_{video_id}" if payload.download_path else "download"
    filename = f"{sanitize_filename(stem)}.{sanitize_extension(extension)}"
    logger.info("download_filename resolved=%s", filename)
    logger.info(
        "download_quality requested=%s preflight_available=%s selector_candidates=%s",
        payload.format.quality,
        sorted(available_qualities, key=lambda quality: int(quality[:-1])),
        candidate_qualities,
    )
    if payload.progress_updates and not payload.download_path:
        raise HTTPException(
            status_code=400,
            detail="progress_updates=true is supported only when download_path is provided.",
        )

    if payload.download_path:
        user_output_path = resolve_output_path(payload.download_path, filename)
        container_output_path = map_host_path_for_container(user_output_path)
        container_output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = build_download_cmd(
            url=url,
            selector=selected_selector,
            output=str(container_output_path),
            extension=extension,
            allow_merge=True,
            show_progress=True,
        )
        if payload.progress_updates:
            logger.info("download_file_progress_stream url=%s user_output=%s", url, user_output_path)
            progress_stream = DownloadStatusStream(
                cmd=cmd,
                url=url,
                video_id=video_id,
                user_output_path=user_output_path,
                container_output_path=container_output_path,
                requested_quality=payload.format.quality,
                selected_quality=selected_quality,
                fallback_detail=fallback_detail,
            )
            return StreamingResponse(
                progress_stream.iterate(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                },
            )

        update_download_status(
            video_id,
            status="started",
            phase="queued",
            progress_percent=0.0,
            filename=user_output_path.name,
            download_path=str(user_output_path),
            container_download_path=str(container_output_path),
            url=url,
            requested_quality=payload.format.quality,
            selected_quality=selected_quality,
            quality_fallback=fallback_detail,
        )
        start_background_file_download(
            video_id=video_id,
            url=url,
            cmd=cmd,
            user_output_path=user_output_path,
            container_output_path=container_output_path,
            requested_quality=payload.format.quality,
            selected_quality=selected_quality,
            fallback_detail=fallback_detail,
        )
        return JSONResponse(
            {
                "status": "started",
                "videoId": video_id,
                "download_path": str(user_output_path),
                "container_download_path": str(container_output_path),
                "filename": user_output_path.name,
                "requested_quality": payload.format.quality,
                "selected_quality": selected_quality,
                "quality_fallback": fallback_detail,
            },
            status_code=202,
        )

    cmd = build_download_cmd(
        url=url,
        selector=selected_selector,
        output="-",
        extension=extension,
        allow_merge=False,
    )

    stream = DownloadStream(cmd, url, video_id)
    first_chunk = stream.prime()

    headers = {
        "Content-Disposition": quoted_filename(filename),
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }
    logger.info("download_stream_start url=%s filename=%s", url, filename)

    return StreamingResponse(
        stream.iterate(first_chunk),
        media_type="application/octet-stream",
        headers=headers,
    )


@app.get("/api/download/status/{video_id}")
def download_status(video_id: str) -> JSONResponse:
    normalized_video_id = normalize_video_id(video_id)
    current = read_download_status(normalized_video_id)
    if current is None:
        return JSONResponse(
            {
                "videoId": normalized_video_id,
                "status": "not_started",
                "phase": "idle",
            }
        )
    return JSONResponse(current)


@app.get("/api/download/status/stream/{video_id}")
def download_status_stream(video_id: str) -> StreamingResponse:
    normalized_video_id = normalize_video_id(video_id)
    current = read_download_status(normalized_video_id)
    if current is None:
        raise HTTPException(status_code=404, detail="No download status available for this videoId.")
    return StreamingResponse(
        iter_status_sse(normalized_video_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/download/running")
def download_running() -> JSONResponse:
    active_count = get_active_download_count()
    return JSONResponse(
        {
            "running": active_count > 0,
            "active_downloads": active_count,
        }
    )
