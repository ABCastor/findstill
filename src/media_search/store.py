from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import numpy as np
from .config import runtime

RRF_K = 60
VISUAL_DEPTH = 300
PHRASE_WEIGHT = 1.5


def fold(value):
    """Case- and accent-insensitive comparison form."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))


class Store:
    def __init__(self, home=None):
        self.home = Path(home) if home else runtime()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.home / "index.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assets (
                    uuid TEXT PRIMARY KEY, metadata TEXT NOT NULL, fingerprint TEXT,
                    embedding BLOB, error TEXT, generation TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
                CREATE INDEX IF NOT EXISTS assets_indexed ON assets(uuid) WHERE embedding IS NOT NULL;
                CREATE INDEX IF NOT EXISTS assets_failed ON assets(uuid) WHERE error IS NOT NULL;
            """)
        self.path.chmod(0o600)
        self._cache_lock = threading.Lock()
        self._signature = None
        self._rows = []
        self._matrix = None
        self._index_of = {}
        self._owners = np.empty(0, dtype=np.int64)
        self._owners_key = None
        self._videos = None

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

    def status(self):
        with self.connect() as db:
            state = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM state")}
            state.update(indexed=db.execute("SELECT count(*) FROM assets WHERE embedding IS NOT NULL").fetchone()[0],
                         failed=db.execute("SELECT count(*) FROM assets WHERE error IS NOT NULL").fetchone()[0])
            videos = [r[0] for r in db.execute("SELECT uuid FROM assets WHERE json_extract(metadata,'$.media_type')='video'")]
        state["video"] = self.videos.status(videos)
        import fcntl
        with (self.home / "index.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state["indexing"] = False
            except BlockingIOError:
                state["indexing"] = True
        return state

    def asset(self, uuid):
        with self.connect() as db:
            row = db.execute("SELECT metadata FROM assets WHERE uuid=?", (uuid,)).fetchone()
        return json.loads(row[0]) if row else None

    def options(self):
        with self.connect() as db:
            rows = [json.loads(r[0]) for r in db.execute("SELECT metadata FROM assets")]
        return {"persons": sorted({p for r in rows for p in r["persons"]}), "places": sorted({r["place"] for r in rows if r["place"]})}

    @property
    def videos(self):
        if self._videos is None:
            from .video import VideoStore
            self._videos = VideoStore(self.home)
        return self._videos

    def search(self, vector=None, limit=60, person="", place="", date_from="", date_to="", media_type="", text=""):
        """Photos, video moments and video text in one ranking.

        Visual: each asset scores its own vector; a video scores its best sampled frame
        (or its poster). Text: SQLite FTS5 over speech and on-screen text, ranked by BM25.
        The two ranked lists are combined by reciprocal-rank fusion, so neither score
        scale has to be calibrated against the other.
        """
        rows, matrix = self.catalog()
        if not (person or place or date_from or date_to or media_type):
            candidates = range(len(rows))
        else:
            candidates = []
            for index, meta in enumerate(rows):
                date = meta["date"][:10]
                if person and person not in meta["persons"]: continue
                if place and place.casefold() not in meta["place"].casefold(): continue
                if date_from and date < date_from: continue
                if date_to and date > date_to: continue
                if media_type and media_type != meta["media_type"]: continue
                candidates.append(index)
        text = (text or "").strip()
        if (vector is None and not text) or not candidates:
            results = [dict(rows[i], score=None) for i in sorted(candidates, key=lambda i: rows[i]["date"], reverse=True)[:limit]]
            return {"results": [public_asset(r) for r in results], "total": len(candidates)}
        frames = self.videos.frames_catalog()
        owners = self._frame_owners(frames)
        best = frame_scores = None
        visual_rank = {}
        if vector is not None:
            best = matrix @ vector
            if frames["count"]:
                frame_scores = frames["matrix"] @ vector
                group_max = np.maximum.reduceat(frame_scores, frames["starts"])
                valid = owners >= 0
                np.maximum.at(best, owners[valid], group_max[valid])
            pool = np.arange(len(rows)) if isinstance(candidates, range) else np.array(candidates, dtype=np.int64)
            depth = min(max(VISUAL_DEPTH, limit), len(pool))
            scores = best[pool]
            top = np.argpartition(-scores, depth - 1)[:depth] if depth < len(pool) else np.arange(len(pool))
            order = pool[top[np.argsort(-scores[top], kind="stable")]]
            visual_rank = {int(i): rank for rank, i in enumerate(order)}
        text_best = {}
        if text:
            allowed = None if isinstance(candidates, range) else set(candidates)
            for hit in self.videos.text_hits(text):
                index = self._index_of.get(hit["uuid"])
                if index is not None and (allowed is None or index in allowed) and index not in text_best:
                    text_best[index] = hit
        text_rank = {index: rank for rank, index in enumerate(text_best)}
        fused = {i: 1 / (RRF_K + r + 1) for i, r in visual_rank.items()}
        phrase = fold(" ".join(re.findall(r"\w+", text))) if len(re.findall(r"\w+", text)) >= 2 else None
        for i, r in text_rank.items():
            # Equal weights interleave the best keyword and visual matches; a whole phrase said
            # or shown word for word is the strongest evidence there is, so it ranks first.
            weight = PHRASE_WEIGHT if phrase and phrase in fold(" ".join(re.findall(r"\w+", text_best[i]["text"]))) else 1.0
            fused[i] = fused.get(i, 0) + weight / (RRF_K + r + 1)
        order = sorted(fused, key=lambda i: (-fused[i], -(float(best[i]) if best is not None else 0)))[:limit]
        groups = {u: g for g, u in enumerate(frames["uuids"])}
        results = []
        for i in order:
            row = dict(rows[i], score=float(best[i]) if best is not None else None)
            if row["media_type"] == "video":
                row["moments"] = self._moments(row, i, frames, groups, frame_scores, text_best.get(i), text,
                                               text_first=i in text_rank and text_rank[i] <= visual_rank.get(i, len(fused)))
            results.append(row)
        return {"results": [public_asset(r) for r in results], "total": len(candidates)}

    def _frame_owners(self, frames):
        key = (id(self._rows), frames["count"], frames["max_id"], len(frames["uuids"]))
        if self._owners_key != key:
            self._owners = np.array([self._index_of.get(u, -1) for u in frames["uuids"]], dtype=np.int64)
            self._owners_key = key
        return self._owners

    def _moments(self, row, index, frames, groups, frame_scores, hit, query, text_first):
        from .video import time_label
        uuid = row["uuid"]
        moments = []
        group = groups.get(uuid)
        if frame_scores is not None and group is not None:
            lo = int(frames["starts"][group])
            hi = int(frames["starts"][group + 1]) if group + 1 < len(frames["starts"]) else frames["count"]
            j = lo + int(np.argmax(frame_scores[lo:hi]))
            moments.append({"kind": "seen", "time": round(float(frames["times"][j]), 2), "time_label": time_label(frames["times"][j]),
                            "score": round(float(frame_scores[j]), 4), "frame_id": int(frames["ids"][j]),
                            "frame_url": f"/api/assets/{uuid}/frames/{int(frames['ids'][j])}"})
        if hit:
            moment = {"kind": hit["kind"], "time": round(float(hit["start"]), 2), "time_label": time_label(hit["start"]),
                      "snippet": snippet(hit["text"], query), "frame_id": hit.get("frame_id"),
                      "frame_url": f"/api/assets/{uuid}/frames/{hit['frame_id']}" if hit.get("frame_id") else None}
            moments.insert(0 if text_first or not moments else 1, moment)
        return moments

    def catalog(self):
        """Cache the matrix, but invalidate on either database or WAL changes."""
        signature = []
        for file in (self.path, Path(str(self.path)+"-wal")):
            try:
                stat = file.stat()
                signature.append((stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError: signature.append(None)
        with self._cache_lock:
            if signature != self._signature:
                with self.connect() as db:
                    records = db.execute("SELECT metadata,embedding FROM assets WHERE embedding IS NOT NULL").fetchall()
                self._rows = [json.loads(r[0]) for r in records]
                self._matrix = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in records]) if records else np.empty((0,0), dtype=np.float32)
                self._index_of = {r["uuid"]: i for i, r in enumerate(self._rows)}
                self._signature = signature
            return self._rows, self._matrix


def public_asset(row):
    result = {k: row.get(k) for k in ("uuid", "filename", "date", "persons", "place", "media_type", "score")} | {
        "thumbnail_url": f"/api/assets/{row['uuid']}/thumbnail",
        "preview_url": f"/api/assets/{row['uuid']}/preview",
        "preview_scope": "local video poster" if row["media_type"] == "video" else "local derivative",
    }
    if row["media_type"] == "video" and row.get("duration") is not None:
        result["duration"] = round(float(row["duration"]), 2)
    if row.get("moments"):
        moment = row["moments"][0]
        result["moment"], result["moments"] = moment, row["moments"]
        if moment.get("frame_url"):
            # Photos cannot open a video at a time, so the result shows the moment's frame and its time.
            result["poster_url"] = result["thumbnail_url"]
            result["thumbnail_url"] = moment["frame_url"]
            result["preview_scope"] = f"video frame at {moment['time_label']}"
    return result


def snippet(text, query, width=160):
    """The matching stretch of a transcript or on-screen text, around the first query word."""
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    words = re.findall(r"\w+", fold(query))
    folded = fold(text)
    position = min((folded.find(w) for w in words if folded.find(w) >= 0), default=0)
    start = max(0, min(position - width // 3, len(text) - width))
    return ("…" if start else "") + text[start:start + width].strip() + ("…" if start + width < len(text) else "")
