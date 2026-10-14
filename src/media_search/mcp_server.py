"""MCP server: photo search results as an inline gallery, with a text and image fallback.

A thin client of the local Findstill service, so the model stays warm in one process
and the MCP process never reads the Photos library itself.
"""
import argparse
import base64
import logging
from io import BytesIO
import os
from pathlib import Path
from typing import Any
import uuid as uuid_module

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image

DEFAULT_URL = "http://127.0.0.1:18473"
GALLERY_URI = "ui://findstill/gallery.html"
MODEL_IMAGE_EDGE = 384
GALLERY_IMAGE_EDGE = 320


class Service:
    """Calls the loopback Findstill service with the same contract as the CLI."""

    def __init__(self, url=DEFAULT_URL, transport=None):
        self.url = url
        self.client = httpx.Client(base_url=url, timeout=180, trust_env=False, transport=transport)

    def _send(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TransportError:
            raise ToolError(f"Findstill is not running at {self.url}; start it with `findstill serve`") from None
        if response.status_code >= 400:
            try: detail = response.json().get("detail")
            except ValueError: detail = None
            raise ToolError(detail or f"Findstill returned HTTP {response.status_code}")
        return response

    def json(self, path, **params):
        return self._send("GET", path, params=params).json()

    def open(self, identifier):
        return self._send("POST", f"/api/assets/{identifier}/open", headers={"X-Media-Search-Client": "cli"}).json()

    def jpeg(self, identifier, edge):
        source = self._send("GET", f"/api/assets/{identifier}/thumbnail" if edge <= 512 else f"/api/assets/{identifier}/preview").content
        with Image.open(BytesIO(source)) as image:
            image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=80, optimize=True)
        return output.getvalue()


def canonical(identifier):
    try:
        return str(uuid_module.UUID(identifier)).upper()
    except (ValueError, AttributeError, TypeError):
        raise ToolError("uuid must be a Photos asset UUID") from None


def describe(row):
    parts = [row["date"][:10], row.get("place") or "", ", ".join(row.get("persons") or []), row.get("media_type") or "", row.get("filename") or ""]
    return " · ".join(p for p in parts if p) + f" · {row['uuid']}"


def search_payload(service, query, person, place, date_from, date_to, media_type, limit):
    if not 1 <= limit <= 60: raise ToolError("limit must be between 1 and 60")
    data = service.json("/api/search", q=query, limit=limit, person=person, place=place, date_from=date_from, date_to=date_to, media_type=media_type)
    results = [{k: r.get(k) for k in ("uuid", "filename", "date", "persons", "place", "media_type", "score")} for r in data["results"]]
    return {"query": query, "total": data["total"], "results": results, "elapsed_ms": data.get("elapsed_ms")}


def search_text(payload, attached):
    if not payload["results"]:
        return "No indexed photos match. Loosen the filters or check `photo_status`."
    lines = [f"{len(payload['results'])} of {payload['total']} candidates, best visual match first. Scores rank similarity; they are not probabilities."]
    lines += [f"{i}. {describe(r)}" for i, r in enumerate(payload["results"], 1)]
    if attached:
        lines.append(f"The first {attached} previews are attached in order. Look at them before claiming a match; open_in_photos shows one in Apple Photos.")
    return "\n".join(lines)


def gallery_html():
    return (Path(__file__).with_name("static") / "mcp-gallery.html").read_text()


def create_server(service):
    from mcp.server import MCPServer
    from mcp.server.apps import Apps, ResourceCsp
    from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

    apps = Apps()
    apps.add_html_resource(GALLERY_URI, gallery_html(), name="Findstill results", description="Photo results as a clickable gallery",
                           csp=ResourceCsp(connect_domains=[], resource_domains=[], frame_domains=[], base_uri_domains=[]), prefers_border=False)
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    @apps.tool(resource_uri=GALLERY_URI, visibility=["model", "app"], name="search_photos", annotations=read_only, structured_output=True,
               description="Search the local Apple Photos library by describing what a picture shows, optionally filtered by person, place, "
                           "date range (YYYY-MM-DD) and media type (image or video). Returns ranked matches with small previews.")
    def search_photos(query: str = "", person: str = "", place: str = "", date_from: str = "", date_to: str = "",
                      media_type: str = "", limit: int = 12, preview_images: int = 4) -> dict[str, Any]:
        if not 0 <= preview_images <= 12: raise ToolError("preview_images must be between 0 and 12")
        payload = search_payload(service, query, person, place, date_from, date_to, media_type, limit)
        images = []
        for row in payload["results"][:preview_images]:
            try: images.append(ImageContent(type="image", mime_type="image/jpeg", data=base64.b64encode(service.jpeg(row["uuid"], MODEL_IMAGE_EDGE)).decode()))
            except (ToolError, OSError): break
        return CallToolResult(content=[TextContent(type="text", text=search_text(payload, len(images))), *images], structuredContent=payload)

    @apps.tool(resource_uri=GALLERY_URI, visibility=["app"], name="photo_thumbnails", annotations=read_only, structured_output=True,
               description="Gallery-only: small JPEG data URLs for up to 60 result UUIDs.")
    def photo_thumbnails(uuids: list[str]) -> dict[str, Any]:
        if len(uuids) > 60: raise ToolError("at most 60 thumbnails per call")
        thumbnails = {}
        for identifier in map(canonical, uuids):
            try: thumbnails[identifier] = "data:image/jpeg;base64," + base64.b64encode(service.jpeg(identifier, GALLERY_IMAGE_EDGE)).decode()
            except ToolError: thumbnails[identifier] = None
        return {"thumbnails": thumbnails}

    @apps.tool(resource_uri=GALLERY_URI, visibility=["model", "app"], name="open_in_photos", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
               description="Show one photo in the Apple Photos app on this Mac. It changes nothing in the library.")
    def open_in_photos(uuid: str) -> dict[str, Any]:
        return service.open(canonical(uuid))

    server = MCPServer(name="findstill", title="Findstill", version="0.3.0", extensions=[apps],
                       instructions="Local Apple Photos search by visual description. Look at returned previews before claiming a match; "
                                    "names of people come from Photos metadata. Use photo_filters to discover exact person and place names.")

    @server.tool(name="get_photo", annotations=read_only, structured_output=True,
                 description="Metadata and a larger preview of one photo by UUID, for a closer look.")
    def get_photo(uuid: str) -> dict[str, Any]:
        identifier = canonical(uuid)
        row = service.json(f"/api/assets/{identifier}")
        meta = {k: row.get(k) for k in ("uuid", "filename", "date", "persons", "place", "media_type")}
        image = ImageContent(type="image", mime_type="image/jpeg", data=base64.b64encode(service.jpeg(identifier, 1024)).decode())
        return CallToolResult(content=[TextContent(type="text", text=describe(meta)), image], structuredContent=meta)

    @server.tool(name="photo_filters", annotations=read_only, structured_output=True,
                 description="Exact person and place names available as search filters.")
    def photo_filters() -> dict[str, Any]:
        return service.json("/api/options")

    @server.tool(name="photo_status", annotations=read_only, structured_output=True,
                 description="Index coverage and freshness: how many photos are searchable, failures, and whether indexing is running.")
    def photo_status() -> dict[str, Any]:
        data = service.json("/api/status")
        return {k: data.get(k) for k in ("indexed", "library_total", "failed", "indexing", "indexed_at", "error", "model", "preview_only")}

    return server


def main():
    parser = argparse.ArgumentParser(description="Findstill MCP server (stdio)")
    parser.add_argument("--url", default=os.environ.get("FINDSTILL_URL", DEFAULT_URL), help="Local Findstill service")
    args = parser.parse_args()
    # Request logs would copy photo identifiers into the host's MCP log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    create_server(Service(args.url)).run("stdio")


if __name__ == "__main__": main()
