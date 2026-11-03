"""In-video search: chunking/dedupe, language choice, resume/skip, FTS ranking, result shape.

Everything here is synthetic: solid-colour frames, invented phrases, fake fetchers.
"""
import json
from io import BytesIO
from pathlib import Path
import platform
import shutil
import subprocess
import uuid

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from media_search import video
from media_search.indexer import synchronize
from media_search.server import create_app
from media_search.store import VISUAL_DEPTH, Store, snippet
from media_search.video import VideoStore, choose_transcript, dedupe, fts_query, index_videos, nearest_frame, ocr_segments, time_label

COLOURS = {"red": (1, 0, 0), "green": (0, 1, 0), "blue": (0, 0, 1)}


def unit(values):
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def poster(tmp_path, name):
    path = tmp_path / f"{name}.jpg"
    Image.new("RGB", (16, 16), "gray").save(path)
    return path


def video_row(tmp_path, name, date="2026-01-01", person="Ada", duration=6.0):
    return dict(uuid=str(uuid.uuid4()).upper(), filename=name + ".mov", date=date, persons=[person], place="Turin",
                media_type="video", path=str(poster(tmp_path, name)), modified=None, duration=duration, original_bytes=1000)


def photo_row(tmp_path, name):
    return dict(uuid=str(uuid.uuid4()).upper(), filename=name + ".jpg", date="2026-02-01", persons=["Ada"], place="Turin",
                media_type="image", path=str(poster(tmp_path, name)), modified=None)


def embed(store, row, vector):
    with store.connect() as db:
        db.execute("UPDATE assets SET embedding=? WHERE uuid=?", (unit(vector).tobytes(), row["uuid"]))


class FakeEncoder:
    """Mean colour as the 'embedding': red, green and blue frames are orthogonal."""
    device = "cpu"

    def images(self, images):
        return np.stack([unit(np.asarray(i, dtype=np.float32).reshape(-1, 3).mean(0) + 1e-3) for i in images])


class FakeFetcher:
    name = "fake"

    def __init__(self, fail=()):
        self.calls, self.fail, self.directories = [], set(fail), []

    def fetch(self, row, directory):
        self.calls.append(row["uuid"])
        directory.mkdir(parents=True, mode=0o700)
        self.directories.append(directory)
        if row["uuid"] in self.fail:
            raise RuntimeError("synthetic fetch failure")
        path = directory / "clip.mov"
        path.write_bytes(b"synthetic video bytes")
        return path


class FakeAnalyzer:
    """Scripted frames (colour per 2 s), one on-screen text and one spoken phrase per clip."""

    def __init__(self, script):
        self.script = script  # uuid -> dict(colours=[...], screen={index: text}, said=[(start, end, text, locale)])
        self.current = None

    def frames(self, path, out, interval, duration=None):
        out.mkdir(mode=0o700)
        uuid_ = next(u for u, s in self.script.items() if s.get("active"))
        self.current = self.script[uuid_]
        rows = []
        for index, colour in enumerate(self.current["colours"]):
            frame = out / f"frame-{index:03d}.jpg"
            Image.new("RGB", (32, 32), colour).save(frame)
            rows.append({"time": index * interval, "path": str(frame)})
        return {"schema_version": 1, "duration": len(rows) * interval, "has_audio": bool(self.current.get("said")), "frames": rows}

    def ocr(self, paths, locales):
        pages = []
        for path in paths:
            index = int(Path(path).stem.split("-")[1])
            text = self.current.get("screen", {}).get(index)
            pages.append({"path": path, "lines": [{"text": text, "confidence": 0.9}] if text else []})
        return pages

    def transcribe(self, path, work, locales, duration=None):
        candidates = []
        for locale in locales:
            segments = [{"start": s, "end": e, "text": t} for s, e, t, spoken in self.current.get("said", []) if spoken == locale]
            language = locale.split("-")[0] if segments else "und"
            candidates.append({"locale": locale, "words": sum(len(s["text"].split()) for s in segments), "mean_confidence": 0.9,
                               "language": {"dominant": language, "hypotheses": {language: 0.95}}, "segments": segments, "error": None})
        return {"schema_version": 1, "has_audio": True, "candidates": candidates}


class ScriptedRun:
    """Activates one scripted clip per fetch, like a real pipeline processing one video at a time."""

    def __init__(self, script, fail=()):
        self.analyzer = FakeAnalyzer(script)
        self.script = script
        outer = self

        class Fetch(FakeFetcher):
            def fetch(self, row, directory):
                for key, value in outer.script.items():
                    value["active"] = key == row["uuid"]
                return super().fetch(row, directory)

        self.fetcher = Fetch(fail)


def library(tmp_path, rows):
    store = Store(tmp_path / "runtime")
    synchronize(store, rows, "generation")
    return store


def run(store, scripted, **kwargs):
    lines = []
    index_videos(store, fetcher=scripted.fetcher, analyzer=scripted.analyzer, encoder=FakeEncoder(), config={},
                 log=lambda line, flush=False: lines.append(line), low_priority=False, **kwargs)
    return lines


# ---------------------------------------------------------------- pure decisions

def test_dedupe_drops_only_consecutive_near_duplicates():
    a, b = unit([1, 0, 0]), unit([0, 1, 0])
    near = unit([1, 0.05, 0])
    assert dedupe(np.stack([a, near, a, b, b, a]), 0.95) == [0, 3, 5]
    assert dedupe(np.stack([a, b, a]), 0.95) == [0, 1, 2]
    assert dedupe(np.empty((0, 3), dtype=np.float32)) == []


def test_on_screen_text_merges_across_consecutive_frames_and_skips_noise():
    pages = [{"lines": [{"text": "GATE B12", "confidence": 0.9}]}, {"lines": [{"text": "GATE B12", "confidence": 0.8}]},
             {"lines": [{"text": "x", "confidence": 0.9}, {"text": "faint", "confidence": 0.1}]},
             {"lines": [{"text": "GATE B12", "confidence": 0.9}]}]
    segments = ocr_segments(pages, [(0, 0.0), (1, 2.0), (2, 4.0), (3, 6.0)])
    assert [(s["start"], s["end"], s["frame"]) for s in segments] == [(0.0, 2.0, 0), (6.0, 6.0, 3)]
    assert nearest_frame([0.0, 2.0, 4.0], 3.1) == 1 and nearest_frame([2.0], 0.5) == 0 and nearest_frame([], 1) is None


def candidate(locale, dominant, own, confidence, text="parole parole parole"):
    return {"locale": locale, "words": len(text.split()), "mean_confidence": confidence, "error": None,
            "language": {"dominant": dominant, "hypotheses": {locale[:2]: own}}, "segments": [{"start": 0, "end": 1, "text": text}]}


def test_language_agreement_beats_confidence():
    # Measured: on English audio the it-IT pass had the HIGHER confidence but read as English.
    english = [candidate("it-IT", "en", 0.001, 0.88), candidate("en-US", "en", 0.98, 0.86)]
    assert choose_transcript(english)["locale"] == "en-US"
    italian = [candidate("it-IT", "it", 0.97, 0.80), candidate("en-US", "es", 0.02, 0.85)]
    assert choose_transcript(italian)["locale"] == "it-IT"
    # No agreement on real-length text: store nothing rather than garbage.
    sentence = "una frase abbastanza lunga da riconoscere"
    assert choose_transcript([candidate("it-IT", "en", 0.1, 0.9, sentence), candidate("en-US", "fr", 0.1, 0.9, sentence)]) is None
    # Too short to identify a language: accept only a confident transcript.
    short = [candidate("it-IT", "pl", 0.1, 0.95, "ciao"), candidate("en-US", "de", 0.1, 0.6, "chow")]
    assert choose_transcript(short)["locale"] == "it-IT"
    assert choose_transcript([dict(candidate("en-US", "en", 0.9, 0.9), error="transcription failed")]) is None


def test_fts_query_and_labels_are_safe():
    assert fts_query('flaps "two" OR) NEAR(') == '"flaps" "two" "or" "near"*'
    assert fts_query("  ") is None
    assert time_label(42.4) == "0:42" and time_label(3725) == "1:02:05"
    long = "word " * 60 + "the registration is papa delta " + "word " * 60
    assert "registration" in snippet(long, "registration") and len(snippet(long, "registration")) < 170


# ---------------------------------------------------------------- indexing

def scripts(a, b):
    return {
        a["uuid"]: {"colours": ["red", "red", "green", "green", "blue"], "screen": {2: "GATE B12 BOARDING"},
                    "said": [(1.0, 3.0, "benvenuti a bordo del volo", "it-IT")]},
        b["uuid"]: {"colours": ["blue", "blue"], "said": [(0.5, 1.5, "flaps fifteen please", "en-US")]},
    }


def test_resume_skip_change_and_temporary_cleanup(tmp_path):
    a, b, photo = video_row(tmp_path, "a"), video_row(tmp_path, "b"), photo_row(tmp_path, "p")
    store = library(tmp_path, [a, b, photo])
    scripted = ScriptedRun(scripts(a, b))
    run(store, scripted)
    vstore = VideoStore(store.home)
    status = vstore.status([a["uuid"], b["uuid"]])
    assert (status["done"], status["failed"], status["pending"]) == (2, 0, 0)
    assert status["frames"] == 3 + 1  # red/green/blue kept for a, one blue for b
    assert all(not d.exists() for d in scripted.fetcher.directories), "originals must never be kept"
    assert not any(vstore.work_root.iterdir())
    # Already indexed: nothing fetched again.
    scripted.fetcher.calls.clear()
    run(store, scripted)
    assert scripted.fetcher.calls == []
    # An edited video is indexed again; the other is skipped.
    a["modified"] = "2026-03-01T10:00:00"
    synchronize(store, [a, b, photo], "second")
    run(store, scripted)
    assert scripted.fetcher.calls == [a["uuid"]]
    # A removed video loses its frames, text and thumbnails.
    synchronize(store, [a, photo], "third")
    run(store, scripted)
    assert not (vstore.frames_dir / b["uuid"]).exists()
    with vstore.connect() as db:
        assert db.execute("SELECT count(*) FROM texts WHERE uuid=?", (b["uuid"],)).fetchone()[0] == 0


def test_failures_retry_then_stop_and_crash_resumes(tmp_path):
    a, b = video_row(tmp_path, "a", "2026-01-02"), video_row(tmp_path, "b", "2026-01-01")
    store = library(tmp_path, [a, b])
    scripted = ScriptedRun(scripts(a, b), fail={a["uuid"]})
    for _ in range(video.MAX_ATTEMPTS):
        run(store, scripted)
    status = VideoStore(store.home).status([a["uuid"], b["uuid"]])
    assert (status["done"], status["failed"]) == (1, 1)
    scripted.fetcher.calls.clear()
    run(store, scripted)
    assert scripted.fetcher.calls == [], "a video that failed three times waits for --retry-failed"
    run(store, scripted, retry_failed=True)
    assert scripted.fetcher.calls == [a["uuid"]]
    # A crash (here: interrupt during the second video) keeps the first and resumes the rest.
    c, d = video_row(tmp_path, "c", "2026-05-02"), video_row(tmp_path, "d", "2026-05-01")
    store = library(tmp_path / "second", [c, d])
    scripted = ScriptedRun(scripts(c, d))
    original = scripted.analyzer.transcribe
    def interrupt(path, work, locales, duration=None):
        if scripted.script[d["uuid"]]["active"]:
            raise KeyboardInterrupt
        return original(path, work, locales, duration)
    scripted.analyzer.transcribe = interrupt
    with pytest.raises(KeyboardInterrupt):
        run(store, scripted)
    vstore = VideoStore(store.home)
    assert vstore.status([c["uuid"], d["uuid"]])["done"] == 1
    assert not any(vstore.work_root.iterdir())
    scripted.analyzer.transcribe = original
    scripted.fetcher.calls.clear()
    run(store, scripted)
    assert scripted.fetcher.calls == [d["uuid"]]


def test_low_disk_pauses_without_failing(tmp_path, monkeypatch):
    a, b = video_row(tmp_path, "a"), video_row(tmp_path, "b")
    store = library(tmp_path, [a, b])
    monkeypatch.setattr(video, "free_bytes", lambda path: 1)
    with pytest.raises(video.LowDisk):
        run(store, ScriptedRun(scripts(a, b)))
    status = VideoStore(store.home).status([a["uuid"], b["uuid"]])
    assert (status["done"], status["failed"], status["pending"]) == (0, 0, 2) and status["paused"]


def test_private_storage_and_no_text_in_logs(tmp_path):
    a, b = video_row(tmp_path, "a"), video_row(tmp_path, "b")
    store = library(tmp_path, [a, b])
    lines = run(store, ScriptedRun(scripts(a, b)))
    vstore = VideoStore(store.home)
    assert oct(vstore.path.stat().st_mode & 0o777) == "0o600"
    assert oct(vstore.frames_dir.stat().st_mode & 0o777) == "0o700"
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in vstore.frames_dir.rglob("*.jpg"))
    log = "\n".join(lines)
    assert "benvenuti" not in log and "GATE" not in log and "flaps" not in log


# ---------------------------------------------------------------- search

def indexed_library(tmp_path):
    a = video_row(tmp_path, "a", person="Ada")
    b = video_row(tmp_path, "b", person="Grace")
    photo = photo_row(tmp_path, "p")
    store = library(tmp_path, [a, b, photo])
    embed(store, a, [0.2, 0.2, 1]); embed(store, b, [0.2, 0.2, 1]); embed(store, photo, [1, 0.6, 0])
    run(store, ScriptedRun(scripts(a, b)))
    return store, a, b, photo


def test_visual_moment_is_the_matching_frame_not_the_poster(tmp_path):
    store, a, b, photo = indexed_library(tmp_path)
    result = store.search(unit([0, 1, 0]), text="")["results"]
    assert result[0]["uuid"] == a["uuid"]
    moment = result[0]["moment"]
    assert moment["kind"] == "seen" and moment["time"] == 4.0 and moment["time_label"] == "0:04"
    assert result[0]["thumbnail_url"] == moment["frame_url"] and result[0]["poster_url"].endswith("/thumbnail")
    assert result[0]["preview_scope"] == "video frame at 0:04"
    assert next(r for r in result if r["uuid"] == photo["uuid"]).get("moment") is None


def test_said_and_on_screen_words_rank_their_video_first_with_snippets(tmp_path):
    store, a, b, photo = indexed_library(tmp_path)
    said = store.search(unit([1, 0.6, 0]), text="flaps fifteen")["results"]
    assert said[0]["uuid"] == b["uuid"], "a keyword hit outranks a merely similar photo"
    assert said[0]["moment"]["kind"] == "said" and said[0]["moment"]["time_label"] == "0:00"
    assert said[0]["moment"]["snippet"] == "flaps fifteen please"
    screen = store.search(unit([1, 0.6, 0]), text="gate b12")["results"]
    assert screen[0]["uuid"] == a["uuid"] and screen[0]["moment"]["kind"] == "on_screen"
    assert screen[0]["moment"]["time"] == 4.0 and screen[0]["moments"][1]["kind"] == "seen"
    # Accents fold, prefixes match while typing, filters still apply.
    assert store.search(unit([1, 0, 0]), text="BENVENUTÌ")["results"][0]["uuid"] == a["uuid"]
    assert store.search(unit([1, 0, 0]), text="board")["results"][0]["uuid"] == a["uuid"]
    filtered = store.search(unit([1, 0.6, 0]), text="flaps fifteen", person="Ada")["results"]
    assert b["uuid"] not in [r["uuid"] for r in filtered]
    # Text alone (no vector) still finds the moment.
    assert store.search(None, text="benvenuti")["results"][0]["moment"]["kind"] == "said"


def test_api_shape_frame_endpoint_open_hint_and_status(tmp_path):
    store, a, b, photo = indexed_library(tmp_path)

    class Encoder:
        is_loaded = True
        def text(self, query):
            return unit([1, 0.6, 0])

    opened = []
    client = TestClient(create_app(store, encoder=Encoder(), opener=opened.append))
    data = client.get("/api/search", params={"q": "flaps fifteen"}).json()
    top = data["results"][0]
    assert top["uuid"] == b["uuid"] and set(top["moment"]) >= {"kind", "time", "time_label", "snippet", "frame_url"}
    assert "path" not in top and "text" not in top
    frame = client.get(top["thumbnail_url"])
    assert frame.status_code == 200 and frame.headers["content-type"] == "image/jpeg"
    with Image.open(BytesIO(frame.content)) as image:
        assert max(image.size) <= video.THUMB_EDGE
    assert client.get(f"/api/assets/{a['uuid']}/frames/999999").status_code == 404
    other = top["thumbnail_url"].replace(b["uuid"], a["uuid"])
    assert client.get(other).status_code == 404, "a frame id only resolves for its own video"
    reply = client.post(f"/api/assets/{b['uuid']}/open?t=61", headers={"Origin": "http://testserver"}).json()
    assert reply == {"ok": True, "uuid": b["uuid"], "scrub_to": "1:01"} and opened == [b["uuid"]]
    status = client.get("/api/status").json()["video"]
    assert (status["total"], status["done"], status["failed"], status["pending"]) == (2, 2, 0, 0)


def test_one_word_interleaves_but_a_whole_phrase_ranks_first(tmp_path):
    # Many photos look more like the query than the video does, so it has no visual rank.
    rows = [photo_row(tmp_path, f"p{i}") for i in range(320)]
    clip = video_row(tmp_path, "clip")
    store = library(tmp_path, rows + [clip])
    for i, row in enumerate(rows):
        embed(store, row, [1, 0.1 + i / 1000, 0])
    embed(store, clip, [0, 0, 1])
    run(store, ScriptedRun({clip["uuid"]: {"colours": ["blue"], "said": [(3.0, 4.0, "flaps fifteen please", "en-US")]}}))
    single = store.search(unit([1, 0.1, 0]), text="flaps")["results"]
    assert single[1]["uuid"] == clip["uuid"], "a single word interleaves with the best visual match"
    phrase = store.search(unit([1, 0.1, 0]), text="flaps fifteen")["results"]
    assert phrase[0]["uuid"] == clip["uuid"] and phrase[0]["moment"]["time_label"] == "0:03"
    assert len(rows) > VISUAL_DEPTH


def test_frame_catalog_appends_new_videos_incrementally(tmp_path):
    store, a, b, photo = indexed_library(tmp_path)
    vstore = store.videos
    first = vstore.frames_catalog()
    c = video_row(tmp_path, "c")
    synchronize(store, [a, b, photo, c], "more")
    run(store, ScriptedRun({**scripts(a, b), c["uuid"]: {"colours": ["green"]}}))
    second = vstore.frames_catalog()
    assert second["count"] == first["count"] + 1 and second["uuids"][: len(first["uuids"])] == first["uuids"]
    assert store.search(unit([0, 1, 0]))["results"][0]["uuid"] in (a["uuid"], c["uuid"])


# ---------------------------------------------------------------- native helper (synthetic media, macOS 26+)

def native_available():
    return platform.system() == "Darwin" and int(platform.mac_ver()[0].split(".")[0] or 0) >= 26 and shutil.which("ffmpeg")


@pytest.mark.skipif(not native_available(), reason="needs macOS 26+ SpeechAnalyzer and ffmpeg to build a synthetic clip")
def test_native_helper_reads_frames_text_and_speech(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_SEARCH_HOME", str(tmp_path / "runtime"))
    from PIL import ImageFont
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 56)
    plain, sign = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (640, 360), "navy").save(plain)
    image = Image.new("RGB", (640, 360), "darkgreen")
    ImageDraw.Draw(image).text((60, 140), "GATE B12 BOARDING", font=font, fill="white")
    image.save(sign)
    speech = tmp_path / "speech.aiff"
    subprocess.run(["say", "-v", "Samantha", "-o", str(speech), "The weather today is sunny and we are ready for departure."], check=True)
    clip = tmp_path / "clip.mov"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-loop", "1", "-t", "3", "-i", str(plain), "-loop", "1", "-t", "4", "-i", str(sign),
                    "-i", str(speech), "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:a",
                    "-c:v", "h264", "-pix_fmt", "yuv420p", "-r", "10", "-c:a", "aac", str(clip)], check=True)
    analyzer = video.Analyzer()
    frames = analyzer.frames(clip, tmp_path / "frames", 2, 7)
    assert frames["has_audio"] and len(frames["frames"]) >= 3
    pages = analyzer.ocr([f["path"] for f in frames["frames"]], ["en-US", "it-IT"])
    assert "GATE B12 BOARDING" not in json.dumps(pages[0]) and "GATE B12 BOARDING" in json.dumps(pages[-1])
    speech_result = analyzer.transcribe(clip, tmp_path, ["it-IT", "en-US"], 7)
    chosen = choose_transcript(speech_result["candidates"])
    assert chosen and chosen["locale"] == "en-US" and "departure" in " ".join(s["text"] for s in chosen["segments"]).lower()
    assert not list(tmp_path.glob("audio-*.m4a")), "the extracted audio is deleted"


def test_fetch_routes_behind_one_interface(tmp_path, monkeypatch):
    from media_search import fetch
    interpreter = tmp_path / "bin" / "python"
    interpreter.parent.mkdir()
    interpreter.write_text("")
    config = {"photos_python": str(interpreter), "photos_library": "/Library.photoslibrary"}
    assert fetch.default_fetcher(config).name == "osxphotos"
    assert fetch.default_fetcher({**config, "photos_helper_app": "/Helper.app"}).name == "helper"
    assert fetch.default_fetcher({**config, "video_fetcher": "osxphotos", "photos_helper_app": "/Helper.app"}).name == "osxphotos"
    with pytest.raises(ValueError):
        fetch.default_fetcher({"video_fetcher": "cloud"})
    fetcher = fetch.OsxphotosFetcher(config)
    row = {"uuid": "11111111-1111-4111-8111-111111111111", "original_bytes": 5_000_000}
    commands = []
    def export(command, **kwargs):
        commands.append((command, kwargs))
        out = Path(command[3])
        (out / "original.mov").write_bytes(b"synthetic")
        (out / ".osxphotos_export.db").write_bytes(b"db")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(fetch.subprocess, "run", export)
    path = fetcher.fetch(row, tmp_path / "out")
    command, kwargs = commands[0]
    assert path.name == "original.mov" and oct(path.stat().st_mode & 0o777) == "0o600"
    assert command[2] == "export" and "--download-missing" in command and "--skip-edited" in command
    assert command[command.index("--uuid") + 1] == row["uuid"] and "--library" in command
    assert "shell" not in kwargs and kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] > 180
    with pytest.raises(ValueError):
        fetcher.fetch(row, tmp_path / "out")  # never reuses an existing folder
