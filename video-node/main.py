"""FastAPI video node for buffering, clipping, previewing, and uploading camera footage."""

from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, TypedDict, cast

import aiohttp
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
import os
import signal

CONFIG_PATH = Path("config.yaml")
MAC_ADDRESS_PATH = Path("/sys/class/net/wlan0/address")
HEARTBEAT_INTERVAL_SECONDS = 10
FFMPEG_HEALTH_CHECK_INTERVAL_SECONDS = 3
RECORDER_MAX_SEGMENT_AGE_SECONDS = 8
UPLOAD_RETRY_INTERVAL_SECONDS = 3
PREVIEW_FRAME_RATE = 10
PREVIEW_SCALE = "640:-1"
ffmpeg_started_at = 0.0
current_mode = "recording"

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class TriggerEvent(TypedDict):
    event_id: str
    created_at: str
    trigger_index: int


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return cast(dict[str, Any], yaml.safe_load(file))


cfg = load_config()

BASE_DIR = Path(cfg["buffer"]["base_dir"])
SEGMENT_DIR = BASE_DIR / "segments"
CLIP_DIR = BASE_DIR / "clips"

SEGMENT_COUNT = int(cfg["buffer"]["segment_count"])
SEGMENT_SECONDS = int(cfg["buffer"]["segment_seconds"])
PRE_SECONDS = int(cfg["trigger"]["pre_seconds"])
POST_SECONDS = int(cfg["trigger"]["post_seconds"])
#TRIGGER_INDEX_OFFSET = int(cfg["trigger"].get("trigger_index_offset", 0))
TRIGGER_INDEX_OFFSET = int(cfg["trigger"]["trigger_index_offset"])
UPLOAD_URL = str(cfg["server"]["upload_url"])
TOKEN = str(cfg["server"]["token"])
SEGMENT_PATTERN = SEGMENT_DIR / "segment_%03d.ts"

ffmpeg_process: subprocess.Popen[bytes] | None = None
preview_process: subprocess.Popen[bytes] | None = None
preview_lock = asyncio.Lock()
trigger_queue: asyncio.Queue[TriggerEvent] = asyncio.Queue()


def generate_node_id() -> str:
    try:
        mac_address = MAC_ADDRESS_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        mac_address = "00:00:00:00:00:00"

    return f"Cam-{mac_address.replace(':', '')}"


NODE_ID = generate_node_id()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    ensure_directories()
    start_ffmpeg()

    tasks = [
        asyncio.create_task(monitor_ffmpeg(), name="monitor-ffmpeg"),
        asyncio.create_task(trigger_worker(), name="trigger-worker"),
        asyncio.create_task(heartbeat_worker(), name="heartbeat-worker"),
    ]

    try:
        yield
    finally:
        logger.info("Shutting down video node")
        for task in tasks:
            task.cancel()

        with suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks)

        stop_preview_process()
        stop_ffmpeg()


app = FastAPI(lifespan=lifespan)


def ensure_directories() -> None:
    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
    CLIP_DIR.mkdir(parents=True, exist_ok=True)

    for segment in SEGMENT_DIR.glob("segment_*.ts"):
        segment.unlink(missing_ok=True)

    for stale_concat_file in CLIP_DIR.glob("*.txt"):
        stale_concat_file.unlink(missing_ok=True)


def get_local_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def get_segments_by_mtime() -> list[Path]:
    segments = [path for path in SEGMENT_DIR.glob("segment_*.ts") if path.stat().st_size > 0]
    return sorted(segments, key=lambda path: path.stat().st_mtime)


def get_safe_segments() -> list[Path]:
    # The newest file may still be written by FFmpeg, so ignore it.
    return get_segments_by_mtime()[:-1]


def required_segment_count(seconds: int) -> int:
    return seconds // SEGMENT_SECONDS + 2


def segment_index(path: Path) -> int:
    # segment_007.ts -> 7
    return int(path.stem.split("_")[1])


def newest_safe_segment_index() -> int | None:
    safe_segments = get_safe_segments()

    if not safe_segments:
        return None

    return segment_index(safe_segments[-1])


def select_segments_around_index(
    trigger_index: int,
    pre_count: int,
    post_count: int,
) -> list[Path]:
    selected: list[Path] = []

    logger.info("trigger_index = %d pre_count = %d post_count = %d", trigger_index, pre_count, post_count)
    logger.info("trigger_index - pre_count = %d trigger_index + post_count + 1 = %d", trigger_index - pre_count, trigger_index + post_count + 1)
    for logical_index in range(trigger_index - pre_count, trigger_index + post_count + 1):
        wrapped_index = logical_index % SEGMENT_COUNT
        logger.info("wrapped_index = %d", wrapped_index)
        segment = SEGMENT_DIR / f"segment_{wrapped_index:03d}.ts"

        if segment.exists() and segment.stat().st_size > 0:
            selected.append(segment)

    return selected


def buffer_ready() -> bool:
    return len(get_safe_segments()) >= required_segment_count(PRE_SECONDS)


def select_recent_segments(seconds: int) -> list[Path]:
    return get_safe_segments()[-required_segment_count(seconds) :]


def recorder_is_healthy() -> bool:
    segments = get_segments_by_mtime()
    if not segments:
        return False

    newest_segment_age = time.time() - segments[-1].stat().st_mtime
    return newest_segment_age < RECORDER_MAX_SEGMENT_AGE_SECONDS


def build_recording_command() -> list[str]:
    camera_cfg = cfg["camera"]

    if camera_cfg.get("type") == "picam":
        return [
            "bash",
            "-lc",
            (
                f"rpicam-vid --nopreview -t 0 "
                f"--width {camera_cfg['width']} "
                f"--height {camera_cfg['height']} "
                f"--framerate {camera_cfg['fps']} "
                f"--codec h264 "
                f"--intra {camera_cfg['fps']} "
                f"--inline "
                f"-o - "
                f"| ffmpeg -hide_banner -loglevel warning "
                f"-f h264 -i pipe:0 "
                f"-c copy "
                f"-f segment "
                f"-segment_time {SEGMENT_SECONDS} "
                f"-segment_wrap {SEGMENT_COUNT} "
                f"-segment_format mpegts "
                f"-reset_timestamps 1 "
                f"{SEGMENT_PATTERN}"
            ),
        ]

    if camera_cfg.get("type") == "usb":
        fps = str(camera_cfg["fps"])

        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "v4l2",
            "-input_format",
            str(camera_cfg.get("input_format", "mjpeg")),
            "-video_size",
            f'{camera_cfg["width"]}x{camera_cfg["height"]}',
            "-framerate",
            fps,
            "-i",
            str(camera_cfg["device"]),
            "-an",
            "-vf",
            "format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-g",
            fps,
            "-keyint_min",
            fps,
            "-sc_threshold",
            "0",
            "-f",
            "segment",
            "-segment_time",
            str(SEGMENT_SECONDS),
            "-segment_wrap",
            str(SEGMENT_COUNT),
            "-segment_format",
            "mpegts",
            "-reset_timestamps",
            "1",
            str(SEGMENT_PATTERN),
        ]


def start_ffmpeg() -> None:
    global ffmpeg_process, ffmpeg_started_at

    if ffmpeg_process is not None and ffmpeg_process.poll() is None:
        return

    ffmpeg_process = subprocess.Popen(
        build_recording_command(),
        start_new_session=True,
    )
    ffmpeg_started_at = time.monotonic()
    logger.info("FFmpeg recorder started")


def stop_ffmpeg() -> None:
    global ffmpeg_process

    if ffmpeg_process is None:
        return

    terminate_process(ffmpeg_process)
    ffmpeg_process = None


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=5)
        except Exception:
            process.kill()
            process.wait(timeout=5)


async def monitor_ffmpeg() -> None:
    while True:
        if current_mode != "recording":
            await asyncio.sleep(FFMPEG_HEALTH_CHECK_INTERVAL_SECONDS)
            continue

        ffmpeg_dead = ffmpeg_process is None or ffmpeg_process.poll() is not None

        if ffmpeg_dead:
            logger.warning("FFmpeg is not running; restarting")
            start_ffmpeg()

        elif time.monotonic() - ffmpeg_started_at < 10:
            pass

        elif not recorder_is_healthy():
            logger.warning("Recorder is unhealthy; restarting FFmpeg")
            stop_ffmpeg()
            start_ffmpeg()

        await asyncio.sleep(FFMPEG_HEALTH_CHECK_INTERVAL_SECONDS)


async def heartbeat_worker() -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    heartbeat_url = UPLOAD_URL.replace("/api/upload", "/api/heartbeat")

    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            try:
                data = aiohttp.FormData()
                data.add_field("node_id", NODE_ID)
                data.add_field("status", "ok")
                data.add_field("ip_address", get_local_ip())

                async with session.post(heartbeat_url, data=data, timeout=5) as response:
                    if response.status >= 400:
                        logger.warning("Heartbeat failed with HTTP %s", response.status)
            except Exception:
                logger.exception("Heartbeat failed")

            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def build_clip(event_id: str, trigger_index: int) -> Path:
    pre_count = PRE_SECONDS // SEGMENT_SECONDS
    post_count = POST_SECONDS // SEGMENT_SECONDS

    # Important:
    # Do not test whether post-trigger segment files exist immediately.
    # In a circular buffer they may already exist as old files.
    # Wait until FFmpeg has had time to overwrite them with new post-trigger data.
    await asyncio.sleep(POST_SECONDS + 0.5)

    segments = select_segments_around_index(
        trigger_index=trigger_index,
        pre_count=pre_count,
        post_count=post_count,
    )

    if len(segments) < 2:
        raise RuntimeError("Too few video segments available")

    event_dir = CLIP_DIR / event_id
    output_file = CLIP_DIR / f"{event_id}.mp4"
    concat_file = event_dir / "concat.txt"

    try:
        event_dir.mkdir(parents=True, exist_ok=True)
        copied_segments = copy_segments_for_clip(segments, event_dir)
        write_concat_file(concat_file, copied_segments)
        run_ffmpeg_concat(concat_file, output_file)
        return output_file
    finally:
        shutil.rmtree(event_dir, ignore_errors=True)


def copy_segments_for_clip(segments: list[Path], event_dir: Path) -> list[Path]:
    copied_segments: list[Path] = []

    for index, segment in enumerate(segments):
        target = event_dir / f"part_{index:03d}.ts"
        shutil.copy2(segment, target)

        if target.stat().st_size == 0:
            raise RuntimeError(f"Copied empty segment: {segment}")

        copied_segments.append(target)

    return copied_segments


def write_concat_file(concat_file: Path, segments: list[Path]) -> None:
    with concat_file.open("w", encoding="utf-8") as file:
        for segment in segments:
            file.write(f"file '{segment.resolve()}'\n")


def run_ffmpeg_concat(concat_file: Path, output_file: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_file),
        ],
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError("Failed to build MP4 clip")


async def upload_clip(event_id: str, clip_path: Path, created_at: str) -> bool:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    timeout_at = time.monotonic() + int(cfg["clip"]["max_retry_seconds"])

    async with aiohttp.ClientSession(headers=headers) as session:
        while time.monotonic() < timeout_at:
            try:
                if await try_upload_clip(session, event_id, clip_path, created_at):
                    logger.info("Uploaded clip %s", event_id)
                    return True
            except Exception:
                logger.exception("Upload error for clip %s", event_id)

            await asyncio.sleep(UPLOAD_RETRY_INTERVAL_SECONDS)

    return False


async def try_upload_clip(
    session: aiohttp.ClientSession,
    event_id: str,
    clip_path: Path,
    created_at: str,
) -> bool:
    data = aiohttp.FormData()
    data.add_field("node_id", NODE_ID)
    data.add_field("event_id", event_id)
    data.add_field("timestamp", created_at)
    data.add_field("duration", str(PRE_SECONDS + POST_SECONDS))

    with clip_path.open("rb") as file:
        data.add_field(
            "video",
            file,
            filename=f"{event_id}.mp4",
            content_type="video/mp4",
        )

        async with session.post(UPLOAD_URL, data=data) as response:
            if response.status == 200:
                return True

            logger.warning("Upload failed for %s with HTTP %s", event_id, response.status)
            return False


async def trigger_worker() -> None:
    while True:
        event = await trigger_queue.get()

        try:
            event_id = event["event_id"]
            logger.info("Building clip %s", event_id)
            clip = await build_clip(event_id, event["trigger_index"])

            logger.info("Uploading clip %s", event_id)
            uploaded = await upload_clip(event_id, clip, event["created_at"])
            if not uploaded:
                logger.warning("Dropping clip after failed upload: %s", event_id)

            clip.unlink(missing_ok=True)
        except Exception:
            logger.exception("Event failed: %s", event)
        finally:
            trigger_queue.task_done()


def build_preview_command() -> list[str]:
    camera_cfg = cfg["camera"]

    if camera_cfg.get("type") == "picam":
        return [
            "bash",
            "-lc",
            (
                f"rpicam-vid --nopreview -t 0 "
                f"--width 640 "
                f"--height 360 "
                f"--framerate {PREVIEW_FRAME_RATE} "
                f"--codec mjpeg "
                f"--flush "
                f"-o -"
            ),
        ]

    if camera_cfg.get("type") == "usb":
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "v4l2",
            "-input_format",
            str(camera_cfg.get("input_format", "mjpeg")),
            "-video_size",
            f'{camera_cfg["width"]}x{camera_cfg["height"]}',
            "-framerate",
            str(PREVIEW_FRAME_RATE),
            "-i",
            str(camera_cfg["device"]),
            "-vf",
            f"scale={PREVIEW_SCALE}",
            "-q:v",
            "5",
            "-f",
            "mjpeg",
            "pipe:1",
        ]


def start_preview_process() -> None:
    global preview_process

    if preview_process is not None and preview_process.poll() is None:
        return
    preview_process = subprocess.Popen(
        build_preview_command(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        start_new_session=True,
    )


def stop_preview_process() -> None:
    global preview_process

    if preview_process is None:
        return

    terminate_process(preview_process)
    preview_process = None
    logger.info("Preview process stopped")


def mjpeg_generator() -> Iterator[bytes]:
    if preview_process is None or preview_process.stdout is None:
        return

    buffer = b""
    while (
        preview_process is not None
        and preview_process.poll() is None
    ):
        if preview_process is None or preview_process.stdout is None:
            break

        chunk = preview_process.stdout.read(4096)
        if not chunk:
            break

        buffer += chunk
        while True:
            start = buffer.find(b"\xff\xd8")
            end = buffer.find(b"\xff\xd9")

            if start == -1 or end == -1 or end <= start:
                break

            frame = buffer[start : end + 2]
            buffer = buffer[end + 2 :]
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"


async def parse_trigger_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}

    return body if isinstance(body, dict) else {}


@app.post("/trigger")
async def trigger(request: Request) -> dict[str, Any]:
    body = await parse_trigger_body(request)
    event_id = str(body.get("event_id") or uuid.uuid4())
    created_at = str(body.get("created_at") or datetime.now(timezone.utc).isoformat())

    if not buffer_ready():
        raise HTTPException(status_code=503, detail="Video buffer not ready")

    trigger_index = newest_safe_segment_index()
    logger.info("trigger_index = %d ", trigger_index)

    if trigger_index is not None:
        trigger_index = (trigger_index + TRIGGER_INDEX_OFFSET) % SEGMENT_COUNT

    if trigger_index is None:
        raise HTTPException(status_code=503, detail="Video buffer not ready")

    if not buffer_ready():
        raise HTTPException(status_code=503, detail="Video buffer not ready")
    logger.info("trigger_index = %d after offset", trigger_index)

    await trigger_queue.put({
        "event_id": event_id,
        "created_at": created_at,
        "trigger_index": trigger_index,
    })

    return {
        "status": "accepted",
        "event_id": event_id,
        "queue_size": trigger_queue.qsize(),
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    ffmpeg_ok = ffmpeg_process is not None and ffmpeg_process.poll() is None
    recorder_ok = recorder_is_healthy()

    return {
        "status": "ok" if ffmpeg_ok and recorder_ok else "degraded",
        "node_id": NODE_ID,
        "ffmpeg_running": ffmpeg_ok,
        "recorder_healthy": recorder_ok,
        "queue_size": trigger_queue.qsize(),
    }


@app.post("/preview/start")
async def preview_start() -> dict[str, str]:
    global current_mode

    async with preview_lock:
        current_mode = "preview"
        stop_ffmpeg()
        await asyncio.sleep(1.0)
        start_preview_process()

    return {"status": "preview_started"}


@app.get("/preview")
async def preview_stream() -> StreamingResponse:
    if preview_process is None or preview_process.poll() is not None:
        start_preview_process()

    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.post("/preview/stop")
async def preview_stop() -> dict[str, str]:
    global current_mode

    async with preview_lock:
        stop_preview_process()
        await asyncio.sleep(1.0)
        current_mode = "recording"
        start_ffmpeg()

    return {"status": "recording_started"}


@app.get("/mode")
async def mode() -> dict[str, str]:
    return {
        "mode": current_mode,
        "node_id": NODE_ID,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(cfg["trigger"]["port"]))
