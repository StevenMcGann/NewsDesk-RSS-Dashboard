# Third-party components

All are copied unmodified (icons: path data only) and pinned to the versions below.

| Component | Version | License | Location |
|---|---|---|---|
| Python (embeddable, bundled in the zip) | 3.14.10 | PSF-2.0 | `python/` in the release ZIP |
| feedparser | 6.0.14 | BSD-2-Clause | `app/vendor/feedparser/` |
| feedparser-sgmllib | 2.1.0 | PSF-2.0 | `app/vendor/feedparser_sgmllib/` |
| DOMPurify | 3.4.17 | Apache-2.0 or MPL-2.0 | `app/static/vendor/purify.min.js` |
| @mozilla/readability | 0.6.0 | Apache-2.0 | `app/static/vendor/Readability.js` |
| Turndown | 7.2.4 | MIT | `app/static/vendor/turndown.js` |
| Lucide icons | 1.54.0 | ISC | `app/static/icons.js` |

License texts are next to each component (`LICENSE-*.txt`).

Python's license is included as `python/LICENSE.txt` in the release ZIP. Python 3.14.10
was adopted on October 30, 2026 as an explicitly authorized security exception to the
normal 14-day waiting period. It is an expedited security release that blocks
compromised eMudhra intermediate CA keys (CVE-2026-115557) and includes other security
fixes. See the [upstream release notes](https://www.python.org/downloads/release/python-31410/).

`app/feeds.py` wraps two feedparser methods at runtime, `_add_tag` and `_sync_author_detail`, to bound their cost on hostile feeds. The vendored feedparser files themselves are unchanged. See "Feed parsing" in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Design credit

The layout and workflow (sidebar, card, list and feed layouts, grouping, and the reader
pane) are modeled on
[RSS Dashboard for Obsidian](https://github.com/amatya-aditya/obsidian-rss-dashboard)
by amatya-aditya (MIT License). No code from that project is included.
