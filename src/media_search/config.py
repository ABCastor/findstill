from pathlib import Path
import json
import os

MODEL = "google/siglip2-base-patch16-224"
REVISION = "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"


def runtime() -> Path:
    path = Path(os.environ.get("MEDIA_SEARCH_HOME", Path.home() / "Library/Application Support/MediaSearch"))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def settings() -> dict:
    file = runtime() / "settings.json"
    return json.loads(file.read_text()) if file.exists() else {}
