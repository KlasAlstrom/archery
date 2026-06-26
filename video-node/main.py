"""FastAPI video node for buffering, clipping, live view, and uploading camera footage.

This version uses one camera owner only: the recorder process. The recorder writes
both the rolling MPEG-TS segment buffer used for trigger clips and a continuously
updated JPEG frame used by /live for browser live view. Live view no longer stops
recording.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
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

CONFIG_PATH = Path("config.yaml")
MAC_ADDRESS_PATH = Path("/sys/class/net/wlan0/address")
HEARTBEAT_INTERVAL_SECONDS = 10
FFMPEG_HEALTH_CHECK_INTERVAL_SECONDS = 3
RECORDER_MAX_SEGMENT_AGE_SECONDS = 8
UPLOAD_RETRY_INTERVAL_SECONDS = 3
LIVE_FPS = 10
LIVE_SCALE = "640:-1"
LIVE_POLL_SECONDS = 0.10

last_trigger_at = time.monotonic()
ffmpeg_started_at = 0.0
current_mode = "recording"  # recording, sleeping

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class TriggerEvent(TypedDict):
    event_id: str
    created_at: str
    trigger_segment: str
    pre_seconds: int
    post_seconds: int


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return cast(dict[str, Any], yaml.safe_load(file))


cfg = load_config()

BASE_DIR = Path(cfg["buffer"]["base_dir"])
SEGMENT_DIR = BASE_DIR / "segments"
CLIP_DIR = BASE_DIR / "clips"
LIVE_JPEG_PATH = BASE_DIR / "live.jpg"

PRE_SECONDS = int(cfg["trigger"]["pre_seconds"])
POST_SECONDS = int(cfg["trigger"]["post_seconds"])
PRE_SECONDS_DELAYED = int(cfg["trigger"].get("pre_seconds_delayed", PRE_SECONDS))
POST_SECONDS_DELAYED = int(cfg["trigger"].get("post_seconds_delayed", POST_SECONDS))
TRIGGER_SEGMENT_OFFSET = int(cfg["trigger"].get("trigger_segment_offset", 0))

SEGMENT_SECONDS = int(cfg["buffer"]["segment_seconds"])
BUFFER_EXTRA_SECONDS = int(cfg["buffer"].get("extra_seconds", 10))
POWER_IDLE_TIMEOUT_SECONDS = int(cfg.get("power", {}).get("idle_timeout_seconds", 900))
WAKE_ON_TRIGGER = bool(cfg.get("power", {}).get("wake_on_trigger", True))

SEGMENT_COUNT = max(
    10,
    (PRE_SECONDS + POST_SECONDS + BUFFER_EXTRA_SECONDS) // SEGMENT_SECONDS,
    (PRE_SECONDS_DELAYED + POST_SECONDS_DELAYED + BUFFER_EXTRA_SECONDS) // SEGMENT_SECONDS,
)

UPLOAD_URL = str(cfg["server"]["upload_url"])
TOKEN = str(cfg["server"]["token"])
SEGMENT_PATTERN = SEGMENT_DIR / "segment_%03d.ts"

ffmpeg_process: subprocess.Popen[bytes] | None = None
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
        asyncio.create_task(power_worker(), name="power-worker"),
    ]

    try:
        yield
    finally:
        logger.info("Shutting down video node")
        for task in tasks:
            task.cancel()

        with suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks)

        stop_ffmpeg()


app = FastAPI(lifespan=lifespan)


def wake_recording() -> None:
    global current_mode, ffmpeg_started_at, last_trigger_at

    if current_mode == "recording":
        return

    current_mode = "recording"
    start_ffmpeg()
    ffmpeg_started_at = time.monotonic()
    last_trigger_at = time.monotonic()
    logger.info("Node woke up; recording started")


def sleep_recording() -> None:
    global current_mode

    if current_mode != "recording":
        return

    stop_ffmpeg()
    current_mode = "sleeping"
    logger.info("Node entered sleep mode")


def ensure_directories() -> None:
    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_JPEG_PATH.unlink(missing_ok=True)

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
    segments = [
        path for path in SEGMENT_DIR.glob("segment_*.ts")
        if path.stat().st_size > 0
    ]
    return sorted(segments, key=lambda path: (path.stat().st_mtime, path.name))


def get_safe_segments() -> list[Path]:
    # The newest file may still be written by FFmpeg, so ignore it.
    return get_segments_by_mtime()[:-1]


def newest_safe_segment() -> Path | None:
    safe_segments = get_safe_segments()
    if not safe_segments:
        return None
    return safe_segments[-1]


def required_segment_count(seconds: int) -> int:
    return seconds // SEGMENT_SECONDS + 2


def buffer_ready(pre_seconds: int = PRE_SECONDS) -> bool:
    return len(get_safe_segments()) >= required_segment_count(pre_seconds)


def offset_segment(segment: Path, offset: int) -> Path:
    segments = get_segments_by_mtime()
    names = [path.name for path in segments]

    if segment.name not in names:
        return segment

    pos = names.index(segment.name)
    new_pos = min(len(segments) - 1, max(0, pos + offset))
    return segments[new_pos]


def select_segments_around_trigger_segment(
    trigger_segment_name: str,
    pre_count: int,
    post_count: int,
) -> list[Path]:
    segments = get_segments_by_mtime()
    names = [segment.name for segment in segments]

    if trigger_segment_name not in names:
        trigger_pos = max(0, len(segments) - post_count - 1)
    else:
        trigger_pos = names.index(trigger_segment_name)

    start = max(0, trigger_pos - pre_count)
    end = min(len(segments), trigger_pos + post_count + 1)
    selected = segments[start:end]

    logger.info(
        "clip_select trigger=%s trigger_pos=%s selected=%s",
        trigger_segment_name,
        trigger_pos,
        [path.name for path in selected],
    )

    return selected


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
                f"| ffmpeg -y -hide_banner -loglevel warning "
                f"-f h264 -i pipe:0 "
                f"-map 0:v -c:v copy "
                f"-f segment "
                f"-segment_time {SEGMENT_SECONDS} "
                f"-segment_wrap {SEGMENT_COUNT} "
                f"-segment_format mpegts "
                f"-reset_timestamps 1 "
                f"{SEGMENT_PATTERN} "
                f"-map 0:v -vf fps={LIVE_FPS},scale={LIVE_SCALE} "
                f"-q:v 5 -update 1 -f image2 {LIVE_JPEG_PATH}"
            ),
        ]

    if camera_cfg.get("type") == "usb":
        fps = str(camera_cfg["fps"])
        return [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "v4l2",
            "-input_format", str(camera_cfg.get("input_format", "mjpeg")),
            "-video_size", f'{camera_cfg["width"]}x{camera_cfg["height"]}',
            "-framerate", fps,
            "-i", str(camera_cfg["device"]),
            "-an",
            "-filter_complex",
            f"[0:v]split=2[rec][live];[rec]format=yuv420p[recout];[live]fps={LIVE_FPS},scale={LIVE_SCALE}[liveout]",
            "-map", "[recout]",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-g", fps,
            "-keyint_min", fps,
            "-sc_threshold", "0",
            "-f", "segment",
            "-segment_time", str(SEGMENT_SECONDS),
            "-segment_wrap", str(SEGMENT_COUNT),
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            str(SEGMENT_PATTERN),
            "-map", "[liveout]",
            "-q:v", "5",
            "-update", "1",
            "-f", "image2",
            str(LIVE_JPEG_PATH),
        ]

    raise RuntimeError(f"Unsupported camera type: {camera_cfg.get('type')}")


def start_ffmpeg() -> None:
    global ffmpeg_process, ffmpeg_started_at

    if ffmpeg_process is not None and ffmpeg_process.poll() is None:
        return
    
    LIVE_JPEG_PATH.unlink(missing_ok=True)

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


def heartbeat_status() -> str:
    return {
        "recording": "ready",
        "sleeping": "sleeping",
    }.get(current_mode, "unknown")


async def heartbeat_worker() -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    heartbeat_url = UPLOAD_URL.replace("/api/upload", "/api/heartbeat")

    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            try:
                data = aiohttp.FormData()
                data.add_field("node_id", NODE_ID)
                data.add_field("status", heartbeat_status())
                data.add_field("ip_address", get_local_ip())

                async with session.post(heartbeat_url, data=data, timeout=5) as response:
                    if response.status >= 400:
                        logger.warning("Heartbeat failed with HTTP %s", response.status)
            except Exception:
                logger.exception("Heartbeat failed")

            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def power_worker() -> None:
    while True:
        if current_mode == "recording":
            idle_seconds = time.monotonic() - last_trigger_at
            if idle_seconds >= POWER_IDLE_TIMEOUT_SECONDS:
                sleep_recording()

        await asyncio.sleep(5)


async def build_clip(
    event_id: str,
    trigger_segment: str,
    pre_seconds: int,
    post_seconds: int,
) -> Path:
    pre_count = pre_seconds // SEGMENT_SECONDS
    post_count = post_seconds // SEGMENT_SECONDS

    await asyncio.sleep(post_seconds + 0.5)

    segments = select_segments_around_trigger_segment(
        trigger_segment_name=trigger_segment,
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


async def upload_clip(
    event_id: str,
    clip_path: Path,
    created_at: str,
    duration_seconds: int,
) -> bool:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    timeout_at = time.monotonic() + int(cfg["clip"]["max_retry_seconds"])

    async with aiohttp.ClientSession(headers=headers) as session:
        while time.monotonic() < timeout_at:
            try:
                if await try_upload_clip(session, event_id, clip_path, created_at, duration_seconds):
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
    duration_seconds: int,
) -> bool:
    data = aiohttp.FormData()
    data.add_field("node_id", NODE_ID)
    data.add_field("event_id", event_id)
    data.add_field("timestamp", created_at)
    data.add_field("duration", str(duration_seconds))

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

            pre_seconds = event["pre_seconds"]
            post_seconds = event["post_seconds"]
            duration_seconds = pre_seconds + post_seconds

            clip = await build_clip(
                event_id,
                event["trigger_segment"],
                pre_seconds,
                post_seconds,
            )

            logger.info("Uploading clip %s", event_id)
            uploaded = await upload_clip(
                event_id,
                clip,
                event["created_at"],
                duration_seconds,
            )

            if not uploaded:
                logger.warning("Dropping clip after failed upload: %s", event_id)

            clip.unlink(missing_ok=True)
        except Exception:
            logger.exception("Event failed: %s", event)
        finally:
            trigger_queue.task_done()


def latest_jpeg_bytes() -> bytes | None:
    if not LIVE_JPEG_PATH.exists():
        return None

    try:
        data = LIVE_JPEG_PATH.read_bytes()
    except OSError:
        return None

    start = data.find(b"\xff\xd8")
    end = data.rfind(b"\xff\xd9")

    if start == -1 or end == -1 or end <= start:
        return None

    return data[start:end + 2]


def live_mjpeg_generator() -> Iterator[bytes]:
    last_frame: bytes | None = None

    while True:
        if current_mode != "recording":
            time.sleep(LIVE_POLL_SECONDS)
            continue

        frame = latest_jpeg_bytes()
        if frame is None or frame == last_frame:
            time.sleep(LIVE_POLL_SECONDS)
            continue

        last_frame = frame
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Cache-Control: no-cache\r\n\r\n"
            + frame
            + b"\r\n"
        )


async def parse_trigger_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}

    return body if isinstance(body, dict) else {}


async def queue_trigger(
    request: Request,
    pre_seconds: int,
    post_seconds: int,
) -> dict[str, Any]:
    global last_trigger_at

    body = await parse_trigger_body(request)
    event_id = str(body.get("event_id") or uuid.uuid4())
    created_at = str(body.get("created_at") or datetime.now(timezone.utc).isoformat())

    last_trigger_at = time.monotonic()

    if current_mode == "sleeping":
        if WAKE_ON_TRIGGER:
            wake_recording()
            return {
                "status": "waking",
                "event_id": event_id,
                "clip_created": False,
            }

        raise HTTPException(status_code=503, detail="Node sleeping")

    if current_mode != "recording":
        raise HTTPException(status_code=503, detail=f"Node not recording: {current_mode}")

    if not buffer_ready(pre_seconds):
        raise HTTPException(status_code=503, detail="Video buffer not ready")

    trigger_segment = newest_safe_segment()
    if trigger_segment is None:
        raise HTTPException(status_code=503, detail="Video buffer not ready")

    if TRIGGER_SEGMENT_OFFSET:
        trigger_segment = offset_segment(trigger_segment, TRIGGER_SEGMENT_OFFSET)

    await trigger_queue.put({
        "event_id": event_id,
        "created_at": created_at,
        "trigger_segment": trigger_segment.name,
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
    })

    return {
        "status": "accepted",
        "event_id": event_id,
        "queue_size": trigger_queue.qsize(),
    }


@app.post("/trigger")
async def trigger(request: Request) -> dict[str, Any]:
    return await queue_trigger(
        request=request,
        pre_seconds=PRE_SECONDS,
        post_seconds=POST_SECONDS,
    )


@app.post("/trigger_delayed")
async def trigger_delayed_node(request: Request) -> dict[str, Any]:
    return await queue_trigger(
        request=request,
        pre_seconds=PRE_SECONDS_DELAYED,
        post_seconds=POST_SECONDS_DELAYED,
    )


@app.get("/live")
async def live_stream() -> StreamingResponse:
    return StreamingResponse(
        live_mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    ffmpeg_ok = ffmpeg_process is not None and ffmpeg_process.poll() is None
    recorder_ok = recorder_is_healthy() if current_mode == "recording" else False

    return {
        "status": "ok" if ffmpeg_ok and recorder_ok else "degraded",
        "node_id": NODE_ID,
        "mode": current_mode,
        "ffmpeg_running": ffmpeg_ok,
        "recorder_healthy": recorder_ok,
        "queue_size": trigger_queue.qsize(),
    }


@app.post("/wake")
async def wake() -> dict[str, Any]:
    wake_recording()
    return {"status": "recording_started"}


@app.get("/mode")
async def mode() -> dict[str, str]:
    return {
        "mode": current_mode,
        "node_id": NODE_ID,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(cfg["trigger"]["port"]))
