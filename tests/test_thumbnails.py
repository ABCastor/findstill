from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import os
from pathlib import Path
import threading
import time
import uuid

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from media_search.indexer import synchronize
from media_search.server import create_app
from media_search.store import Store
from media_search.thumbnails import PreviewUnavailable, ThumbnailCache


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_image(tmp_path, filename="preview.png", size=(2400, 1200), color="red"):
    path = tmp_path / filename
    Image.new("RGB", size, color).save(path)
    return path


def test_thumbnail_is_small_jpeg_private_and_preserves_source(tmp_path):
    source = source_image(tmp_path)
    before = digest(source), source.stat().st_mtime_ns
    cache = ThumbnailCache(tmp_path / "runtime")
    thumbnail = cache.get(source)
    with Image.open(thumbnail) as image:
        assert image.format == "JPEG"
        assert image.size == (512, 256)
        assert not image.getexif()
    assert (digest(source), source.stat().st_mtime_ns) == before
    assert thumbnail.stat().st_mode & 0o777 == 0o600
    assert thumbnail.parent.stat().st_mode & 0o777 == 0o700
    assert cache.get(source) == thumbnail
    assert list(thumbnail.parent.iterdir()) == [thumbnail]


def test_exif_orientation_and_transparency_are_respected(tmp_path):
    source = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (80, 40), "red")
    exif = Image.Exif()
    exif[274] = 6
    exif[37510] = b"private source comment"
    image.save(source, exif=exif)
    cache = ThumbnailCache(tmp_path / "runtime")
    with Image.open(cache.get(source)) as result:
        assert result.size == (40, 80)
        assert not result.getexif()
    transparent = tmp_path / "transparent.png"
    Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(transparent)
    with Image.open(cache.get(transparent)) as result:
        assert result.getpixel((0, 0)) == (255, 255, 255)


def test_apple_multi_picture_jpeg_derivative_uses_first_visible_frame(tmp_path):
    source = tmp_path / "apple-derivative.jpg"
    Image.new("RGB", (32, 16), "red").save(
        source, format="MPO", save_all=True, append_images=[Image.new("RGB", (32, 16), "blue")]
    )
    before = digest(source)
    with Image.open(source) as original:
        assert original.format == "MPO"
    thumbnail = ThumbnailCache(tmp_path / "runtime").get(source)
    with Image.open(thumbnail) as result:
        assert result.format == "JPEG"
        assert result.size == (32, 16)
        assert result.getpixel((0, 0))[0] > 240
    assert digest(source) == before


def test_jpeg_comment_is_not_copied_into_either_cached_size(tmp_path):
    source = tmp_path / "commented.jpg"
    comment = b"Private location and source description"
    Image.new("RGB", (128, 64), "red").save(source, comment=comment)
    before = digest(source)
    with Image.open(source) as original:
        assert original.info["comment"] == comment
    cache = ThumbnailCache(tmp_path / "runtime")
    for edge in (512, 2048):
        thumbnail = cache.get(source, edge)
        with Image.open(thumbnail) as result:
            assert "comment" not in result.info
            assert not result.getexif()
        assert comment not in thumbnail.read_bytes()
    assert digest(source) == before


def test_changed_source_rebuilds_cache_even_with_restored_mtime(tmp_path):
    source = source_image(tmp_path, size=(64, 64))
    cache = ThumbnailCache(tmp_path / "runtime")
    first = cache.get(source)
    old_stat = source.stat()
    Image.new("RGB", (64, 64), "blue").save(source)
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    second = cache.get(source)
    assert first != second
    assert first.is_file()  # Existing cache entries are retained.
    with Image.open(second) as result:
        assert result.getpixel((0, 0))[2] > 240
    # A deleted source cannot remain accessible through its old cache entry.
    with pytest.raises(PreviewUnavailable):
        cache.get(tmp_path / "missing.png")


def test_concurrent_requests_publish_one_complete_thumbnail(tmp_path, monkeypatch):
    source = source_image(tmp_path, size=(1024, 512))
    cache = ThumbnailCache(tmp_path / "runtime")
    render = cache._render
    calls = []
    call_lock = threading.Lock()

    def slow_render(path, edge):
        with call_lock:
            calls.append(path)
        time.sleep(0.02)
        return render(path, edge)

    monkeypatch.setattr(cache, "_render", slow_render)
    with ThreadPoolExecutor(max_workers=12) as workers:
        paths = list(workers.map(lambda _: cache.get(source), range(24)))
    assert len(set(paths)) == 1
    assert len(calls) == 1
    with Image.open(paths[0]) as result:
        result.load()
        assert result.size == (512, 256)
    assert list(cache.directory.iterdir()) == [paths[0]]


def test_decoder_rejects_invalid_unsupported_and_oversized_sources(tmp_path, monkeypatch):
    import media_search.thumbnails as thumbnails

    cache = ThumbnailCache(tmp_path / "runtime")
    invalid = tmp_path / "invalid.jpg"
    invalid.write_bytes(b"not an image")
    unsupported = tmp_path / "animated.gif"
    Image.new("RGB", (8, 8), "red").save(unsupported)
    large = source_image(tmp_path, size=(40, 40))
    monkeypatch.setattr(thumbnails, "MAX_SOURCE_PIXELS", 1000)
    for path in (invalid, unsupported, large):
        with pytest.raises(PreviewUnavailable, match="Local preview unavailable"):
            cache.get(path)
    assert list(cache.directory.iterdir()) == []


def test_api_thumbnail_and_dialog_preview_have_distinct_bounds_and_private_errors(tmp_path):
    source = source_image(tmp_path)
    before = digest(source)
    store = Store(tmp_path / "runtime")
    identifier = str(uuid.uuid4()).upper()
    row = dict(uuid=identifier, filename="preview.png", date="2026-10-05", persons=[],
               place="", media_type="image", path=str(source), modified=None)
    synchronize(store, [row], "fixture")
    client = TestClient(create_app(store))
    detail = client.get(f"/api/assets/{identifier}").json()
    assert detail["preview_url"] == f"/api/assets/{identifier}/preview"
    for endpoint, size in (("thumbnail", (512, 256)), ("preview", (2048, 1024))):
        response = client.get(f"/api/assets/{identifier}/{endpoint}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "no-store"
        with Image.open(BytesIO(response.content)) as image:
            assert image.format == "JPEG"
            assert image.size == size
    assert digest(source) == before
    # Decoder failure exposes no source path and cannot fall back to unvalidated bytes.
    source.write_bytes(b"broken")
    response = client.get(detail["thumbnail_url"])
    assert response.status_code == 404
    assert str(source) not in response.text


def test_api_uses_live_indexed_alternate_after_selected_derivative_disappears(tmp_path):
    primary = source_image(tmp_path, filename="primary.png", color="red")
    alternate = source_image(tmp_path, filename="alternate.png", size=(800, 400), color="blue")
    before = digest(primary), digest(alternate)
    store = Store(tmp_path / "runtime")
    identifier = str(uuid.uuid4()).upper()
    row = dict(uuid=identifier, filename="photo.png", date="2026-10-05", persons=[], place="",
               media_type="image", path=str(primary), paths=[str(primary), str(alternate)], modified=None)
    synchronize(store, [row], "fixture")
    client = TestClient(create_app(store))
    endpoints = [f"/api/assets/{identifier}/{suffix}" for suffix in ("thumbnail", "preview")]
    # Populate both caches, then make the selected path unavailable without losing the fixture.
    assert all(client.get(endpoint).status_code == 200 for endpoint in endpoints)
    retained_primary = primary.rename(tmp_path / "retained-primary.png")
    for endpoint, size in zip(endpoints, ((512, 256), (800, 400)), strict=True):
        response = client.get(endpoint)
        assert response.status_code == 200
        with Image.open(BytesIO(response.content)) as result:
            assert result.size == size
            assert result.getpixel((0, 0))[2] > 240
    # Cached images alone are not accepted when every indexed source is unavailable.
    retained_alternate = alternate.rename(tmp_path / "retained-alternate.png")
    assert all(client.get(endpoint).status_code == 404 for endpoint in endpoints)
    assert (digest(retained_primary), digest(retained_alternate)) == before
