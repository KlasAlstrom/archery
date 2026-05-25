import asyncio
import os
import uuid
import time
import yaml
import aiohttp
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, BackgroundTasks, Request, HTTPException
from fastapi import FastAPI, BackgroundTasks
import uvicorn
import socket
from contextlib import asynccontextmanager
import os
import shutil

CONFIG_PATH = "config.yaml"

with open(CONFIG_PATH, "r") as f:
    cfg = yaml.safe_load(f)

NODE_ID = os.environ.get(
    "NODE_ID",
    cfg.get("node_id", "node-unknown")
)

BASE_DIR = Path(cfg["buffer"]["base_dir"])
SEGMENT_DIR = BASE_DIR / "segments"
CLIP_DIR = BASE_DIR / "clips"

SEGMENT_COUNT = cfg["buffer"]["segment_count"]
SEGMENT_SECONDS = cfg["buffer"]["segment_seconds"]

PRE_SECONDS = cfg["trigger"]["pre_seconds"]
POST_SECONDS = cfg["trigger"]["post_seconds"]

UPLOAD_URL = cfg["server"]["upload_url"]
TOKEN = cfg["server"]["token"]

SEGMENT_PATTERN = SEGMENT_DIR / "segment_%03d.ts"

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    start_ffmpeg()

    monitor_task = asyncio.create_task(monitor_ffmpeg())
    trigger_task = asyncio.create_task(trigger_worker())
    heartbeat_task = asyncio.create_task(heartbeat_worker())

    try:
        yield

    finally:
        print("Shutting down video node...")

        monitor_task.cancel()
        trigger_task.cancel()
        heartbeat_task.cancel()

        stop_ffmpeg()


app = FastAPI(lifespan = lifespan)

trigger_queue: asyncio.Queue = asyncio.Queue()
ffmpeg_process: subprocess.Popen | None = None


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

async def heartbeat_worker():
    headers = {"Authorization": f"Bearer {TOKEN}"}
    heartbeat_url = UPLOAD_URL.replace("/api/upload", "/api/heartbeat")

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                data = aiohttp.FormData()
                data.add_field("node_id", NODE_ID)
                data.add_field("status", "ok")
                data.add_field("ip_address", get_local_ip())

                await session.post(
                    heartbeat_url,
                    data=data,
                    headers=headers,
                    timeout=5,
                )
        except Exception as e:
            print(f"Heartbeat failed: {e}")

        await asyncio.sleep(30)

def ensure_dirs():
    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
    CLIP_DIR.mkdir(parents=True, exist_ok=True)

    for f in SEGMENT_DIR.glob("segment_*.ts"):
        f.unlink(missing_ok=True)

    for f in CLIP_DIR.glob("*.txt"):
        f.unlink(missing_ok=True)


def buffer_ready():
    safe_files = get_segments_by_mtime()[:-1]
    return len(safe_files) >= PRE_SECONDS + 2


def start_ffmpeg():
    global ffmpeg_process

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",

        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-video_size", f'{cfg["camera"]["width"]}x{cfg["camera"]["height"]}',
        "-framerate", str(cfg["camera"]["fps"]),
        "-i", cfg["camera"]["device"],

        "-an",
        "-vf", "format=yuv420p",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",

        "-g", str(cfg["camera"]["fps"]),
        "-keyint_min", str(cfg["camera"]["fps"]),
        "-sc_threshold", "0",

        "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-segment_wrap", str(SEGMENT_COUNT),
        "-segment_format", "mpegts",
        "-reset_timestamps", "1",

        str(SEGMENT_PATTERN),
    ]

    ffmpeg_process = subprocess.Popen(cmd)
    print("FFmpeg started")


async def monitor_ffmpeg():
    global ffmpeg_process

    while True:
        ffmpeg_dead = ffmpeg_process is None or ffmpeg_process.poll() is not None

        if ffmpeg_dead:
            print("FFmpeg not running, restarting...")
            start_ffmpeg()

        elif not recorder_is_healthy():
            print("Recorder unhealthy, restarting FFmpeg...")
            stop_ffmpeg()
            start_ffmpeg()

        await asyncio.sleep(3)


def stop_ffmpeg():
    global ffmpeg_process

    if ffmpeg_process is None:
        return

    try:
        ffmpeg_process.terminate()
        ffmpeg_process.wait(timeout=5)
    except Exception:
        ffmpeg_process.kill()

    ffmpeg_process = None


def get_segments_by_mtime():
    files = list(SEGMENT_DIR.glob("segment_*.ts"))
    files = [f for f in files if f.stat().st_size > 0]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files


def select_recent_segments(seconds: int):
    files = get_segments_by_mtime()

    # Newest file may still be written by FFmpeg, so ignore it.
    safe_files = files[:-1]

    needed = seconds // SEGMENT_SECONDS + 2
    return safe_files[-needed:]


def recorder_is_healthy():
    files = get_segments_by_mtime()

    if not files:
        return False

    newest = files[-1]
    age = time.time() - newest.stat().st_mtime

    return age < 3


async def build_clip(event_id: str) -> Path:
    total_seconds = PRE_SECONDS + POST_SECONDS

    await asyncio.sleep(POST_SECONDS + 0.5)

    segments = select_recent_segments(total_seconds)

    if len(segments) < total_seconds:
        raise RuntimeError("Not enough video segments available")

    event_dir = CLIP_DIR / event_id
    event_dir.mkdir(parents=True, exist_ok=True)

    copied_segments = []

    for i, segment in enumerate(segments):
        target = event_dir / f"part_{i:03d}.ts"

        # Copy segment immediately so circular buffer cannot overwrite it
        shutil.copy2(segment, target)

        if target.stat().st_size == 0:
            raise RuntimeError(f"Copied empty segment: {segment}")

        copied_segments.append(target)

    concat_file = event_dir / "concat.txt"
    output_file = CLIP_DIR / f"{event_id}.mp4"

    with open(concat_file, "w") as f:
        for segment in copied_segments:
            f.write(f"file '{segment.resolve()}'\n")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_file),
    ]

    result = subprocess.run(cmd)

    shutil.rmtree(event_dir, ignore_errors=True)

    if result.returncode != 0:
        raise RuntimeError("Failed to build MP4 clip")

    return output_file


async def upload_clip(event_id: str, clip_path: Path, created_at: str):
    headers = {
        "Authorization": f"Bearer {TOKEN}"
    }

    timeout_at = time.monotonic() + cfg["clip"]["max_retry_seconds"]

    while time.monotonic() < timeout_at:
        try:
            async with aiohttp.ClientSession() as session:
                data = aiohttp.FormData()
                data.add_field("node_id", NODE_ID)
                data.add_field("event_id", event_id)
                data.add_field("timestamp", created_at)
                data.add_field("duration", str(PRE_SECONDS + POST_SECONDS))

                with open(clip_path, "rb") as f:
                    data.add_field(
                        "video",
                        f,
                        filename=f"{event_id}.mp4",
                        content_type="video/mp4",
                    )

                    async with session.post(
                        UPLOAD_URL,
                        data=data,
                        headers=headers,
                    ) as resp:
                        if resp.status == 200:
                            print(f"Uploaded {event_id}")
                            return True

                        print(f"Upload failed: HTTP {resp.status}")

        except Exception as e:
            print(f"Upload error: {e}")

        await asyncio.sleep(3)

    return False


async def trigger_worker():
    while True:
        event = await trigger_queue.get()

        event_id = event["event_id"]
        created_at = event["created_at"]

        try:
            print(f"Building clip {event_id}")
            clip = await build_clip(event_id)

            print(f"Uploading clip {event_id}")
            ok = await upload_clip(event_id, clip, created_at)

            if not ok:
                print(f"Dropping clip after failed upload: {event_id}")

            clip.unlink(missing_ok=True)

        except Exception as e:
            print(f"Event failed {event_id}: {e}")

        finally:
            trigger_queue.task_done()


@app.post("/trigger")
async def trigger(request: Request):
    body = {}

    try:
        body = await request.json()
    except Exception:
        pass

    event_id = body.get("event_id") or str(uuid.uuid4())
    created_at = body.get("created_at") or datetime.now(timezone.utc).isoformat()

    if not buffer_ready():
        raise HTTPException(status_code=503, detail="Video buffer not ready")

    await trigger_queue.put({
        "event_id": event_id,
        "created_at": created_at,
    })

    return {
        "status": "accepted",
        "event_id": event_id,
        "queue_size": trigger_queue.qsize(),
    }


@app.get("/health")
async def health():
    ffmpeg_ok = ffmpeg_process is not None and ffmpeg_process.poll() is None
    recorder_ok = recorder_is_healthy()

    return {
        "status": "ok" if ffmpeg_ok and recorder_ok else "degraded",
        "node_id": NODE_ID,
        "ffmpeg_running": ffmpeg_ok,
        "recorder_healthy": recorder_ok,
        "queue_size": trigger_queue.qsize(),
    }



# @app.on_event("shutdown")
# async def shutdown():
#     print("Shutting down video node...")
#     stop_ffmpeg()

# @app.on_event("startup")
# async def startup():
#     ensure_dirs()
#     start_ffmpeg()

#     asyncio.create_task(monitor_ffmpeg())
#     asyncio.create_task(trigger_worker())
#     asyncio.create_task(heartbeat_worker())

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=cfg["trigger"]["port"],
    )
