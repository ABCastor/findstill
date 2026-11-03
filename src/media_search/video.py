"""Search inside videos: sampled frames (seen), speech (said) and on-screen text.

One video at a time: fetch an original into a private temporary folder, sample a frame
every few seconds, drop near-duplicate frames, embed the kept frames, read their text,
transcribe the audio on device, store everything in a private index beside the photo
index, then delete the temporary folder. Originals are never kept. Transcripts and
on-screen text live only in that local index (owner-only permissions).
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time

import numpy as np
from PIL import Image

from . import native
from .config import MODEL, REVISION, settings

PIPELINE = "video-v1"
DEFAULT_LOCALES = ("it-IT", "en-US")
DEFAULT_INTERVAL = 2.0
DEFAULT_DUPLICATE = 0.95
FRAME_EDGE = 1280
THUMB_EDGE = 480
MAX_ATTEMPTS = 3
KINDS = ("seen", "said", "on_screen")


class VideoError(RuntimeError):
    """A video could not be fetched or analysed; no media content is included."""


class LowDisk(RuntimeError):
    """Free space is below the configured floor; indexing pauses without failing a video."""


# ---------------------------------------------------------------- pure decisions

def video_config(config=None):
    config = settings() if config is None else config
    locales = config.get("video_locales") or list(DEFAULT_LOCALES)
    if isinstance(locales, str):
        locales = [p.strip() for p in locales.split(",") if p.strip()]
    return {
        "locales": [str(l) for l in locales],
        "interval": float(config.get("video_frame_interval", DEFAULT_INTERVAL)),
        "duplicate": float(config.get("video_duplicate_similarity", DEFAULT_DUPLICATE)),
        "min_free_gb": float(config.get("video_min_free_gb", 10)),
        "fetcher": config.get("video_fetcher"),
    }


def fingerprint(row, options):
    """Changes when the video, its edit, the model or the analysis settings change."""
    return hashlib.sha256(json.dumps([PIPELINE, row["uuid"], row.get("modified"), row.get("original_bytes"),
                                      row.get("duration"), MODEL, REVISION, options["locales"],
                                      options["interval"], options["duplicate"]]).encode()).hexdigest()


def dedupe(vectors, threshold=DEFAULT_DUPLICATE):
    """Keep a frame unless it is a near-duplicate of the last kept frame (cosine >= threshold)."""
    kept = []
    for index, vector in enumerate(vectors):
        if not kept or float(np.dot(vectors[kept[-1]], vector)) < threshold:
            kept.append(index)
    return kept


def _language(locale):
    return str(locale or "").replace("_", "-").split("-")[0].lower()


def choose_transcript(candidates):
    """Pick the transcript whose text the language recogniser agrees is in its locale's language.

    A wrong-locale pass produces fluent-looking text in the wrong language, often with high
    recogniser confidence, so agreement decides; confidence and length only break ties.
    Returns None rather than guess when no candidate agrees (garbage is worse than nothing).
    """
    viable = [c for c in candidates if not c.get("error") and c.get("locale") and c.get("segments")]
    def own(c):
        return float((c.get("language") or {}).get("hypotheses", {}).get(_language(c["locale"]), 0.0))
    def quality(c):
        return (own(c) * float(c.get("mean_confidence") or 0.5), c.get("words") or 0)
    agreeing = [c for c in viable if (c.get("language") or {}).get("dominant") == _language(c["locale"])]
    if agreeing:
        return max(agreeing, key=quality)
    # Two or three words are too short for language identification; accept only a confident one.
    short = [c for c in viable if (c.get("words") or 0) <= 3 and float(c.get("mean_confidence") or 0) >= 0.85]
    return max(short, key=lambda c: float(c.get("mean_confidence") or 0)) if short else None


def ocr_segments(pages, times, min_confidence=0.4):
    """Merge identical consecutive on-screen text into one segment spanning its frames."""
    segments = []
    for (frame_index, time_seconds), page in zip(times, pages, strict=True):
        lines = [l["text"].strip() for l in page.get("lines", []) if float(l.get("confidence", 0)) >= min_confidence
                 and len(l.get("text", "").strip()) >= 2]
        text = " · ".join(dict.fromkeys(lines))
        if not text:
            continue
        if segments and segments[-1]["text"] == text and segments[-1]["last_frame"] == frame_index - 1:
            segments[-1].update(end=time_seconds, last_frame=frame_index)
        else:
            segments.append({"kind": "on_screen", "start": time_seconds, "end": time_seconds, "text": text,
                             "frame": frame_index, "last_frame": frame_index})
    return segments


def nearest_frame(times, seconds):
    """Index of the kept frame closest to a moment, preferring the frame already showing it."""
    if not times:
        return None
    earlier = [i for i, t in enumerate(times) if t <= seconds + 0.25]
    return earlier[-1] if earlier else 0


TOKEN = re.compile(r"\w+", re.UNICODE)


def fts_query(text):
    """Every word must appear (implicit AND); the last word also matches as a prefix."""
    tokens = TOKEN.findall(text.casefold())[:16]
    if not tokens:
        return None
    quoted = [f'"{t}"' for t in tokens]
    if len(tokens[-1]) >= 3:
        quoted[-1] += "*"
    return " ".join(quoted)


def time_label(seconds):
    seconds = max(0, int(round(seconds or 0)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


# ---------------------------------------------------------------- private index

class VideoStore:
    def __init__(self, home):
        self.home = Path(home)
        self.path = self.home / "video-index.sqlite3"
        self.frames_dir = self.home / "video-frames"
        self.work_root = self.home / "video-work"
        self.lock_path = self.home / "video.lock"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS videos (
                    uuid TEXT PRIMARY KEY, fingerprint TEXT, status TEXT NOT NULL, error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, duration REAL, sampled INTEGER, kept INTEGER,
                    language TEXT, said INTEGER, on_screen INTEGER, seconds REAL, bytes INTEGER, indexed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS frames (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL, t REAL NOT NULL,
                    embedding BLOB NOT NULL, thumb TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS frames_uuid ON frames(uuid, t);
                CREATE TABLE IF NOT EXISTS texts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL, kind TEXT NOT NULL,
                    start REAL NOT NULL, end REAL NOT NULL, frame_id INTEGER, text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS texts_uuid ON texts(uuid);
                CREATE VIRTUAL TABLE IF NOT EXISTS texts_fts USING fts5(
                    text, content='texts', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TRIGGER IF NOT EXISTS texts_ai AFTER INSERT ON texts BEGIN
                    INSERT INTO texts_fts(rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS texts_ad AFTER DELETE ON texts BEGIN
                    INSERT INTO texts_fts(texts_fts, rowid, text) VALUES ('delete', old.id, old.text);
                END;
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            """)
        for suffix in ("", "-wal", "-shm"):
            file = Path(str(self.path) + suffix)
            if file.exists():
                file.chmod(0o600)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._signature = None
        self._frames = _empty_frames()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def set_state(self, **values):
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO state VALUES (?,?)", [(k, json.dumps(v)) for k, v in values.items()])

    def running(self):
        with self.lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True

    def status(self, video_uuids):
        with self.connect() as db:
            state = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM state")}
            rows = {r["uuid"]: r for r in db.execute("SELECT uuid,status,attempts FROM videos")}
            frames = db.execute("SELECT count(*) FROM frames").fetchone()[0]
            texts = {r[0]: r[1] for r in db.execute("SELECT kind,count(*) FROM texts GROUP BY kind")}
        done = sum(1 for u in video_uuids if u in rows and rows[u]["status"] == "done")
        failed = sum(1 for u in video_uuids if u in rows and rows[u]["status"] == "failed")
        total = len(video_uuids)
        return {"total": total, "done": done, "failed": failed, "pending": total - done - failed,
                "frames": frames, "said_segments": texts.get("said", 0), "on_screen_segments": texts.get("on_screen", 0),
                "indexing": self.running(), **{k: v for k, v in state.items() if k in
                ("started_at", "finished_at", "last_video_at", "paused", "error", "average_seconds", "processed_this_run")}}

    def replace(self, uuid, fp, frames, texts, stats):
        """Atomically swap one video's frames and text; frame thumbnails are already on disk."""
        with self.connect() as db:
            db.execute("DELETE FROM texts WHERE uuid=?", (uuid,))
            db.execute("DELETE FROM frames WHERE uuid=?", (uuid,))
            ids = []
            for frame in frames:
                cursor = db.execute("INSERT INTO frames(uuid,t,embedding,thumb) VALUES (?,?,?,?)",
                                    (uuid, frame["time"], frame["vector"].astype(np.float32).tobytes(), frame["thumb"]))
                ids.append(cursor.lastrowid)
            for text in texts:
                frame_id = ids[text["frame"]] if text.get("frame") is not None and ids else None
                db.execute("INSERT INTO texts(uuid,kind,start,end,frame_id,text) VALUES (?,?,?,?,?,?)",
                           (uuid, text["kind"], text["start"], text["end"], frame_id, text["text"]))
            db.execute("""INSERT OR REPLACE INTO videos(uuid,fingerprint,status,error,attempts,duration,sampled,kept,language,
                          said,on_screen,seconds,bytes,indexed_at) VALUES (?,?,'done',NULL,0,?,?,?,?,?,?,?,?,?)""",
                       (uuid, fp, stats.get("duration"), stats.get("sampled"), len(frames), stats.get("language"),
                        sum(t["kind"] == "said" for t in texts), sum(t["kind"] == "on_screen" for t in texts),
                        stats.get("seconds"), stats.get("bytes"), datetime.now(timezone.utc).isoformat()))

    def mark_failed(self, uuid, fp, message):
        with self.connect() as db:
            previous = db.execute("SELECT fingerprint,attempts FROM videos WHERE uuid=?", (uuid,)).fetchone()
            attempts = (previous["attempts"] if previous and previous["fingerprint"] == fp else 0) + 1
            db.execute("""INSERT OR REPLACE INTO videos(uuid,fingerprint,status,error,attempts,indexed_at)
                          VALUES (?,?,'failed',?,?,?)""", (uuid, fp, message[:200], attempts, datetime.now(timezone.utc).isoformat()))

    def pending(self, rows, options, retry_failed=False):
        with self.connect() as db:
            known = {r["uuid"]: r for r in db.execute("SELECT uuid,fingerprint,status,attempts FROM videos")}
        todo = []
        for row in rows:
            fp = fingerprint(row, options)
            previous = known.get(row["uuid"])
            if previous and previous["fingerprint"] == fp:
                if previous["status"] == "done":
                    continue
                if previous["status"] == "failed" and previous["attempts"] >= MAX_ATTEMPTS and not retry_failed:
                    continue
            todo.append((row, fp))
        return todo

    def prune(self, keep_uuids):
        """Remove index entries and frame thumbnails for videos no longer in the library."""
        keep = set(keep_uuids)
        with self.connect() as db:
            gone = [r[0] for r in db.execute("SELECT uuid FROM videos") if r[0] not in keep]
            gone += [r[0] for r in db.execute("SELECT DISTINCT uuid FROM frames") if r[0] not in keep]
            for uuid in set(gone):
                db.execute("DELETE FROM texts WHERE uuid=?", (uuid,))
                db.execute("DELETE FROM frames WHERE uuid=?", (uuid,))
                db.execute("DELETE FROM videos WHERE uuid=?", (uuid,))
            done = {r[0] for r in db.execute("SELECT uuid FROM videos WHERE status='done'")}
        if self.frames_dir.is_dir():
            for folder in self.frames_dir.iterdir():
                if folder.is_dir() and folder.name not in done:
                    shutil.rmtree(folder, ignore_errors=True)
        return len(set(gone))

    def frame_path(self, uuid, frame_id):
        with self.connect() as db:
            row = db.execute("SELECT thumb FROM frames WHERE id=? AND uuid=?", (frame_id, uuid)).fetchone()
        if not row:
            return None
        path = (self.frames_dir / uuid / row["thumb"]).resolve()
        return path if path.parent == (self.frames_dir / uuid).resolve() and path.is_file() else None

    # -------------------------------------------------------------- search side

    def signature(self):
        signature = []
        for file in (self.path, Path(str(self.path) + "-wal")):
            try:
                stat = file.stat()
                signature.append((stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                signature.append(None)
        return tuple(signature)

    def frames_catalog(self):
        """Frame vectors grouped by video, kept in memory; new rows are appended incrementally."""
        signature = self.signature()
        with self._lock:
            if signature == self._signature:
                return self._frames
            current = self._frames
            with self.connect() as db:
                count, highest = db.execute("SELECT count(*), coalesce(max(id),0) FROM frames").fetchone()
                fresh = db.execute("SELECT id,uuid,t,embedding FROM frames WHERE id>? ORDER BY uuid,t", (current["max_id"],)).fetchall() \
                    if current["count"] and highest >= current["max_id"] else None
                appendable = fresh is not None and current["count"] + len(fresh) == count and \
                    not ({r["uuid"] for r in fresh} & set(current["uuids"]))
                records = fresh if appendable else db.execute("SELECT id,uuid,t,embedding FROM frames ORDER BY uuid,t").fetchall()
            self._frames = _extend_frames(current if appendable else _empty_frames(), records)
            self._signature = signature
            return self._frames

    def text_hits(self, query, limit=300):
        expression = fts_query(query)
        if not expression:
            return []
        try:
            return [dict(r) for r in self._reader().execute(
                """SELECT texts.id, texts.uuid, texts.kind, texts.start, texts.end, texts.frame_id, texts.text,
                          bm25(texts_fts) AS rank FROM texts_fts JOIN texts ON texts.id = texts_fts.rowid
                   WHERE texts_fts MATCH ? ORDER BY rank LIMIT ?""", (expression, limit))]
        except sqlite3.OperationalError:
            return []

    def _reader(self):
        """One read-only connection per serving thread; opening one per query cost milliseconds."""
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
            db.row_factory = sqlite3.Row
            self._local.db = db
        return db

    def frame_rows(self, ids):
        if not ids:
            return {}
        with self.connect() as db:
            marks = ",".join("?" * len(ids))
            return {r["id"]: dict(r) for r in db.execute(f"SELECT id,uuid,t FROM frames WHERE id IN ({marks})", list(ids))}


def _empty_frames():
    return {"count": 0, "max_id": 0, "ids": np.empty(0, dtype=np.int64), "times": np.empty(0, dtype=np.float32),
            "matrix": None, "uuids": [], "starts": np.empty(0, dtype=np.int64)}


def _extend_frames(base, records):
    if not records:
        return base
    ids = np.array([r["id"] for r in records], dtype=np.int64)
    times = np.array([r["t"] for r in records], dtype=np.float32)
    matrix = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in records])
    uuids, starts = list(base["uuids"]), list(base["starts"])
    offset = base["count"]
    previous = None
    for index, record in enumerate(records):
        if record["uuid"] != previous:
            uuids.append(record["uuid"])
            starts.append(offset + index)
            previous = record["uuid"]
    return {"count": offset + len(records), "max_id": max(int(ids.max()), base["max_id"]),
            "ids": np.concatenate([base["ids"], ids]), "times": np.concatenate([base["times"], times]),
            "matrix": matrix if base["matrix"] is None else np.concatenate([base["matrix"], matrix]),
            "uuids": uuids, "starts": np.array(starts, dtype=np.int64)}


# ---------------------------------------------------------------- native analysis

class Analyzer:
    """Frames, speech and on-screen text through the bundled Swift helper (macOS 26+)."""

    def __init__(self, binary=None):
        self._binary = binary

    @property
    def binary(self):
        if self._binary is None:
            self._binary = native.helper("video", "26.0", extra_flags=("-swift-version", "5"), error=VideoError)
        return self._binary

    def _json(self, arguments, timeout):
        result = native.run([str(self.binary), *arguments], timeout=timeout, error=VideoError)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise VideoError("Video helper returned invalid JSON") from exc
        if data.get("schema_version") != 1:
            raise VideoError("Video helper returned an unsupported result")
        return data

    def frames(self, path, out, interval, duration=None):
        out.mkdir(mode=0o700)
        return self._json(["frames", str(path), str(out), str(interval), str(FRAME_EDGE)], 120 + 2 * (duration or 600))

    def transcribe(self, path, work, locales, duration=None):
        return self._json(["transcribe", str(path), str(work), *locales], 180 + 3 * len(locales) * (duration or 600))

    def ocr(self, paths, locales):
        pages = []
        for offset in range(0, len(paths), 40):
            chunk = paths[offset:offset + 40]
            pages += self._json(["ocr", ",".join(locales), *map(str, chunk)], 60 + 5 * len(chunk))["pages"]
        return pages


# ---------------------------------------------------------------- indexing

IO_POLICIES = {"standard": 1, "utility": 2, "throttle": 3}  # IOPOL_STANDARD, IOPOL_UTILITY, IOPOL_THROTTLE


def background_priority(io_policy=None):
    """Lower CPU and disk priority for this process and its native children.

    Disk throttling (even the utility tier) made a 12-second clip take minutes on a busy
    Mac, because every stage reads and writes files; CPU niceness alone keeps the Mac
    responsive while indexing still moves. Disk policy stays standard unless asked.
    """
    io_policy = io_policy or os.environ.get("FINDSTILL_VIDEO_IO_POLICY", "standard")
    try:
        os.nice(10)
    except OSError:
        pass
    try:
        import ctypes
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        libc.setiopolicy_np(0, 0, IO_POLICIES.get(io_policy, 2))  # IOPOL_TYPE_DISK, IOPOL_SCOPE_PROCESS
    except (OSError, AttributeError):
        pass


def free_bytes(path):
    return shutil.disk_usage(path).free


def _thumbnail(source, destination):
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp", delete=False) as temporary:
            os.fchmod(temporary.fileno(), 0o600)
            image.save(temporary, format="JPEG", quality=78, optimize=True)
        Path(temporary.name).replace(destination)


def analyse(row, fp, vstore, fetcher, analyzer, encoder, options):
    """Fetch, analyse and store one video; its temporary folder is always deleted."""
    started = time.monotonic()
    vstore.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = Path(tempfile.mkdtemp(prefix="video-", dir=vstore.work_root))
    stages = {}
    mark = time.monotonic()
    def stage(name):
        nonlocal mark
        now = time.monotonic()
        stages[name] = round(now - mark, 2)
        mark = now
    try:
        original = fetcher.fetch(row, work / "original")
        size = original.stat().st_size
        stage("fetch")
        sampled = analyzer.frames(original, work / "frames", options["interval"], row.get("duration"))
        stage("frames")
        frames = [f for f in sampled["frames"] if Path(f["path"]).is_file()]
        vectors = []
        for offset in range(0, len(frames), 16):
            images = []
            for frame in frames[offset:offset + 16]:
                with Image.open(frame["path"]) as image:
                    images.append(image.convert("RGB"))
            vectors.append(encoder.images(images))
        vectors = np.concatenate(vectors) if vectors else np.empty((0, 0), dtype=np.float32)
        stage("embed")
        keep = dedupe(vectors, options["duplicate"])
        kept = [dict(frames[i], vector=vectors[i]) for i in keep]
        times = [f["time"] for f in kept]
        texts = []
        if kept:
            pages = analyzer.ocr([f["path"] for f in kept], options["locales"])
            texts += ocr_segments(pages, list(enumerate(times)))
        stage("ocr")
        language = None
        if sampled.get("has_audio"):
            speech = analyzer.transcribe(original, work, options["locales"], sampled.get("duration"))
            chosen = choose_transcript(speech.get("candidates", []))
            if chosen:
                language = chosen["locale"]
                for segment in chosen["segments"]:
                    texts.append({"kind": "said", "start": float(segment["start"]), "end": float(segment["end"]),
                                  "text": segment["text"].strip(), "frame": nearest_frame(times, float(segment["start"]))})
        stage("speech")
        # Keep only small thumbnails of the kept frames, never the original or full frames.
        folder = vstore.frames_dir / row["uuid"]
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, mode=0o700)
        vstore.frames_dir.chmod(0o700)
        for frame in kept:
            frame["thumb"] = f"{int(round(frame['time'] * 1000)):09d}.jpg"
            _thumbnail(frame["path"], folder / frame["thumb"])
        stats = {"duration": sampled.get("duration"), "sampled": len(frames), "language": language,
                 "seconds": round(time.monotonic() - started, 2), "bytes": size, "stages": stages}
        vstore.replace(row["uuid"], fp, kept, [t for t in texts if t["text"]], stats)
        return stats
    finally:
        shutil.rmtree(work, ignore_errors=True)


def index_videos(store, *, vstore=None, fetcher=None, analyzer=None, encoder=None, limit=None, uuids=None,
                 retry_failed=False, config=None, log=print, low_priority=True):
    """Resumable and idempotent: done videos are skipped unless they or the settings changed."""
    from .fetch import default_fetcher
    config = settings() if config is None else config
    options = video_config(config)
    vstore = vstore or VideoStore(store.home)
    vstore.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = vstore.lock_path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("Another video indexing process is running")
    try:
        if low_priority:
            background_priority()
        # Leftovers of an interrupted run are this process's own temporary files.
        shutil.rmtree(vstore.work_root, ignore_errors=True)
        with store.connect() as db:
            videos = [json.loads(r[0]) for r in db.execute("SELECT metadata FROM assets")]
        videos = [r for r in videos if r.get("media_type") == "video"]
        vstore.prune([r["uuid"] for r in videos])
        if uuids:
            wanted = {u.upper() for u in uuids}
            videos = [r for r in videos if r["uuid"].upper() in wanted]
        videos.sort(key=lambda r: r.get("date") or "", reverse=True)
        todo = vstore.pending(videos, options, retry_failed)
        if limit is not None:
            todo = todo[:limit]
        fetcher = fetcher or default_fetcher(config)
        analyzer = analyzer or Analyzer()
        vstore.set_state(started_at=datetime.now(timezone.utc).isoformat(), finished_at=None, paused=None, error=None,
                         processed_this_run=0)
        log(json.dumps({"videos_pending": len(todo), "fetcher": fetcher.name}), flush=True)
        if todo and encoder is None:
            from .model import Encoder
            encoder = Encoder(allow_download=True)
        durations = []
        for number, (row, fp) in enumerate(todo, 1):
            needed = options["min_free_gb"] * 2**30 + 2 * int(row.get("original_bytes") or 0)
            if free_bytes(vstore.home) < needed:
                vstore.set_state(paused="low disk: free space below the configured floor")
                log(json.dumps({"paused": "low disk", "done_this_run": number - 1}), flush=True)
                raise LowDisk("Free space is below the video indexing floor; free space and run again")
            try:
                stats = analyse(row, fp, vstore, fetcher, analyzer, encoder, options)
                durations.append(stats["seconds"])
                outcome = {"video": number, "of": len(todo), "seconds": stats["seconds"], "frames": stats["sampled"],
                           "language": stats["language"], "stages": stats["stages"]}
            except (VideoError, RuntimeError, OSError, ValueError) as error:
                vstore.mark_failed(row["uuid"], fp, type(error).__name__ + ": " + str(error).split("\n")[0])
                outcome = {"video": number, "of": len(todo), "failed": type(error).__name__}
            vstore.set_state(last_video_at=datetime.now(timezone.utc).isoformat(), processed_this_run=number,
                             average_seconds=round(sum(durations) / len(durations), 1) if durations else None)
            log(json.dumps(outcome), flush=True)
        vstore.set_state(finished_at=datetime.now(timezone.utc).isoformat())
    except LowDisk:
        raise
    except BaseException as error:
        vstore.set_state(error=type(error).__name__ + ": video indexing interrupted or failed; inspect local log")
        raise
    finally:
        lock.close()
