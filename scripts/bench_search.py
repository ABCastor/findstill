"""Warm search latency against a running Findstill service: p50/p95 in milliseconds.

Usage: python scripts/bench_search.py [--url URL] [--rounds N]
Queries are generic and contain nothing from any library. Output is JSON only.
"""
import argparse
import json
import statistics
import time

import httpx

QUERIES = [
    "beach at sunset", "a dog on a sofa", "snow on mountains", "birthday cake", "airplane cockpit",
    "city street at night", "a plate of pasta", "people dancing", "a red car", "forest path",
    "someone laughing", "a boat on the sea", "train station", "a cat sleeping", "fireworks",
    "a bicycle", "a museum painting", "rain on a window", "a mountain lake", "a crowded concert",
    "landing", "checklist", "grazie", "flaps", "ciao a tutti", "runway", "menu", "welcome",
]


def percentile(values, share):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18473")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--limit", type=int, default=60)
    args = parser.parse_args()
    wall, server = [], []
    with httpx.Client(base_url=args.url, timeout=600, trust_env=False) as client:
        for query in QUERIES[:3]:  # warm the model and caches
            client.get("/api/search", params={"q": query, "limit": args.limit}).raise_for_status()
        for _ in range(args.rounds):
            for query in QUERIES:
                started = time.perf_counter()
                response = client.get("/api/search", params={"q": query, "limit": args.limit})
                response.raise_for_status()
                wall.append((time.perf_counter() - started) * 1000)
                server.append(response.json().get("elapsed_ms") or 0)
        status = client.get("/api/status").json()
    print(json.dumps({
        "requests": len(wall), "limit": args.limit,
        "indexed_assets": status.get("indexed"), "videos_indexed": (status.get("video") or {}).get("done"),
        "video_frames": (status.get("video") or {}).get("frames"),
        "text_segments": sum((status.get("video") or {}).get(k) or 0 for k in ("said_segments", "on_screen_segments")),
        "wall_ms": {"p50": round(statistics.median(wall), 1), "p95": round(percentile(wall, .95), 1), "max": round(max(wall), 1)},
        "server_ms": {"p50": round(statistics.median(server), 1), "p95": round(percentile(server, .95), 1)},
    }, indent=2))


if __name__ == "__main__":
    main()
