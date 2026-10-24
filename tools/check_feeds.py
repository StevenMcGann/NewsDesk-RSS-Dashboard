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

# Some sites (cisa.gov's Akamai, for one) refuse cloud datacenter addresses outright. From a
# CI runner that says nothing about whether the feed works for a user, so it's a warning.
REFUSED = {403, 429}


def flatten(nodes, folder=""):
    for node in nodes:
        if node["type"] == "folder":
            yield from flatten(node["children"], node["title"])
        else:
            yield folder, node["title"], node["url"]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # a Windows CI console is cp1252
    source =Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "starter-feeds.opml"
    rows, failed, refused = [], 0, 0
    fetcher = Fetcher()
    try:
        for folder, title, url in flatten(opml.parse(source.read_bytes())):
            started = time.monotonic()
            try:
                r = fetcher.get(url)
                _, entries = feeds.parse(r.body, r.url, r.content_type)
                result = f"{len(entries)} articles via {r.via}" if entries else "no articles"
                mark = "✅" if entries else "❌"
            except FetchError as e:
                result = str(e) + (" from this network" if e.status in REFUSED else "")
                mark = "⚠️" if e.status in REFUSED else "❌"
            except feeds.FeedError as e:
                result, mark = str(e), "❌"
            failed += mark == "❌"
            refused += mark == "⚠️"
            rows.append(f"| {mark} | {folder} | {title} | {result} | {time.monotonic() - started:.1f}s |")
    finally:
        fetcher.close()
    total = f"{len(rows) - failed - refused} of {len(rows)} feeds OK"
    if refused:
        total += f"; {refused} refused this network (⚠️, not counted as failures)"
    report = "\n".join(["## Starter feeds", "", "| | Folder | Feed | Result | Time |", "|---|---|---|---|---|", *rows, "",
                        total + "."])
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
