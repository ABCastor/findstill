from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from PIL import Image, ImageOps
from .config import MODEL, REVISION, settings
from .store import Store


def fingerprint(row):
    path = row.get("path")
    if not path or not Path(path).is_file(): return None
    stat = Path(path).stat()
    files = [(p, Path(p).stat().st_size, Path(p).stat().st_mtime_ns) for p in (row.get("paths") or [path]) if Path(p).is_file()]
    return hashlib.sha256(json.dumps([files, row.get("modified"), MODEL, REVISION]).encode()).hexdigest()


def read_preview(row):
    for path in row.get("paths") or [row["path"]]:
        try:
            with Image.open(path) as image:
                preview = ImageOps.exif_transpose(image).convert("RGB")
            row["path"] = path
            return preview
        except (OSError, ValueError):
            continue
    raise ValueError("No decodable local image preview")


def synchronize(store, rows, generation):
    """Immediately remove inaccessible/hidden/deleted items and invalidate changed vectors."""
    pending = []
    with store.connect() as db:
        for row in rows:
            fp = fingerprint(row)
            previous = db.execute("SELECT fingerprint,embedding,metadata FROM assets WHERE uuid=?", (row["uuid"],)).fetchone()
            unchanged = previous and previous["fingerprint"] == fp and previous["embedding"] is not None
            if unchanged:
                saved = json.loads(previous["metadata"]).get("path")
                if row.get("paths") and saved in row["paths"]: row["path"] = saved
                db.execute("UPDATE assets SET metadata=?,generation=? WHERE uuid=?", (json.dumps(row), generation, row["uuid"]))
            else:
                db.execute("INSERT OR REPLACE INTO assets VALUES (?,?,?,NULL,?,?)", (row["uuid"], json.dumps(row), fp, None if fp else "No local derivative", generation))
                if fp: pending.append(row)
        db.execute("DELETE FROM assets WHERE generation<>?", (generation,))
    return pending


def index(store=None, batch_size=32, limit=None, encoder=None):
    store = store or Store()
    # A process-owned advisory lock survives crashes without stale lockfiles.
    import fcntl
    lock = (store.home / "index.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Another indexing process is running")
    started = time.monotonic()
    store.set_state(indexing=True, error=None, started_at=datetime.now(timezone.utc).isoformat())
    try:
        config = settings()
        interpreter = config.get("photos_python", str(Path.home() / ".local/share/uv/tools/osxphotos/bin/python"))
        command = [interpreter, str(Path(__file__).with_name("photos_bridge.py")), str(store.home / "snapshot.json")]
        if config.get("photos_library"):
            command.append(config["photos_library"])
        subprocess.run(command, check=True, timeout=180)
        snapshot = json.loads((store.home / "snapshot.json").read_text())
        rows = snapshot["assets"]
        store.set_state(library_total=len(rows), excluded=snapshot["excluded"], model=MODEL, revision=REVISION, provider_version=snapshot["version"], preview_only=True)
        pending = synchronize(store, rows, str(uuid.uuid4()))
        if limit is not None: pending = pending[:limit]
        if pending:
            if encoder is None:
                from .model import Encoder
                encoder = Encoder(allow_download=True)
            store.set_state(device=encoder.device)
        for offset in range(0, len(pending), batch_size):
            images, valid = [], []
            for row in pending[offset:offset + batch_size]:
                try:
                    images.append(read_preview(row))
                    valid.append(row)
                except (OSError, ValueError) as error:
                    with store.connect() as db:
                        db.execute("UPDATE assets SET error=? WHERE uuid=?", (type(error).__name__ + ": preview decode failed", row["uuid"]))
            if images:
                vectors = encoder.images(images)
                with store.connect() as db:
                    db.executemany("UPDATE assets SET embedding=?,metadata=?,error=NULL WHERE uuid=?", [(v.astype("float32").tobytes(), json.dumps(r), r["uuid"]) for v, r in zip(vectors, valid, strict=True)])
            store.set_state(last_batch_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=round(time.monotonic()-started, 2))
            print(json.dumps({"processed": min(offset+batch_size, len(pending)), "pending":len(pending), "seconds":round(time.monotonic()-started, 1)}), flush=True)
        store.set_state(indexed_at=datetime.now(timezone.utc).isoformat(), indexing=False, elapsed_seconds=round(time.monotonic()-started, 2))
    except BaseException as error:
        store.set_state(indexing=False, error=type(error).__name__ + ": indexing interrupted or failed; inspect local log")
        raise
    finally:
        lock.close()
