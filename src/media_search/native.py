"""Compile and cache the bundled Swift helpers (Apple frameworks only)."""
import fcntl
import hashlib
from pathlib import Path
import platform
import subprocess

from .config import runtime


class NativeError(RuntimeError):
    """The local compiler or a native helper could not complete."""


def source(name: str) -> Path:
    packaged = Path(__file__).with_name("native") / f"{name}.swift"
    if packaged.is_file():
        return packaged
    editable = Path(__file__).resolve().parents[2] / "native" / f"{name}.swift"
    if editable.is_file():
        return editable
    raise NativeError(f"Native helper source {name}.swift is missing; reinstall Findstill")


def run(arguments: list[str], *, timeout: float, error=NativeError) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(arguments, capture_output=True, text=True, check=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise error(f"Local native operation exceeded {round(timeout)} seconds") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (getattr(exc, "stderr", None) or str(exc)).strip()
        raise error(f"Local native operation failed: {detail[:300]}") from exc


def helper(name: str, minimum_macos: str, *, source_path: Path | None = None, home: Path | None = None,
           runner=run, extra_flags=(), error=NativeError) -> Path:
    """Compile once per source, machine and OS version; lock only compilation."""
    source_path = source_path or source(name)
    fingerprint = hashlib.sha256(f"macos{minimum_macos}-target-sdk-v1".encode() + source_path.read_bytes()
                                 + platform.machine().encode() + platform.mac_ver()[0].encode()).hexdigest()[:20]
    home = home or runtime()
    binary_dir = home / "bin"
    binary_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = binary_dir / f"{name}-{fingerprint}"
    with (binary_dir / f"{name}-compile.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not binary.is_file():
            compiler = runner(["/usr/bin/xcrun", "--find", "swiftc"], timeout=30).stdout.strip()
            if not compiler:
                raise error("Swift compiler unavailable; install Xcode Command Line Tools")
            sdk = runner(["/usr/bin/xcrun", "--show-sdk-path"], timeout=30).stdout.strip()
            if not sdk:
                raise error("macOS SDK unavailable; install Xcode Command Line Tools")
            pending = binary.with_suffix(".pending")
            runner([compiler, "-O", *extra_flags, "-target", f"{platform.machine()}-apple-macosx{minimum_macos}",
                    "-sdk", sdk, "-module-cache-path", str(home / "swift-module-cache"),
                    str(source_path), "-o", str(pending)], timeout=300)
            pending.chmod(0o700)
            pending.replace(binary)
    return binary
