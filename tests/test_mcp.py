import asyncio
import base64
from io import BytesIO

import httpx
import pytest
from PIL import Image

pytest.importorskip("mcp")
from mcp import Client
from media_search.mcp_server import GALLERY_URI, Service, create_server

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
ROWS = [
    {"uuid": A, "filename": "a.jpg", "date": "2024-01-05T18:00:00", "persons": ["Ada Example"], "place": "Rome", "media_type": "image", "score": .31, "thumbnail_url": "x"},
    {"uuid": B, "filename": "b.mov", "date": "2023-02-01T09:00:00", "persons": [], "place": "", "media_type": "video", "score": .2, "thumbnail_url": "x"},
]


def jpeg(size):
    output = BytesIO()
    Image.new("RGB", size, "teal").save(output, format="JPEG")
    return output.getvalue()


def fake_service(calls, running=True):
    def handle(request):
        calls.append((request.method, request.url.path, dict(request.url.params), request.headers.get("x-media-search-client")))
        if not running: raise httpx.ConnectError("refused")
        path = request.url.path
        if path == "/api/search": return httpx.Response(200, json={"results": ROWS, "total": 2, "elapsed_ms": 4})
        if path == "/api/status": return httpx.Response(200, json={"indexed": 2, "library_total": 3, "failed": 0, "indexing": False, "secret_path": "/x"})
        if path == "/api/options": return httpx.Response(200, json={"persons": ["Ada Example"], "places": ["Rome"]})
        if path.endswith("/thumbnail") and A in path: return httpx.Response(200, content=jpeg((512, 384)))
        if path.endswith("/preview"): return httpx.Response(200, content=jpeg((2048, 1536)))
        if path.endswith("/open"): return httpx.Response(200, json={"ok": True, "uuid": A})
        if path == f"/api/assets/{A}": return httpx.Response(200, json=ROWS[0])
        return httpx.Response(404, json={"detail": "Asset is no longer indexed"})
    return Service(transport=httpx.MockTransport(handle))


def run(service, scenario):
    async def go():
        async with Client(create_server(service)) as client:
            return await scenario(client)
    return asyncio.run(go())


def test_search_returns_text_previews_and_ui_binding_without_image_bytes_in_structured_data():
    calls = []
    async def scenario(client):
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool("search_photos", {"query": "sunset", "place": "Rome", "preview_images": 1})
        html = await client.read_resource(GALLERY_URI)
        return tools, result, html
    tools, result, html = run(fake_service(calls), scenario)
    assert tools["search_photos"].meta["ui"] == {"resourceUri": GALLERY_URI, "visibility": ["model", "app"]}
    assert tools["photo_thumbnails"].meta["ui"]["visibility"] == ["app"]
    assert not result.is_error
    text, image = result.content
    assert "1. 2024-01-05 · Rome · Ada Example · image · a.jpg · " + A in text.text
    assert "not probabilities" in text.text
    with Image.open(BytesIO(base64.b64decode(image.data))) as preview:
        assert max(preview.size) == 384
    assert result.structured_content["results"][0] == {k: ROWS[0][k] for k in ("uuid", "filename", "date", "persons", "place", "media_type", "score")}
    assert "data:image" not in str(result.structured_content)
    assert calls[0][1] == "/api/search" and calls[0][2]["q"] == "sunset" and calls[0][2]["place"] == "Rome"
    assert "tools/call" in html.contents[0].text and html.contents[0].mime_type == "text/html;profile=mcp-app"


def test_gallery_thumbnails_and_opening_use_exact_identifiers():
    calls = []
    async def scenario(client):
        thumbs = await client.call_tool("photo_thumbnails", {"uuids": [A.lower(), "33333333-3333-4333-8333-333333333333"]})
        opened = await client.call_tool("open_in_photos", {"uuid": A.lower()})
        return thumbs, opened
    thumbs, opened = run(fake_service(calls), scenario)
    thumbnails = thumbs.structured_content["thumbnails"]
    assert thumbnails[A].startswith("data:image/jpeg;base64,")
    assert thumbnails["33333333-3333-4333-8333-333333333333"] is None
    assert opened.structured_content == {"ok": True, "uuid": A}
    assert ("POST", f"/api/assets/{A}/open", {}, "cli") in calls


def test_status_hides_unlisted_fields_and_errors_are_readable():
    async def scenario(client):
        status = await client.call_tool("photo_status", {})
        bad = await client.call_tool("open_in_photos", {"uuid": "../../etc"})
        too_many = await client.call_tool("search_photos", {"query": "x", "limit": 500})
        return status, bad, too_many
    status, bad, too_many = run(fake_service([]), scenario)
    assert status.structured_content["indexed"] == 2 and "secret_path" not in status.structured_content
    assert bad.is_error and "uuid must be a Photos asset UUID" in bad.content[0].text
    assert too_many.is_error and "limit must be between 1 and 60" in too_many.content[0].text


def test_stopped_service_reports_how_to_start_it():
    async def scenario(client):
        return await client.call_tool("search_photos", {"query": "x"})
    result = run(fake_service([], running=False), scenario)
    assert result.is_error and "findstill serve" in result.content[0].text
