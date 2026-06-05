"""FastAPI video event server.

This module receives MP4 clips from camera nodes, stores them on disk, tracks
metadata in SQLAlchemy, and exposes a small web UI for browsing, status, and
camera aiming.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]
VIDEO_STORAGE = Path(os.environ["VIDEO_STORAGE"])
NODE_TOKEN = os.environ["NODE_TOKEN"]

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
NODE_ONLINE_WINDOW = timedelta(seconds=25)
VIDEO_RETENTION = timedelta(days=30)
NODE_REQUEST_TIMEOUT = 5.0
THUMBNAIL_TIMESTAMP = "00:00:02"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("video-server")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()
app = FastAPI()


class Video(Base):
    __tablename__ = "videos"

    id = Column(String, primary_key=True)
    node_id = Column(String, index=True, nullable=False)
    event_id = Column(String, nullable=False)
    created_at = Column(DateTime, index=True, nullable=False)
    uploaded_at = Column(DateTime, nullable=False)
    duration_seconds = Column(Integer, nullable=False)
    filesize_bytes = Column(BigInteger, nullable=False)
    storage_path = Column(String, nullable=False)
    thumbnail_path = Column(String, nullable=True)


class Node(Base):
    __tablename__ = "nodes"

    node_id = Column(String, primary_key=True)
    alias = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    last_seen = Column(DateTime, nullable=False)
    status = Column(String, nullable=False)


@contextmanager
def db_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def next_node_alias(session: Session) -> str:
    count = session.query(Node).count()
    return f"cam-{count + 1}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_iso(dt: datetime) -> str:
    return ensure_utc(dt).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    try:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        logger.warning("invalid_timestamp value=%s", value)
        return utc_now()


def require_node_token(authorization: str | None) -> None:
    if authorization != f"Bearer {NODE_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def is_node_online(node: Node, now: datetime | None = None) -> bool:
    return (now or utc_now()) - ensure_utc(node.last_seen) < NODE_ONLINE_WINDOW


def node_to_dict(node: Node, now: datetime | None = None) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "alias": node.alias or node.node_id,
        "ip_address": node.ip_address,
        "status": "online" if is_node_online(node, now) else "offline",
        "last_seen": utc_iso(node.last_seen),
    }


def video_to_dict(video: Video) -> dict[str, Any]:
    return {
        "id": video.id,
        "node_id": video.node_id,
        "event_id": video.event_id,
        "created_at": utc_iso(video.created_at),
        "duration_seconds": video.duration_seconds,
        "filesize_bytes": video.filesize_bytes,
    }


def validate_upload(video: UploadFile) -> None:
    filename = video.filename or ""

    if not filename.lower().endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Only MP4 files are allowed")

    if video.content_type != "video/mp4":
        raise HTTPException(status_code=400, detail="Invalid content type")


def create_storage_paths(node_id: str, created_at: datetime, video_id: str) -> tuple[Path, Path]:
    day_dir = VIDEO_STORAGE / node_id / created_at.strftime("%Y") / created_at.strftime("%m") / created_at.strftime("%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir / f"{video_id}.mp4", day_dir / f"{video_id}.uploading"


def save_upload(video: UploadFile, output_path: Path, tmp_path: Path) -> int:
    with tmp_path.open("wb") as file_handle:
        shutil.copyfileobj(video.file, file_handle)

    tmp_path.rename(output_path)
    return output_path.stat().st_size


def validate_file_size(path: Path, size_bytes: int) -> None:
    if size_bytes == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Empty video file")

    if size_bytes > MAX_UPLOAD_BYTES:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail="Video too large")


def generate_thumbnail(video_path: Path, thumbnail_path: Path) -> bool:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-ss",
        THUMBNAIL_TIMESTAMP,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(thumbnail_path),
    ]

    result = subprocess.run(command, check=False)
    return result.returncode == 0


def delete_file(path: Path | None) -> None:
    if path and path.exists():
        path.unlink()


def get_video_or_404(session: Session, video_id: str) -> Video:
    video = session.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def get_node_or_404(node_id: str) -> dict[str, str]:
    with db_session() as session:
        node = session.query(Node).filter(Node.node_id == node_id).first()
        if not node or not node.ip_address:
            raise HTTPException(status_code=404, detail="Node not found or missing IP")
        return {"node_id": node.node_id, "ip_address": node.ip_address}


@app.on_event("startup")
def startup() -> None:
    VIDEO_STORAGE.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


@app.post("/api/upload")
async def upload_video(
    video: UploadFile = File(...),
    node_id: str = Form(...),
    event_id: str = Form(...),
    timestamp: str = Form(...),
    duration: int = Form(...),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_node_token(authorization)
    validate_upload(video)

    video_id = str(uuid.uuid4())
    created_at = parse_timestamp(timestamp)
    output_path, tmp_path = create_storage_paths(node_id, created_at, video_id)

    try:
        filesize = save_upload(video, output_path, tmp_path)
        validate_file_size(output_path, filesize)

        thumbnail_path = output_path.with_suffix(".jpg")
        thumbnail_created = generate_thumbnail(output_path, thumbnail_path)
        thumbnail_str = str(thumbnail_path) if thumbnail_created else None

        if not thumbnail_created:
            logger.warning("thumbnail_failed video_id=%s path=%s", video_id, output_path)

        with db_session() as session:
            session.add(
                Video(
                    id=video_id,
                    node_id=node_id,
                    event_id=event_id,
                    created_at=created_at,
                    uploaded_at=utc_now(),
                    duration_seconds=duration,
                    filesize_bytes=filesize,
                    storage_path=str(output_path),
                    thumbnail_path=thumbnail_str,
                )
            )
            session.commit()

        logger.info(
            "upload_ok video_id=%s event_id=%s node_id=%s size=%s",
            video_id,
            event_id,
            node_id,
            filesize,
        )
        return {"status": "ok", "video_id": video_id, "filesize": filesize}
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


@app.post("/api/nodes/{node_id}/alias")
async def update_node_alias(
    node_id: str,
    request: Request,
) -> dict[str, Any]:
    body = await request.json()
    alias = str(body.get("alias", "")).strip()

    if not alias:
        raise HTTPException(status_code=400, detail="Alias may not be empty")

    with db_session() as session:
        node = session.query(Node).filter(Node.node_id == node_id).first()

        if not node:
            raise HTTPException(status_code=404, detail="Node not found")

        node.alias = alias
        session.commit()

    return {
        "status": "ok",
        "node_id": node_id,
        "alias": alias,
    }


@app.get("/api/videos")
def list_videos(limit: int = 100, offset: int = 0, node_id: str | None = None) -> dict[str, Any]:
    with db_session() as session:
        query = session.query(Video).order_by(Video.created_at.desc())

        if node_id:
            query = query.filter(Video.node_id == node_id)

        return {
            "total": query.count(),
            "limit": limit,
            "offset": offset,
            "items": [video_to_dict(video) for video in query.offset(offset).limit(limit).all()],
        }


@app.get("/api/video/{video_id}")
def get_video(video_id: str) -> FileResponse:
    with db_session() as session:
        video = get_video_or_404(session, video_id)
        path = Path(video.storage_path)

    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing")

    return FileResponse(path, media_type="video/mp4", filename=f"{video_id}.mp4")


@app.post("/api/heartbeat")
def heartbeat(
    node_id: str = Form(...),
    status: str = Form("ok"),
    ip_address: str | None = Form(None),
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    require_node_token(authorization)

    with db_session() as session:
        node = session.query(Node).filter(Node.node_id == node_id).first()

        if node is None:
            session.add(Node(
                node_id=node_id,
                alias=next_node_alias(session),
                ip_address=ip_address,
                last_seen=utc_now(),
                status=status,
            ))
        else:
            node.ip_address = ip_address
            node.last_seen = utc_now()
            node.status = status

        session.commit()

    return {"status": "ok"}


@app.get("/api/nodes")
def list_nodes() -> list[dict[str, Any]]:
    with db_session() as session:
        now = utc_now()
        nodes = session.query(Node).order_by(Node.node_id).all()
        return [node_to_dict(node, now) for node in nodes]


@app.post("/api/cleanup")
def cleanup_old_videos(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_node_token(authorization)
    cutoff = utc_now() - VIDEO_RETENTION
    deleted = 0

    with db_session() as session:
        videos = session.query(Video).filter(Video.created_at < cutoff).all()

        for video in videos:
            delete_file(Path(video.storage_path))
            delete_file(Path(video.thumbnail_path) if video.thumbnail_path else None)
            session.delete(video)
            deleted += 1

        session.commit()

    logger.info("cleanup_deleted count=%s", deleted)
    return {"status": "ok", "deleted": deleted}


@app.get("/api/thumbnail/{video_id}")
def get_thumbnail(video_id: str) -> FileResponse:
    with db_session() as session:
        video = get_video_or_404(session, video_id)
        if not video.thumbnail_path:
            raise HTTPException(status_code=404, detail="Thumbnail not found")
        thumbnail = Path(video.thumbnail_path)

    if not thumbnail.exists():
        raise HTTPException(status_code=404, detail="Thumbnail file missing")

    return FileResponse(thumbnail, media_type="image/jpeg")


@app.post("/api/trigger-all")
async def trigger_all() -> dict[str, Any]:
    shared_event_id = str(uuid.uuid4())
    created_at = utc_now().isoformat()
    targets: list[dict[str, str]] = []

    with db_session() as session:
        now = utc_now()
        for node in session.query(Node).all():
            if node.ip_address and is_node_online(node, now):
                targets.append(
                    {
                        "node_id": node.node_id,
                        "ip_address": node.ip_address,
                        "url": f"http://{node.ip_address}:8080/trigger",
                    }
                )

    results = []
    async with httpx.AsyncClient(timeout=NODE_REQUEST_TIMEOUT) as client:
        for target in targets:
            try:
                response = await client.post(
                    target["url"],
                    json={"event_id": shared_event_id, "created_at": created_at},
                )
                results.append(
                    {
                        "node_id": target["node_id"],
                        "ip_address": target["ip_address"],
                        "ok": response.status_code == 200,
                        "status_code": response.status_code,
                    }
                )
            except httpx.HTTPError as exc:
                results.append(
                    {
                        "node_id": target["node_id"],
                        "ip_address": target["ip_address"],
                        "ok": False,
                        "error": str(exc),
                    }
                )

    logger.info(
        "trigger_all event_id=%s targets=%s ok=%s",
        shared_event_id,
        len(results),
        sum(1 for result in results if result.get("ok")),
    )

    return {
        "status": "ok",
        "event_id": shared_event_id,
        "triggered": len(results),
        "results": results,
    }


@app.get("/api/events")
def list_events(limit: int = 100, offset: int = 0, node_id: str | None = None) -> dict[str, Any]:
    with db_session() as session:
        query = session.query(Video).order_by(Video.created_at.desc())
        if node_id:
            query = query.filter(Video.node_id == node_id)
        videos = query.all()

    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for video in videos:
        event = grouped.setdefault(
            video.event_id,
            {"event_id": video.event_id, "created_at": video.created_at, "clips": []},
        )
        event["clips"].append(
            {
                "id": video.id,
                "node_id": video.node_id,
                "created_at": utc_iso(video.created_at),
                "duration_seconds": video.duration_seconds,
                "filesize_bytes": video.filesize_bytes,
            }
        )
        event["created_at"] = min(event["created_at"], video.created_at)

    events = sorted(grouped.values(), key=lambda event: event["created_at"], reverse=True)
    page = events[offset : offset + limit]

    for event in page:
        event["created_at"] = utc_iso(event["created_at"])

    return {"total": len(events), "limit": limit, "offset": offset, "items": page}


@app.delete("/api/events/{event_id}")
def delete_event(event_id: str) -> dict[str, Any]:
    deleted = 0

    with db_session() as session:
        videos = session.query(Video).filter(Video.event_id == event_id).all()

        for video in videos:
            delete_file(Path(video.storage_path))
            delete_file(Path(video.thumbnail_path) if video.thumbnail_path else None)
            session.delete(video)
            deleted += 1

        session.commit()

    logger.info("delete_event event_id=%s deleted=%s", event_id, deleted)
    return {"status": "ok", "event_id": event_id, "deleted": deleted}


@app.get("/api/status")
def server_status() -> dict[str, Any]:
    total, used, free = shutil.disk_usage(VIDEO_STORAGE)

    with db_session() as session:
        now = utc_now()
        nodes = session.query(Node).order_by(Node.node_id).all()
        latest_video = session.query(Video).order_by(Video.uploaded_at.desc()).first()
        video_count = session.query(Video).count()

        return {
            "storage": {"total_bytes": total, "used_bytes": used, "free_bytes": free},
            "nodes": [node_to_dict(node, now) for node in nodes],
            "video_count": video_count,
            "latest_upload": utc_iso(latest_video.uploaded_at) if latest_video else None,
        }


@app.post("/api/nodes/{node_id}/preview/start")
async def api_preview_start(node_id: str) -> dict[str, Any]:
    node = get_node_or_404(node_id)

    async with httpx.AsyncClient(timeout=NODE_REQUEST_TIMEOUT) as client:
        response = await client.post(f"http://{node['ip_address']}:8080/preview/start")

    return {"status": "ok", "node_id": node_id, "node_response": response.json()}


@app.post("/api/nodes/{node_id}/preview/stop")
async def api_preview_stop(node_id: str) -> dict[str, Any]:
    node = get_node_or_404(node_id)

    async with httpx.AsyncClient(timeout=NODE_REQUEST_TIMEOUT) as client:
        response = await client.post(f"http://{node['ip_address']}:8080/preview/stop")

    return {"status": "ok", "node_id": node_id, "node_response": response.json()}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


@app.get("/status", response_class=HTMLResponse)
def status_page() -> str:
    return STATUS_HTML


@app.get("/aim", response_class=HTMLResponse)
def aim_page() -> str:
    return AIM_HTML


@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: str) -> dict[str, Any]:
    with db_session() as session:
        node = session.query(Node).filter(Node.node_id == node_id).first()

        if not node:
            raise HTTPException(status_code=404, detail="Node not found")

        if is_node_online(node):
            raise HTTPException(status_code=400, detail="Cannot delete online node")

        session.delete(node)
        session.commit()

    return {"status": "ok", "deleted": node_id}

COMMON_CSS = """
<style>
  :root {
    --bg: #f6f6f6;
    --panel: #ffffff;
    --text: #111111;
    --muted: #555555;
    --border: #dddddd;
    --primary: #222222;
    --primary-text: #ffffff;
  }

  * {
    box-sizing: border-box;
  }

  body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0;
    background: var(--bg);
    color: var(--text);
  }

  header {
    position: sticky;
    top: 0;
    z-index: 10;
    background: var(--panel);
    padding: 12px;
    border-bottom: 1px solid var(--border);
  }

  h1, h2 {
    margin: 0 0 10px 0;
  }

  .top-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  a {
    color: inherit;
    text-decoration: none;
  }

  button, select {
    font-size: 16px;
    padding: 10px 12px;
    border-radius: 8px;
    border: 1px solid #bbbbbb;
    background: white;
    cursor: pointer;
  }

  button:hover {
    background: #eeeeee;
  }

  .primary {
    background: var(--primary);
    color: var(--primary-text);
    border-color: var(--primary);
  }

  .primary:hover {
    background: #444444;
  }

  .layout {
    display: grid;
    grid-template-columns: 460px 1fr;
    gap: 16px;
    padding: 16px;
  }

  .status-layout, .aim-layout {
    display: grid;
    grid-template-columns: 360px 1fr;
    gap: 16px;
    padding: 16px;
  }

  .status-layout {
    grid-template-columns: 1fr;
    max-width: 900px;
  }

  .panel {
    background: var(--panel);
    padding: 14px;
    border-radius: 12px;
  }

  .panel-actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 12px;
  }

  .event {
    padding: 12px;
    border-bottom: 1px solid var(--border);
  }

  .clip {
    display: flex;
    gap: 10px;
    margin-top: 8px;
    padding: 8px;
    background: #f4f4f4;
    border-radius: 10px;
  }

  .clip img {
    width: 120px;
    height: 68px;
    object-fit: cover;
    background: #000000;
    border-radius: 6px;
    cursor: pointer;
  }

  video, .preview-image {
    width: 100%;
    max-height: 75vh;
    background: #000000;
    border-radius: 10px;
  }

  .player-controls {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 10px;
  }

  .node-row {
    border-bottom: 1px solid var(--border);
    padding: 8px 0;
  }

  .muted {
    color: var(--muted);
  }

  .online { color: green; font-weight: bold; }
  .offline { color: red; font-weight: bold; }

  @media (max-width: 800px) {
    .layout, .status-layout, .aim-layout {
      display: flex;
      flex-direction: column;
      padding: 10px;
    }

    .player-panel {
      order: -1;
    }

    .top-actions {
      flex-direction: column;
      align-items: stretch;
    }

    button, select {
      width: 100%;
      font-size: 18px;
      padding: 12px;
    }

    .player-controls {
      display: grid;
      grid-template-columns: 1fr 1fr;
    }

    .clip img {
      width: 110px;
      height: 62px;
    }
  }
</style>
"""

INDEX_HTML = """<!doctype html>
<html>
<head>
  <title>Video Events</title>
  {{COMMON_CSS}}
</head>
<body>
<header>
  <h1>Video Events</h1>
  <div class="top-actions">
    <a href="/status"><button>System Status</button></a>
    <a href="/aim"><button>Camera Aim</button></a>
    <span id="triggerStatus"></span>
  </div>
</header>

<div class="layout">
  <div>
    <div class="panel">
      <h2>Nodes</h2>
      <div class="panel-actions">
        <button class="primary" onclick="triggerAllNodes()">Trigger All Nodes</button>
      </div>
      <div id="nodes">Loading...</div>
    </div>

    <br>

    <div class="panel">
      <h2>Events</h2>
      <select id="nodeFilter" onchange="loadVideos(true)">
        <option value="">All nodes</option>
      </select>
      <button onclick="loadVideos()">Refresh</button>
      <div id="events"></div>
    </div>
  </div>

  <div class="panel player-panel">
    <h2 id="playerTitle">Player</h2>
    <video id="player" controls preload="metadata"></video>

    <div class="player-controls">
      <button onclick="slowMotion()">Slow motion</button>
      <button onclick="normalSpeed()">Normal speed</button>
      <button onclick="stepBack()">Frame back</button>
      <button onclick="stepForward()">Frame forward</button>
    </div>
  </div>
</div>

<script>
let offset = 0;
const limit = 100;
let knownNodes = new Set();
let nodeAliases = {};

function fmtSize(bytes) {
  return Math.round(bytes / 1024 / 1024 * 10) / 10 + " MB";
}

function fmtLocalTime(isoString) {
  const date = new Date(isoString);

  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  });
}

async function loadNodes() {
  const res = await fetch('/api/nodes');
  const nodes = await res.json();

  const container = document.getElementById('nodes');
  container.innerHTML = '';

  const filter = document.getElementById('nodeFilter');

  nodeAliases = {};
  for (const n of nodes) {
    nodeAliases[n.node_id] = n.alias;
    const div = document.createElement('div');
    div.className = 'node-row';
    div.innerHTML = `${n.alias}: <span class="${n.status}">${n.status}</span><br><small>Last seen: ${fmtLocalTime(n.last_seen)}</small>`;
    container.appendChild(div);

    if (!knownNodes.has(n.node_id)) {
      knownNodes.add(n.node_id);
      const opt = document.createElement('option');
      opt.value = n.node_id;
      opt.textContent = n.alias;
      filter.appendChild(opt);
    }
  }
}

async function loadVideos(resetOffset = false) {
  if (resetOffset) offset = 0;

  const node = document.getElementById('nodeFilter').value;
  let url = `/api/events?limit=${limit}&offset=${offset}`;
  if (node) url += '&node_id=' + encodeURIComponent(node);

  const res = await fetch(url);
  const data = await res.json();
  const events = data.items;

  const container = document.getElementById('events');
  container.innerHTML = '';

  const info = document.createElement('div');
  info.innerHTML = `<small class="muted">Showing ${offset + 1}-${Math.min(offset + limit, data.total)} of ${data.total} trigger events</small>`;
  container.appendChild(info);

  for (const ev of events) {
    const eventDiv = document.createElement('div');
    eventDiv.className = 'event';

    const localTime = fmtLocalTime(ev.created_at);

    let clipsHtml = '';

    for (const clip of ev.clips) {
      clipsHtml += `
        <div class="clip">
          <img
            src="/api/thumbnail/${clip.id}"
            onclick="playClip(event, '${clip.id}', '${clip.node_id}', '${localTime}')"
          >

          <div>
            <b>${nodeAliases[clip.node_id] || clip.node_id}</b><br>
            <small class="muted">
              ${clip.duration_seconds}s |
              ${fmtSize(clip.filesize_bytes)}
            </small><br>
            <button onclick="playClip(event, '${clip.id}', '${clip.node_id}', '${localTime}')">
              Play
            </button>
          </div>
        </div>
      `;
    }

    eventDiv.innerHTML = `
      <div>
        <b>${localTime}</b><br>
        <small class="muted">${ev.clips.length} clip(s) from this trigger</small><br>
        <button onclick="deleteEvent(event, '${ev.event_id}')">
          Delete trigger
        </button>
      </div>

      <div style="margin-top:8px;">
        ${clipsHtml}
      </div>
    `;

    container.appendChild(eventDiv);
  }

  const nav = document.createElement('div');
  nav.style.marginTop = '10px';

  const prev = document.createElement('button');
  prev.innerText = 'Previous';
  prev.disabled = offset === 0;
  prev.onclick = () => {
    offset = Math.max(0, offset - limit);
    loadVideos();
  };

  const next = document.createElement('button');
  next.innerText = 'Next';
  next.disabled = offset + limit >= data.total;
  next.onclick = () => {
    offset += limit;
    loadVideos();
  };

  nav.appendChild(prev);
  nav.appendChild(next);
  container.appendChild(nav);
}

function playClip(e, videoId, nodeId, localTime) {
  e.stopPropagation();

  const player = document.getElementById('player');
  player.src = `/api/video/${videoId}`;

  document.getElementById('playerTitle').innerText =
    `${nodeAliases[nodeId] || nodeId} — ${localTime}`;

  player.play();
}

async function triggerAllNodes() {
  const status = document.getElementById('triggerStatus');

  status.innerText = 'Triggering...';

  try {
    const res = await fetch('/api/trigger-all', {
      method: 'POST'
    });

    const data = await res.json();

    const ok = data.results.filter(r => r.ok).length;
    const total = data.results.length;

    status.innerText = `Triggered ${ok}/${total} nodes`;

    await loadVideos(true);
    setTimeout(() => loadVideos(true), 3000);
    setTimeout(() => loadVideos(true), 6000);
    setTimeout(() => loadVideos(true), 9000);

    setTimeout(() => {
      status.innerText = '';
    }, 5000);

  } catch (e) {
    status.innerText = 'Trigger failed';
  }
}

async function deleteEvent(e, eventId) {
  e.stopPropagation();

  if (!confirm('Delete all clips from this trigger?')) {
    return;
  }

  const res = await fetch(`/api/events/${eventId}`, {
    method: 'DELETE'
  });

  const data = await res.json();

  alert(`Deleted ${data.deleted} clip(s)`);

  await loadVideos(true);
}

const FPS = 30;

function slowMotion() {
  const player = document.getElementById('player');
  player.playbackRate = 0.25;
}

function normalSpeed() {
  const player = document.getElementById('player');
  player.playbackRate = 1.0;
}

function stepForward() {
  const player = document.getElementById('player');
  player.pause();
  player.currentTime += 1 / FPS;
}

function stepBack() {
  const player = document.getElementById('player');
  player.pause();
  player.currentTime = Math.max(0, player.currentTime - 1 / FPS);
}

async function refreshAll() {
  await loadNodes();
  await loadVideos(false);
}

refreshAll();
setInterval(refreshAll, 10000);
</script>
</body>
</html>
""".replace("{{COMMON_CSS}}", COMMON_CSS)

STATUS_HTML = """<!doctype html>
<html>
<head>
  <title>System Status</title>
  {{COMMON_CSS}}
</head>
<body>
<header>
  <h1>System Status</h1>
  <div class="top-actions">
    <a href="/"><button>Video Events</button></a>
    <a href="/aim"><button>Camera Aim</button></a>
  </div>
</header>

<div class="status-layout">
  <div class="panel">
    <h2>Storage</h2>
    <div id="storage">Loading...</div>
  </div>

  <div class="panel">
    <h2>Nodes</h2>
    <div id="nodes">Loading...</div>
  </div>

  <div class="panel">
    <h2>Videos</h2>
    <div id="videos">Loading...</div>
  </div>
</div>

<script>
let editingAlias = false;

function fmtSize(bytes) {
  return Math.round(bytes / 1024 / 1024 / 1024 * 10) / 10 + " GB";
}

function fmtLocalTime(isoString) {
  if (!isoString) return "None";
  return new Date(isoString).toLocaleString();
}

async function loadStatus() {
  const res = await fetch('/api/status');
  const s = await res.json();

  const usedPercent = Math.round(s.storage.used_bytes / s.storage.total_bytes * 1000) / 10;

  document.getElementById('storage').innerHTML = `
    Used: <b>${fmtSize(s.storage.used_bytes)}</b><br>
    Free: <b>${fmtSize(s.storage.free_bytes)}</b><br>
    Total: <b>${fmtSize(s.storage.total_bytes)}</b><br>
    Used percent: <b>${usedPercent}%</b>
  `;

  document.getElementById('nodes').innerHTML = s.nodes.map(n => `
    <div>
      <b>${n.alias}</b>
      <span class="${n.status}">${n.status}</span><br>
      Real ID: <small>${n.node_id}</small><br>
      IP: ${n.ip_address || "-"}<br>
      Last seen: ${fmtLocalTime(n.last_seen)}<br>
  
      <input
        id="alias-${n.node_id}"
        value="${n.alias}"
        onfocus="editingAlias = true"
        onblur="editingAlias = false"
        style="font-size:16px; padding:8px; margin-top:6px;"
      >      

      <button onclick="saveAlias('${n.node_id}')">
        Save name
      </button>

      <button
        onclick="deleteNode('${n.node_id}')"
        ${n.status === "online" ? "disabled" : ""}
      >
        Delete node
      </button>

    </div><br>
  `).join('');  
  
  document.getElementById('videos').innerHTML = `
    Stored clips: <b>${s.video_count}</b><br>
    Latest upload: <b>${fmtLocalTime(s.latest_upload)}</b>
  `;
}

async function saveAlias(nodeId) {
  const input = document.getElementById(`alias-${nodeId}`);
  const alias = input.value.trim();

  if (!alias) {
    alert("Name may not be empty");
    return;
  }

  editingAlias = false;

  const res = await fetch(`/api/nodes/${nodeId}/alias`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ alias }),
  });

  if (!res.ok) {
    alert("Failed to save name");
    return;
  }

  await loadStatus();
}

async function deleteNode(nodeId) {
  if (!confirm("Delete this offline node?")) {
    return;
  }

  const res = await fetch(`/api/nodes/${nodeId}`, {
    method: "DELETE"
  });

  if (!res.ok) {
    alert("Failed to delete node");
    return;
  }

  await loadStatus();
}

loadStatus();
setInterval(() => {
  if (!editingAlias) {
    loadStatus();
  }
}, 10000);
</script>
</body>
</html>
""".replace("{{COMMON_CSS}}", COMMON_CSS)

AIM_HTML = """<!doctype html>
<html>
<head>
  <title>Camera Aim</title>
  {{COMMON_CSS}}
</head>
<body>
<header>
  <h1>Camera Aim</h1>
  <div class="top-actions">
    <a href="/"><button>Video Events</button></a>
    <a href="/status"><button>System Status</button></a>
  </div>
</header>

<div class="aim-layout">
  <div class="panel">
    <h2>Nodes</h2>
    <div id="nodes">Loading...</div>
  </div>

  <div class="panel">
    <h2 id="title">Preview</h2>
    <div id="preview">Select a camera and start preview.</div>
  </div>
</div>

<script>
async function loadNodes() {
  const res = await fetch('/api/nodes');
  const nodes = await res.json();

  const container = document.getElementById('nodes');
  container.innerHTML = '';
  
  for (const n of nodes) {
    const div = document.createElement('div');
    div.className = 'node-row';

    div.innerHTML = `
      <b>${n.alias}</b><br>
      <span class="${n.status}">${n.status}</span><br>
      <small class="muted">${n.ip_address || '-'}</small><br>
      <button onclick="startPreview('${n.node_id}', '${n.ip_address}')">Start preview</button>
      <button onclick="stopPreview('${n.node_id}')">Stop preview</button>
    `;

    container.appendChild(div);
  }
}

async function startPreview(nodeId, ip) {
  document.getElementById('title').innerText = `Preview — ${nodeId}`;

  await fetch(`/api/nodes/${nodeId}/preview/start`, { method: 'POST' });

  document.getElementById('preview').innerHTML = `
    <img class="preview-image" src="http://${ip}:8080/preview?ts=${Date.now()}">
  `;
}

async function stopPreview(nodeId) {
  await fetch(`/api/nodes/${nodeId}/preview/stop`, { method: 'POST' });

  document.getElementById('preview').innerHTML =
    'Preview stopped. Recording mode restarted.';

  document.getElementById('title').innerText = 'Preview';
}

loadNodes();
setInterval(loadNodes, 10000);
</script>
</body>
</html>
""".replace("{{COMMON_CSS}}", COMMON_CSS)
