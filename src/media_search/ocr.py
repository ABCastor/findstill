"""Bounded local OCR for one explicit page image, using macOS Apple Vision."""
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import warnings

from PIL import Image, UnidentifiedImageError

from .config import runtime

MAX_EDGE = 12_000
MAX_PIXELS = 60_000_000
MAX_BYTES = 150_000_000


class OCRError(RuntimeError):
    """Local compiler, image decoder or Vision could not complete OCR."""


def _source() -> Path:
    packaged = Path(__file__).with_name("native") / "ocr.swift"
    if packaged.is_file():
        return packaged
    editable = Path(__file__).resolve().parents[2] / "native" / "ocr.swift"
    if editable.is_file():
        return editable
    raise OCRError("Apple Vision OCR helper source is missing; reinstall media-search")


def _run(arguments: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(arguments, capture_output=True, text=True, check=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise OCRError(f"Local OCR operation exceeded {timeout} seconds") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (getattr(exc, "stderr", None) or str(exc)).strip()
        raise OCRError(f"Local OCR operation failed: {detail}") from exc


def _helper() -> Path:
    # Lock only compilation. Separate OCR calls can safely share the immutable binary.
    import fcntl
    source = _source()
    fingerprint = hashlib.sha256(b"macos12-target-sdk-v1" + source.read_bytes() + platform.machine().encode()
                                 + platform.mac_ver()[0].encode()).hexdigest()[:20]
    home = runtime()
    binary_dir = home / "bin"
    binary_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = binary_dir / f"ocr-{fingerprint}"
    with (binary_dir / "ocr-compile.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not binary.is_file():
            compiler = _run(["/usr/bin/xcrun", "--find", "swiftc"], timeout=30).stdout.strip()
            if not compiler:
                raise OCRError("Swift compiler unavailable; install Xcode Command Line Tools")
            sdk = _run(["/usr/bin/xcrun", "--show-sdk-path"], timeout=30).stdout.strip()
            if not sdk:
                raise OCRError("macOS SDK unavailable; install Xcode Command Line Tools")
            pending = binary.with_suffix(".pending")
            _run([compiler, "-O", "-target", f"{platform.machine()}-apple-macosx12.0",
                  "-sdk", sdk, "-module-cache-path", str(home / "swift-module-cache"),
                  str(source), "-o", str(pending)], timeout=120)
            pending.chmod(0o700)
            pending.replace(binary)
    return binary


def read_page(path: str | Path) -> dict:
    """Return raw text lines and geometry, never mapped logbook fields.

    Bounding boxes use normalized EXIF-oriented image coordinates: top-left
    origin, x right, y down. Vision confidence is not calibrated accuracy.
    The helper downsamples images beyond 6000 pixels on their longest edge.
    """
    image_path = Path(path).expanduser().resolve()
    if not image_path.is_file():
        raise ValueError("OCR requires an existing image file")
    if image_path.stat().st_size > MAX_BYTES:
        raise ValueError("OCR image exceeds the 150 MB file limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(image_path) as image:
                width, height = image.size
                if min(width, height) <= 0 or max(width, height) > MAX_EDGE or width * height > MAX_PIXELS:
                    raise ValueError("OCR image exceeds the 12000-edge / 60-million-pixel limit")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("OCR accepts one still image; extract a page or frame first")
                image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("OCR requires a valid, bounded image file") from exc
    if platform.system() != "Darwin":
        raise OCRError("Local Apple Vision OCR requires macOS")
    result = _run([str(_helper()), str(image_path)], timeout=120)
    try:
        page = json.loads(result.stdout)
        if page.get("schema_version") != 1 or not isinstance(page.get("lines"), list):
            raise ValueError("unsupported result schema")
    except (json.JSONDecodeError, ValueError, AttributeError) as exc:
        raise OCRError("Apple Vision helper returned invalid OCR JSON") from exc
    return page
