"""Turn fetched bytes into feed metadata and normalized articles."""
import calendar
import codecs
import hashlib
import html
import json
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import feedparser

MAX_ENTRIES = 300
SNIPPET_CHARS = 300
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json",
              "application/rdf+xml", "application/xml", "text/xml")
COMMON_FEED_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml")
TRACKER_HINTS = ("feedburner.com/~", "pixel", "tracking", "doubleclick", "/stats?", "gravatar.com/avatar")


class FeedError(Exception):
    pass


def parse(body: bytes, url: str, content_type: str = ""):
    """Return (meta, entries) or raise FeedError if the body isn't a feed."""
    if _looks_like_json(body, content_type):
        return _parse_json_feed(body, url)
    d = feedparser.parse(body, response_headers={
        "content-location": url,
        "content-type": content_type or "application/xml",
    })
    if not d.get("version") and not d.entries:
        raise FeedError("This address didn't return a feed")
    feed = d.feed
    site = _absolute(url, feed.get("link")) or _origin(url)
    meta = {
        "title": text(feed.get("title")) or urlsplit(url).hostname or url,
        "site_url": site,
        "icon_url": _absolute(url, feed.get("icon")) or urljoin(_origin(site), "/favicon.ico"),
    }
    entries = [_entry(e, url) for e in d.entries[:MAX_ENTRIES]]
    return meta, entries


def _entry(e, base: str) -> dict:
    link = _absolute(base, e.get("link"))
    title = text(e.get("title")) or "(untitled)"
    content = ""
    for c in e.get("content") or []:
        if len(c.get("value") or "") > len(content):
            content = c["value"]
    summary = e.get("summary") or ""
    if summary == content:
        summary = ""
    published = _timestamp(e.get("published_parsed") or e.get("updated_parsed") or e.get("created_parsed"))
    guid = e.get("id") or link or _hash(title, summary or content)
    return {
        "guid": guid[:1000],
        "url": link,
        "title": title[:1000],
        "author": text(e.get("author"))[:200] or None,
        "published": published,
        "summary": summary,
        "content": content,
        "snippet": snippet(summary or content),
        "image_url": _image(e, content or summary, link or base),
    }


def _image(e, body_html: str, base: str):
    for thumb in e.get("media_thumbnail") or []:
        if _http(thumb.get("url")):
            return thumb["url"]
    for media in e.get("media_content") or []:
        url, kind, medium = media.get("url"), media.get("type") or "", media.get("medium") or ""
        if _http(url) and (medium == "image" or kind.startswith("image/")
                           or (not kind and not medium and re.search(r"\.(jpe?g|png|gif|webp|avif)(\?|$)", url, re.I))):
            return url
    for enc in e.get("enclosures") or []:
        if (enc.get("type") or "").startswith("image/") and _http(enc.get("href")):
            return enc["href"]
    finder = _ImageFinder()
    try:
        finder.feed(body_html[:200_000])
    except Exception:
        pass
    return _http(urljoin(base, finder.src)) if finder.src else None


class _ImageFinder(HTMLParser):
    """Finds the first real image in article HTML, skipping tracking pixels."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.src = None

    def handle_starttag(self, tag, attrs):
        if self.src or tag != "img":
            return
        a = dict(attrs)
        src = a.get("src") or a.get("data-src") or ""
        if not src or src.startswith("data:") or any(h in src.lower() for h in TRACKER_HINTS):
            return
        if a.get("width") in ("0", "1", "2") or a.get("height") in ("0", "1", "2"):
            return
        self.src = src


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "head", "title"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "figcaption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def text(value) -> str:
    """Plain text from a string that may contain HTML or entities."""
    if not value:
        return ""
    value = str(value)
    if "<" in value:
        extractor = _TextExtractor()
        try:
            extractor.feed(value)
            extractor.close()
            value = "".join(extractor.parts)
        except Exception:
            value = re.sub(r"<[^>]*>", " ", value)
    return " ".join(html.unescape(value).split())


def snippet(markup: str) -> str:
    plain = text(markup[:50_000])
    if len(plain) <= SNIPPET_CHARS:
        return plain
    return plain[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"


def _timestamp(struct):
    if not struct:
        return None
    try:
        ts = calendar.timegm(struct)
    except (TypeError, ValueError, OverflowError):
        return None
    if ts > time.time() + 86400 or ts < 0:  # future or nonsense dates sort as "now"
        return None
    return float(ts)


def _hash(*parts) -> str:
    return "sha1:" + hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()


def _http(url):
    if url and urlsplit(url).scheme in ("http", "https"):
        return url
    return None


def _absolute(base: str, url):
    """Resolve a possibly relative http(s) URL; None when missing or another scheme."""
    return _http(urljoin(base, str(url).strip())) if url and str(url).strip() else None


def _origin(url: str) -> str:
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}/" if parts.scheme and parts.netloc else ""


# ---- JSON Feed (jsonfeed.org) ---------------------------------------------------

def _looks_like_json(body: bytes, content_type: str) -> bool:
    return "json" in content_type.lower() or body.lstrip()[:1] == b"{"


def _parse_json_feed(body: bytes, url: str):
    try:
        data = json.loads(body.decode("utf-8-sig", "replace"))
    except ValueError:
        raise FeedError("This address didn't return a feed") from None
    if not isinstance(data, dict) or "jsonfeed.org" not in str(data.get("version", "")):
        raise FeedError("This address didn't return a feed")
    site = _http(data.get("home_page_url")) or _origin(url)
    meta = {
        "title": text(data.get("title")) or urlsplit(url).hostname or url,
        "site_url": site,
        "icon_url": _http(data.get("favicon")) or urljoin(_origin(site), "/favicon.ico"),
    }
    entries = []
    for item in (data.get("items") or [])[:MAX_ENTRIES]:
        if not isinstance(item, dict):
            continue
        link = _absolute(url, item.get("url") or item.get("external_url"))
        title = text(item.get("title")) or "(untitled)"
        content = item.get("content_html") or ""
        if not content and item.get("content_text"):
            content = "".join(f"<p>{html.escape(p)}</p>" for p in str(item["content_text"]).split("\n\n"))
        summary = str(item.get("summary") or "")
        authors = item.get("authors") or ([item["author"]] if isinstance(item.get("author"), dict) else [])
        author = text(authors[0].get("name")) if authors and isinstance(authors[0], dict) else ""
        entries.append({
            "guid": str(item.get("id") or link or _hash(title, content))[:1000],
            "url": link,
            "title": title[:1000],
            "author": author[:200] or None,
            "published": _iso_timestamp(item.get("date_published") or item.get("date_modified")),
            "summary": html.escape(summary) if summary else "",
            "content": content,
            "snippet": snippet(summary or content),
            "image_url": _http(item.get("image")) or _http(item.get("banner_image")) or _image({}, content, link or url),
        })
    return meta, entries


def _iso_timestamp(value):
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return ts if 0 < ts < time.time() + 86400 else None


# ---- web pages ------------------------------------------------------------------

class _LinkFinder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k: (v or "") for k, v in attrs}
        rel = a.get("rel", "").lower().split()
        kind = a.get("type", "").lower().split(";")[0].strip()
        if "alternate" in rel and kind in FEED_TYPES and a.get("href"):
            self.links.append((a["href"], a.get("title", "")))


def discover(body: bytes, base: str) -> list:
    """Feed URLs advertised by an HTML page, best first, plus common fallbacks."""
    finder = _LinkFinder()
    try:
        finder.feed(decode_html(body[:1_000_000], "")[:1_000_000])
    except Exception:
        pass
    found = []
    for href, title in finder.links:
        url = _http(urljoin(base, href))
        if url and url not in found:
            found.append((url, "comment" in title.lower()))
    ordered = [u for u, comments in found if not comments] + [u for u, comments in found if comments]
    origin = _origin(base)
    for path in COMMON_FEED_PATHS:
        guess = urljoin(origin, path)
        if guess not in ordered:
            ordered.append(guess)
    return ordered


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I)


def decode_html(body: bytes, content_type: str) -> str:
    """Decode a web page using the HTTP charset, then <meta charset>, then UTF-8."""
    candidates = []
    m = re.search(r"charset\s*=\s*[\"']?([\w\-]+)", content_type or "", re.I)
    if m:
        candidates.append(m.group(1))
    m = _META_CHARSET.search(body[:8192])
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    for name in candidates:
        try:
            codecs.lookup(name)
            return body.decode(name, "replace")
        except LookupError:
            continue
    return body.decode("utf-8", "replace")
