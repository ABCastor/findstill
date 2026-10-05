from contextlib import asynccontextmanager
from datetime import date
import os
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlparse
import uuid as uuid_module
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .config import settings, runtime
from .store import Store, public_asset
from .thumbnails import PreviewUnavailable, ThumbnailCache


def create_app(store=None, encoder=None, opener=None):
    store = store or Store()
    thumbnails = ThumbnailCache(store.home)
    model_lock = threading.Lock()
    model = encoder

    def text_vector(query):
        nonlocal model
        with model_lock:
            if model is None:
                from .model import Encoder
                model = Encoder()
            return model.text(query)

    app = FastAPI(title="Findstill", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def private_local_requests(request: Request, call_next):
        origin = request.headers.get("origin")
        navigation = request.method == "GET" and request.url.path == "/" and request.headers.get("sec-fetch-mode") == "navigate" and request.headers.get("sec-fetch-dest") == "document"
        if (request.headers.get("sec-fetch-site") == "cross-site" and not navigation) or (origin and urlparse(origin).netloc != request.headers.get("host")):
            return JSONResponse({"detail": "Only requests from this local dashboard are allowed"}, status_code=403)
        if request.method not in ("GET", "HEAD") and not origin and request.headers.get("x-media-search-client") != "cli":
            return JSONResponse({"detail": "Local client header required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
        return response

    def asset(identifier):
        try:
            canonical = str(uuid_module.UUID(identifier)).upper()
        except ValueError:
            raise HTTPException(404, "Unknown asset")
        row = store.asset(canonical) or store.asset(canonical.lower())
        if not row: raise HTTPException(404, "Asset is no longer indexed")
        return row

    @app.get("/api/status")
    def status():
        result = store.status()
        result.setdefault("library_total", 0)
        result.setdefault("indexing", False)
        return result

    @app.get("/api/options")
    def options(): return store.options()

    @app.get("/api/search")
    def search(q: str = Query("", max_length=512), limit: int = Query(60, ge=1, le=200),
               person: str = Query("", max_length=256), place: str = Query("", max_length=256),
               date_from: str = "", date_to: str = "", media_type: str = ""):
        start = time.perf_counter()
        try:
            for value in (date_from, date_to):
                if value and date.fromisoformat(value).isoformat() != value: raise ValueError("Non-canonical date")
        except ValueError: raise HTTPException(422, "Dates must use YYYY-MM-DD")
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "Start date must precede end date")
        if media_type not in ("", "image", "video"): raise HTTPException(422, "Unknown media type")
        vector = text_vector(q.strip()) if q.strip() else None
        result = store.search(vector, limit, person, place, date_from, date_to, media_type)
        result["elapsed_ms"] = round((time.perf_counter()-start)*1000, 1)
        return result

    @app.get("/api/assets/{identifier}")
    def detail(identifier: str): return public_asset(asset(identifier))

    def image_response(identifier, edge):
        row = asset(identifier)
        # Photos can remove one derivative while retaining smaller indexed alternatives.
        paths = dict.fromkeys([row.get("path"), *(row.get("paths") or [])])
        for path in paths:
            if not path: continue
            try:
                thumbnail_path = thumbnails.get(path, edge)
            except PreviewUnavailable:
                continue
            return FileResponse(thumbnail_path, media_type="image/jpeg")
        raise HTTPException(404, "Local preview unavailable; refresh the index")

    @app.get("/api/assets/{identifier}/thumbnail")
    def thumbnail(identifier: str):
        return image_response(identifier, 512)

    @app.get("/api/assets/{identifier}/preview")
    def preview(identifier: str):
        return image_response(identifier, 2048)

    @app.post("/api/assets/{identifier}/open")
    def open_asset(identifier: str):
        row = asset(identifier)
        if opener:
            opener(row["uuid"])
        else:
            config = settings()
            interpreter = config.get("photos_python")
            if not interpreter or not Path(interpreter).is_file(): raise HTTPException(503, "Configure the installed osxphotos interpreter first")
            try:
                if config.get("photos_helper_app"):
                    responses = runtime() / "responses"
                    responses.mkdir(exist_ok=True, mode=0o700)
                    response = responses / (uuid_module.uuid4().hex+".json")
                    subprocess.run(["/usr/bin/open", "-n", config["photos_helper_app"], "--args", "show", "--uuid", row["uuid"]+"/L0/001", "--response-file", str(response)], check=True, capture_output=True, timeout=5)
                    deadline = time.monotonic()+40
                    while not response.is_file() and time.monotonic() < deadline: time.sleep(.1)
                    if not response.is_file(): raise subprocess.TimeoutExpired("Photos helper",40)
                    response.chmod(0o600)
                    if json.loads(response.read_text()).get("ok") is not True: raise subprocess.SubprocessError("Photos helper rejected the request")
                else:
                    command = str(Path(interpreter).parent / "osxphotos")
                    subprocess.run([interpreter, command, "show", row["uuid"]], check=True, capture_output=True, timeout=45)
            except (subprocess.SubprocessError, OSError):
                raise HTTPException(503, "Photos could not open the item; check local automation permission")
        return {"ok": True, "uuid": row["uuid"]}

    static = Path(__file__).with_name("static")
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="dashboard-files")
        @app.get("/")
        def dashboard(): return FileResponse(static / "index.html", media_type="text/html")
    return app
