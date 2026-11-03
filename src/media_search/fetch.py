"""Bring one video original into a private folder for analysis. Never writes to Photos.

Two routes behind one small interface:
- "helper": a separately installed, signed PhotoKit helper app (configured as
  `photos_helper_app`), which asks Photos for the original resource and lets it
  download from iCloud.
- "osxphotos": the public route, `osxphotos export --download-missing` through the
  configured osxphotos interpreter. Photos itself downloads the original from iCloud.
"""
from pathlib import Path
import subprocess
from typing import Protocol

from .config import settings

VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mts", ".hevc"}


class Fetcher(Protocol):
    name: str

    def fetch(self, row: dict, directory: Path) -> Path:
        """Create `directory` (it must not exist) and return the path of the fetched original."""


def _timeout(row):
    # iCloud downloads scale with size; allow roughly 1 MB/s plus a fixed allowance.
    return 180 + int(row.get("original_bytes") or 0) / 1_000_000


class HelperFetcher:
    name = "helper"

    def __init__(self, config=None):
        self.config = config

    def fetch(self, row, directory):
        from .export import export_asset
        result = export_asset(row["uuid"], directory, download_missing=True, config=self.config, timeout=_timeout(row))
        return Path(result["path"])


class OsxphotosFetcher:
    name = "osxphotos"

    def __init__(self, config=None):
        config = settings() if config is None else config
        self.interpreter = config.get("photos_python")
        self.library = config.get("photos_library")
        # PhotoKit avoids driving the Photos app with AppleScript but needs Photos access for the interpreter.
        self.photokit = bool(config.get("video_osxphotos_photokit", False))

    def command(self, row, directory):
        interpreter = Path(self.interpreter or "")
        # Original only, no sidecars; osxphotos keeps its export database in the private folder, deleted afterwards.
        command = [str(interpreter), str(interpreter.parent / "osxphotos"), "export", str(directory), "--uuid", row["uuid"],
                   "--download-missing", "--skip-edited", "--skip-live", "--skip-raw", "--skip-bursts", "--no-progress",
                   "--filename", "original"]
        if self.photokit:
            command.append("--use-photokit")
        if self.library:
            command += ["--library", self.library]
        return command

    def fetch(self, row, directory):
        if not self.interpreter or not Path(self.interpreter).is_file():
            raise ValueError("Configure the osxphotos interpreter (photos_python) to fetch videos")
        directory = Path(directory)
        if directory.exists():
            raise ValueError("Fetch requires a new destination directory")
        directory.mkdir(parents=True, mode=0o700)
        try:
            subprocess.run(self.command(row, directory), check=True, capture_output=True, timeout=_timeout(row) + 120,
                           stdin=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError) as exc:
            # osxphotos output names files and places; never echo it.
            raise RuntimeError("osxphotos could not export the video from Photos") from exc
        files = [p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES]
        if len(files) != 1 or files[0].stat().st_size == 0:
            raise RuntimeError("osxphotos did not return exactly one video original")
        files[0].chmod(0o600)
        return files[0]


def default_fetcher(config=None):
    config = settings() if config is None else config
    choice = config.get("video_fetcher") or ("helper" if config.get("photos_helper_app") else "osxphotos")
    if choice == "helper":
        return HelperFetcher(config)
    if choice == "osxphotos":
        return OsxphotosFetcher(config)
    raise ValueError("video_fetcher must be 'helper' or 'osxphotos'")
