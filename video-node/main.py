import asyncio
import os
import uuid
import time
import yaml
import aiohttp
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, BackgroundTasks
import uvicorn


CONFIG_PATH = "config.yaml"

with open(CONFIG_PATH, "r") as f:
    cfg = yaml.safe_load(f)

NODE_ID = cfg["node_id"]

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

app = FastAPI()

trigger_queue: asyncio.Queue = asyncio.Queue()
ffmpeg_process: subprocess.Popen | None = None


def ensure_dirs():
    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
    CLIP_DIR.mkdir(parents=True, exist_ok=True)


def start_ffmpeg():
    global ffmpeg_process

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-f", "v4l2",
        "-input_format", cfg["camera"]["input_format"],
        "-video_size", f'{cfg["camera"]["width"]}x{cfg["camera"]["height"]}',
        "-framerate", str(cfg["camera"]["fps"]),
        "-i", cfg["camera"]["device"],
        "-c", "copy",
        "-an",
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
        if ffmpeg_process is None or ffmpeg_process.poll() is not None:
            print("FFmpeg not running, restarting...")
            start_ffmpeg()

        await asyncio.sleep(2)


def get_segments_by_mtime():
    files = list(SEGMENT_DIR.glob("segment_*.ts"))
    files = [f for f in files if f.stat().st_size > 0]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files


def select_recent_segments(seconds: int):
    files = get_segments_by_mtime()
    needed = seconds // SEGMENT_SECONDS + 2
    return files[-needed:]


async def build_clip(event_id: str) -> Path:
    total_seconds = PRE_SECONDS + POST_SECONDS

    # Wait for post-trigger video to be written.
    await asyncio.sleep(POST_SECONDS + 1)

    segments = select_recent_segments(total_seconds)

    if len(segments) < total_seconds:
        raise RuntimeError("Not enough video segments available")

    concat_file = CLIP_DIR / f"{event_id}.txt"
    output_file = CLIP_DIR / f"{event_id}.mp4"

    with open(concat_file, "w") as f:
        for segment in segments:
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

    concat_file.unlink(missing_ok=True)

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
async def trigger():
    event_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

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

    return {
        "status": "ok" if ffmpeg_ok else "degraded",
        "node_id": NODE_ID,
        "ffmpeg_running": ffmpeg_ok,
        "queue_size": trigger_queue.qsize(),
    }


@app.on_event("startup")
async def startup():
    ensure_dirs()
    start_ffmpeg()

    asyncio.create_task(monitor_ffmpeg())
    asyncio.create_task(trigger_worker())


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=cfg["trigger"]["port"],
    )
