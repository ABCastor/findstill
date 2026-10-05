"""Export one explicitly selected Photos resource through the signed native helper."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

from .config import settings
from .store import Store


def export_asset(identifier, directory, download_missing=False, *, store=None, config=None):
    identifier = str(uuid.UUID(identifier)).upper()
    store = store or Store()
    if not store.asset(identifier):
        raise ValueError("Select an accessible indexed asset; refresh the index if it is missing")
    config = settings() if config is None else config
    helper = Path(config.get("photos_helper_app", "")).expanduser()
    if helper.suffix != ".app" or not helper.is_dir():
        raise ValueError("Configure the existing signed Photos helper app to export resources")
    out = Path(directory).expanduser().absolute()
    if out.exists() or out.is_symlink():
        raise ValueError("Export requires a new destination directory; existing files are preserved")
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    out.mkdir(mode=0o700)
    out = out.resolve()
    response = out / "photos-response.json"
    command = ["/usr/bin/open", "-n", str(helper), "--args", "export", "--uuid",
               identifier + "/L0/001", "--output", str(out), "--response-file", str(response)]
    if download_missing:
        command.append("--download-missing")
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=10)
        deadline = time.monotonic() + 180
        while not response.is_file() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not response.is_file():
            raise RuntimeError("Photos export timed out; partial evidence remains in the destination")
        response.chmod(0o600)
        records = json.loads(response.read_text())
        if not isinstance(records, list) or len(records) != 1:
            raise RuntimeError("Photos could not export the resource; inspect the private response file")
        record = records[0]
        path = Path(record["path"]).resolve()
        if record.get("uuid") != identifier or path.parent != out or not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Photos returned unexpected export evidence")
        path.chmod(0o600)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        result = {"uuid": identifier, "path": str(path), "media_type": record.get("mediaType"),
                  "bytes": path.stat().st_size, "sha256": digest.hexdigest(),
                  "download_missing_allowed": bool(download_missing), "source": "apple-photos-resource"}
        manifest = out / "export.json"
        manifest.write_text(json.dumps(result, indent=2))
        manifest.chmod(0o600)
        return result
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, KeyError) as exc:
        # Never echo native helper output, which can contain private paths/content.
        raise RuntimeError("Photos export failed; retained private evidence can be inspected locally") from exc
