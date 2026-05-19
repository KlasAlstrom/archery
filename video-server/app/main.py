import os
import uuid
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import create_engine, Column, String, DateTime, Integer, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker


DATABASE_URL = os.environ["DATABASE_URL"]
VIDEO_STORAGE = Path(os.environ["VIDEO_STORAGE"])
NODE_TOKEN = os.environ["NODE_TOKEN"]

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


class Node(Base):
    __tablename__ = "nodes"

    node_id = Column(String, primary_key=True)
    last_seen = Column(DateTime, nullable=False)
    status = Column(String, nullable=False)


@app.on_event("startup")
def startup():
    VIDEO_STORAGE.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


def check_token(auth_header: str | None):
    if auth_header != f"Bearer {NODE_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.post("/api/upload")
async def upload_video(
    video: UploadFile = File(...),
    node_id: str = Form(...),
    event_id: str = Form(...),
    timestamp: str = Form(...),
    duration: int = Form(...),
    authorization: str | None = Header(default=None),
):
    check_token(authorization)

    video_id = str(uuid.uuid4())

    try:
        created_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except Exception:
        created_at = datetime.now(timezone.utc)

    day_dir = VIDEO_STORAGE / node_id / created_at.strftime("%Y") / created_at.strftime("%m") / created_at.strftime("%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    output_path = day_dir / f"{video_id}.mp4"

    with output_path.open("wb") as f:
        shutil.copyfileobj(video.file, f)

    filesize = output_path.stat().st_size

    db = SessionLocal()
    try:
        db.add(Video(
            id=video_id,
            node_id=node_id,
            event_id=event_id,
            created_at=created_at,
            uploaded_at=datetime.now(timezone.utc),
            duration_seconds=duration,
            filesize_bytes=filesize,
            storage_path=str(output_path),
        ))
        db.commit()
    finally:
        db.close()

    return {
        "status": "ok",
        "video_id": video_id,
        "filesize": filesize,
    }


@app.get("/api/videos")
def list_videos(
    limit: int = 100,
    offset: int = 0,
    node_id: str | None = None,
):
    db = SessionLocal()
    try:
        q = db.query(Video).order_by(Video.created_at.desc())

        if node_id:
            q = q.filter(Video.node_id == node_id)

        total = q.count()
        rows = q.offset(offset).limit(limit).all()

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "id": v.id,
                    "node_id": v.node_id,
                    "event_id": v.event_id,
                    "created_at": v.created_at.isoformat(),
                    "duration_seconds": v.duration_seconds,
                    "filesize_bytes": v.filesize_bytes,
                }
                for v in rows
            ],
        }
    finally:
        db.close()


@app.get("/api/video/{video_id}")
def get_video(video_id: str):
    db = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise HTTPException(status_code=404, detail="Video not found")

        path = Path(video.storage_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="File missing")

        return FileResponse(path, media_type="video/mp4", filename=f"{video_id}.mp4")
    finally:
        db.close()


@app.post("/api/heartbeat")
def heartbeat(
    node_id: str = Form(...),
    status: str = Form("ok"),
    authorization: str | None = Header(default=None),
):
    check_token(authorization)

    db = SessionLocal()
    try:
        node = db.query(Node).filter(Node.node_id == node_id).first()
        if not node:
            node = Node(
                node_id=node_id,
                last_seen=datetime.now(timezone.utc),
                status=status,
            )
            db.add(node)
        else:
            node.last_seen = datetime.now(timezone.utc)
            node.status = status

        db.commit()
    finally:
        db.close()

    return {"status": "ok"}


@app.get("/api/nodes")
def list_nodes():
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        rows = db.query(Node).order_by(Node.node_id).all()

        result = []

        for n in rows:
            last_seen = n.last_seen

            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)

            result.append({
                "node_id": n.node_id,
                "status": "online" if now - last_seen < timedelta(minutes=2) else "offline",
                "last_seen": last_seen.isoformat(),
            })

        return result
    finally:
        db.close()

@app.post("/api/cleanup")
def cleanup_old_videos(authorization: str | None = Header(default=None)):
    check_token(authorization)

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    db = SessionLocal()
    deleted = 0

    try:
        rows = db.query(Video).filter(Video.created_at < cutoff).all()

        for video in rows:
            path = Path(video.storage_path)
            if path.exists():
                path.unlink()

            db.delete(video)
            deleted += 1

        db.commit()
    finally:
        db.close()

    return {
        "status": "ok",
        "deleted": deleted,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<html>
<head>
  <title>Video Events</title>
  <style>
    body { font-family: sans-serif; margin: 20px; background: #f6f6f6; }
    h1 { margin-bottom: 10px; }
    .layout { display: grid; grid-template-columns: 460px 1fr; gap: 20px; }
    .panel { background: white; padding: 14px; border-radius: 8px; }
    .event { padding: 10px; border-bottom: 1px solid #ddd; cursor: pointer; }
    .event:hover { background: #eee; }
    .event small { color: #555; }
    video { width: 100%; max-height: 75vh; background: black; }
    select, button { padding: 6px; margin-right: 6px; }
    .online { color: green; font-weight: bold; }
    .offline { color: red; font-weight: bold; }
  </style>
</head>
<body>
  <h1>Video Events</h1>

  <div class="layout">
    <div>
      <div class="panel">
        <h2>Nodes</h2>
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

    <div class="panel">
      <h2 id="playerTitle">Player</h2>
      <video id="player" controls preload="metadata"></video>
    </div>
  </div>

<script>
let offset = 0;
const limit = 100;
let knownNodes = new Set();

function fmtSize(bytes) {
  return Math.round(bytes / 1024 / 1024 * 10) / 10 + " MB";
}

async function loadNodes() {
  const res = await fetch('/api/nodes');
  const nodes = await res.json();

  const container = document.getElementById('nodes');
  container.innerHTML = '';

  const filter = document.getElementById('nodeFilter');

  for (const n of nodes) {
    const div = document.createElement('div');
    div.innerHTML = `${n.node_id}: <span class="${n.status}">${n.status}</span><br><small>Last seen: ${n.last_seen}</small>`;
    container.appendChild(div);

    if (!knownNodes.has(n.node_id)) {
      knownNodes.add(n.node_id);
      const opt = document.createElement('option');
      opt.value = n.node_id;
      opt.textContent = n.node_id;
      filter.appendChild(opt);
    }
  }
}

async function loadVideos(resetOffset = false) {
  if (resetOffset) offset = 0;

  const node = document.getElementById('nodeFilter').value;
  let url = `/api/videos?limit=${limit}&offset=${offset}`;
  if (node) url += '&node_id=' + encodeURIComponent(node);

  const res = await fetch(url);
  const data = await res.json();
  const videos = data.items;

  const container = document.getElementById('events');
  container.innerHTML = '';

  const info = document.createElement('div');
  info.innerHTML = `<small>Showing ${offset + 1}-${Math.min(offset + limit, data.total)} of ${data.total}</small>`;
  container.appendChild(info);

  for (const v of videos) {
    const div = document.createElement('div');
    div.className = 'event';

    const date = new Date(v.created_at);

    div.innerHTML = `
      <b>${date.toLocaleString()}</b><br>
      <small>${v.node_id} | ${v.duration_seconds}s | ${fmtSize(v.filesize_bytes)}</small>
    `;

    div.onclick = () => {
      document.getElementById('player').src = `/api/video/${v.id}`;
      document.getElementById('playerTitle').innerText =
        `${v.node_id} — ${date.toLocaleString()}`;
      document.getElementById('player').play();
    };

    container.appendChild(div);
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

async function refreshAll() {
  await loadNodes();
  await loadVideos(false);
}

refreshAll();
setInterval(refreshAll, 10000);
</script>
</body>
</html>
"""

@app.get("/api/health")
def health():
    return {"status": "ok"}
