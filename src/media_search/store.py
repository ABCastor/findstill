from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import numpy as np
from .config import runtime


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

    def search(self, vector=None, limit=60, person="", place="", date_from="", date_to="", media_type=""):
        rows, matrix = self.catalog()
        candidates = []
        for index, meta in enumerate(rows):
            date = meta["date"][:10]
            if person and person not in meta["persons"]: continue
            if place and place.casefold() not in meta["place"].casefold(): continue
            if date_from and date < date_from: continue
            if date_to and date > date_to: continue
            if media_type and media_type != meta["media_type"]: continue
            candidates.append(index)
        if vector is not None and candidates:
            vectors = matrix if len(candidates) == len(rows) else matrix[candidates]
            scores = vectors @ vector
            order = np.argsort(-scores, kind="stable")[:limit]
            results = [dict(rows[candidates[i]], score=float(scores[i])) for i in order]
        else:
            results = [dict(rows[i], score=None) for i in sorted(candidates, key=lambda i: rows[i]["date"], reverse=True)[:limit]]
        return {"results": [public_asset(r) for r in results], "total": len(candidates)}

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
                self._signature = signature
            return self._rows, self._matrix


def public_asset(row):
    return {k: row.get(k) for k in ("uuid", "filename", "date", "persons", "place", "media_type", "score")} | {
        "thumbnail_url": f"/api/assets/{row['uuid']}/thumbnail",
        "preview_url": f"/api/assets/{row['uuid']}/preview",
        "preview_scope": "local video poster" if row["media_type"] == "video" else "local derivative",
    }
