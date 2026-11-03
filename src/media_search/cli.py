import argparse
import json
from pathlib import Path
import sys
import subprocess
import httpx
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Local Apple Photos search for people and agents")
    parser.add_argument("--url", default="http://127.0.0.1:18473")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("options")
    sub.add_parser("prepare-model", help="Prepare pinned model weights on CPU; allows their download, never reads Photos")
    reader = sub.add_parser("read-page", help="Read printed text locally with Apple Vision; returns raw lines, not logbook entries")
    reader.add_argument("image_path")
    installation = sub.add_parser("install")
    installation.add_argument("--photos-python", required=True)
    installation.add_argument("--port", type=int, default=18473)
    installation.add_argument("--photos-helper-app", help="Existing signed Apple Photos helper app for background-service reopening")
    installation.add_argument("--photos-library", help="Explicit Photos library bundle for unattended metadata refresh")
    indexing = sub.add_parser("index")
    indexing.add_argument("--batch-size", type=int, default=32)
    indexing.add_argument("--limit", type=int, help="Embed at most this many pending items; metadata is synchronized for the full library")
    videos = sub.add_parser("index-videos", help="Search inside videos: fetch each original once (iCloud download), analyse it on device, delete it")
    videos.add_argument("--limit", type=int, help="Process at most this many pending videos")
    videos.add_argument("--uuid", action="append", help="Only this video (repeatable)")
    videos.add_argument("--retry-failed", action="store_true", help="Retry videos that failed three times")
    videos.add_argument("--normal-priority", action="store_true", help="Do not lower CPU and disk priority")
    video_settings = sub.add_parser("video-search", help="Show or change in-video search settings")
    video_settings.add_argument("mode", nargs="?", choices=("on", "off"), help="Include videos in the scheduled index refresh")
    video_settings.add_argument("--locales", help="Speech locales to try, comma separated, e.g. it-IT,en-US")
    video_settings.add_argument("--fetcher", choices=("helper", "osxphotos"), help="How originals are fetched from Photos")
    video_settings.add_argument("--min-free-gb", type=float, help="Pause indexing below this much free disk space")
    server = sub.add_parser("serve")
    server.add_argument("--port", type=int, default=18473)
    search = sub.add_parser("search")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--limit", type=int, default=20)
    for name in ("person", "place", "date-from", "date-to", "media-type"): search.add_argument("--"+name, default="")
    get = sub.add_parser("asset")
    get.add_argument("uuid")
    get.add_argument("--preview-path", action="store_true", help="Include a local source preview path for an agent to inspect")
    opening = sub.add_parser("open")
    opening.add_argument("uuid")
    exporting = sub.add_parser("export", help="Export one selected Photos resource into a new private directory")
    exporting.add_argument("uuid")
    exporting.add_argument("--out", required=True)
    exporting.add_argument("--download-missing", action="store_true", help="Allow Photos to download this selected resource from iCloud")
    args = parser.parse_args()
    try:
        if args.command == "prepare-model":
            from .model import Encoder
            from .config import MODEL, REVISION
            Encoder(device="cpu", allow_download=True)
            print(json.dumps({"model": MODEL, "revision": REVISION, "cache_ready": True}, indent=2))
            return
        if args.command == "read-page":
            from .ocr import read_page
            print(json.dumps(read_page(args.image_path), ensure_ascii=False, indent=2))
            return
        if args.command == "export":
            from .export import export_asset
            print(json.dumps(export_asset(args.uuid, args.out, args.download_missing), indent=2))
            return
        if args.command == "install":
            from .install import install
            print(json.dumps(install(args.photos_python,args.port,args.photos_helper_app,args.photos_library),indent=2))
            return
        if args.command == "index":
            if args.batch_size < 1 or args.batch_size > 128: parser.error("batch size must be 1-128")
            from .indexer import index
            index(batch_size=args.batch_size, limit=args.limit)
            return
        if args.command == "index-videos":
            from .video import index_videos
            index_videos(Store(), limit=args.limit, uuids=args.uuid, retry_failed=args.retry_failed,
                         low_priority=not args.normal_priority)
            return
        if args.command == "video-search":
            from .config import settings as read_settings, update_settings
            from .video import video_config
            changes = {}
            if args.mode: changes["video_search"] = args.mode == "on"
            if args.locales: changes["video_locales"] = [l.strip() for l in args.locales.split(",") if l.strip()]
            if args.fetcher: changes["video_fetcher"] = args.fetcher
            if args.min_free_gb is not None: changes["video_min_free_gb"] = args.min_free_gb
            config = update_settings(**changes) if changes else read_settings()
            options = video_config(config)
            from .fetch import default_fetcher
            print(json.dumps({"video_search": bool(config.get("video_search")), "locales": options["locales"],
                              "frame_interval_seconds": options["interval"], "fetcher": default_fetcher(config).name,
                              "min_free_gb": options["min_free_gb"]}, indent=2))
            return
        if args.command == "serve":
            import uvicorn
            from .server import create_app
            uvicorn.run(create_app(), host="127.0.0.1", port=args.port, access_log=False)
            return
        with httpx.Client(base_url=args.url, timeout=180, trust_env=False) as client:
            if args.command == "search":
                response = client.get("/api/search", params={"q": args.query, "limit": args.limit, **{n: getattr(args, n) for n in ("person", "place", "date_from", "date_to", "media_type")}})
            elif args.command in ("asset", "open"):
                import uuid
                identifier = str(uuid.UUID(args.uuid)).upper()
                endpoint = f"/api/assets/{identifier}"
                response = client.post(endpoint+"/open", headers={"X-Media-Search-Client": "cli"}) if args.command == "open" else client.get(endpoint)
            else:
                try: response = client.get("/api/"+args.command)
                except httpx.TransportError:
                    if args.command != "status": raise
                    # Indexing progress stays readable while the dashboard service is stopped.
                    print(json.dumps(Store().status() | {"service": "not running"}, ensure_ascii=False, indent=2))
                    return
            response.raise_for_status()
            data = response.json()
        if args.command == "asset" and args.preview_path:
            row = Store().asset(identifier)
            if row:
                paths = [row.get("path"), *(row.get("paths") or [])]
                data["local_preview_path"] = next((p for p in paths if p and Path(p).is_file()), None)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except (httpx.HTTPError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__": main()
