"""Private, bounded JPEG derivatives. Source previews are only opened for reading."""
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import tempfile
import threading

from PIL import Image, ImageOps


MAX_SOURCE_PIXELS = 40_000_000
SUPPORTED_FORMATS = {"JPEG", "MPO", "PNG", "TIFF", "WEBP", "BMP", "AVIF"}
_decoders = threading.BoundedSemaphore(4)


class PreviewUnavailable(ValueError):
    pass


class ThumbnailCache:
    def __init__(self, home):
        self.directory = Path(home) / "thumbnails"
        # A fixed set bounds lock memory while deduplicating concurrent requests.
        self._locks = [threading.Lock() for _ in range(64)]

    @staticmethod
    def _identity(source):
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise PreviewUnavailable("Local preview unavailable")
        return (str(source), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    @staticmethod
    def _render(source, edge):
        with _decoders:
            with Image.open(source) as original:
                if original.format not in SUPPORTED_FORMATS or original.width * original.height > MAX_SOURCE_PIXELS:
                    raise PreviewUnavailable("Local preview unavailable")
                # JPEG decoders can avoid loading full-sized pixel buffers.
                original.draft("RGB", (edge, edge))
                oriented = ImageOps.exif_transpose(original)
                oriented.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    image = Image.new("RGB", rgba.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    image = oriented.convert("RGB")
                output = BytesIO()
                # Do not copy EXIF, locations, comments or other source metadata.
                image.info.clear()
                image.save(output, format="JPEG", quality=82, optimize=True)
                return output.getvalue()

    def get(self, path, edge=512):
        if edge not in (512, 2048):
            raise ValueError("Unsupported preview size")
        try:
            source = Path(path).resolve(strict=True)
            if self.directory.is_symlink():
                raise PreviewUnavailable("Local preview unavailable")
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
            for _ in range(2):
                identity = self._identity(source)
                key = hashlib.sha256(json.dumps(["jpeg-v2", edge, identity]).encode()).hexdigest()
                destination = self.directory / (key + ".jpg")
                with self._locks[int(key[:8], 16) % len(self._locks)]:
                    if destination.exists():
                        if destination.is_symlink() or not destination.is_file():
                            raise PreviewUnavailable("Local preview unavailable")
                        if self._identity(source) != identity:
                            continue
                        destination.chmod(0o600)
                        return destination
                    data = self._render(source, edge)
                    if self._identity(source) != identity:
                        continue
                    # Unique files and replace keep simultaneous processes from exposing partial JPEGs.
                    # Failed writes remain private; existing cache entries and source files are never deleted.
                    with tempfile.NamedTemporaryFile(dir=self.directory, prefix=key + ".", suffix=".tmp", delete=False) as temporary:
                        os.fchmod(temporary.fileno(), 0o600)
                        temporary.write(data)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                        temporary_path = Path(temporary.name)
                    temporary_path.replace(destination)
                    return destination
            raise PreviewUnavailable("Local preview changed during decoding")
        except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning, ValueError) as error:
            raise PreviewUnavailable("Local preview unavailable") from error
