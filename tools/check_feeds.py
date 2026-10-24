"""Check that every feed in starter-feeds.opml still downloads and parses.

Run by the daily CI job; exits non-zero if any feed fails, so GitHub emails the owner.
Usage:  py -3 tools/check_feeds.py [path/to/feeds.opml]
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "app" / "vendor"), str(ROOT / "app")]

import feeds  # noqa: E402
import opml  # noqa: E402
from fetcher import FetchError, Fetcher  # noqa: E402


def flatten(nodes, folder=""):
    for node in nodes:
        if node["type"] == "folder":
            yield from flatten(node["children"], node["title"])
        else:
            yield folder, node["title"], node["url"]


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "starter-feeds.opml"
    rows, failed = [], 0
    fetcher = Fetcher()
    try:
        for folder, title, url in flatten(opml.parse(source.read_bytes())):
            started = time.monotonic()
            try:
                r = fetcher.get(url)
                _, entries = feeds.parse(r.body, r.url, r.content_type)
                result = f"{len(entries)} articles via {r.via}" if entries else "no articles"
                ok = bool(entries)
            except (FetchError, feeds.FeedError) as e:
                result, ok = str(e), False
            failed += not ok
            rows.append(f"| {'✅' if ok else '❌'} | {folder} | {title} | {result} | {time.monotonic() - started:.1f}s |")
    finally:
        fetcher.close()
    report = "\n".join(["## Starter feeds", "", "| | Folder | Feed | Result | Time |", "|---|---|---|---|---|", *rows, "",
                        f"{len(rows) - failed} of {len(rows)} feeds OK."])
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
