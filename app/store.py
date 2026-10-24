"""SQLite storage for folders, feeds, articles and tags.

One connection shared by all threads, serialized with a lock. That is plenty for a
single user and keeps transactions simple.
"""
import sqlite3
import threading
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    collapsed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    site_url TEXT,
    icon_url TEXT,
    etag TEXT,
    last_modified TEXT,
    last_fetched REAL,
    last_error TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    full_text INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    url TEXT,
    title TEXT NOT NULL,
    author TEXT,
    published REAL NOT NULL,
    summary TEXT,
    content TEXT,
    snippet TEXT,
    image_url TEXT,
    content_full TEXT,
    read INTEGER NOT NULL DEFAULT 0,
    starred INTEGER NOT NULL DEFAULT 0,
    saved_path TEXT,
    added REAL NOT NULL,
    last_seen REAL NOT NULL,
    read_at REAL,
    UNIQUE (feed_id, guid)
);
CREATE INDEX IF NOT EXISTS items_by_date ON items (published);
CREATE INDEX IF NOT EXISTS items_by_feed ON items (feed_id, read);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, tag_id)
);
CREATE INDEX IF NOT EXISTS item_tags_by_tag ON item_tags (tag_id);
"""

TAG_COLORS = ["#e5484d", "#3e63dd", "#e2a336", "#30a46c", "#8e4ec6",
              "#f76b15", "#12a594", "#d6409f", "#8b8d98"]
DEFAULT_TAGS = [("Important", "#e5484d"), ("Read later", "#3e63dd"), ("Favorite", "#e2a336")]

# Read items are only pruned once they have also dropped out of their feed, so a
# refresh cannot bring them back as unread.
UNSEEN_GRACE = 3 * 86400

LIST_COLUMNS = """
    i.id, i.feed_id, f.title AS feed_title, f.icon_url AS feed_icon, f.site_url,
    i.title, i.url, i.author, i.published, i.snippet, i.image_url,
    i.read, i.starred, i.saved_path IS NOT NULL AS saved
"""


class NotFound(Exception):
    pass


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.version = 0  # bumped when a refresh or import changes what the user sees
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA synchronous = NORMAL")
            fresh = self.db.execute("PRAGMA user_version").fetchone()[0] == 0
            self.db.executescript(SCHEMA)
            if fresh:
                self.db.executemany("INSERT OR IGNORE INTO tags (name, color) VALUES (?, ?)", DEFAULT_TAGS)
                self.db.execute("PRAGMA user_version = 1")

    def close(self):
        with self.lock:
            self.db.close()

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def _rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def _one(self, sql, args=()):
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    # ---- overview -------------------------------------------------------------

    def state(self) -> dict:
        with self.lock:
            folders = self._rows(
                "SELECT id, name, parent_id, position, collapsed FROM folders "
                "ORDER BY position, name COLLATE NOCASE")
            feeds = self._rows(
                "SELECT f.id, f.folder_id, f.title, f.url, f.site_url, f.icon_url, f.last_error, "
                "f.last_fetched, f.full_text, f.position, "
                "(SELECT COUNT(*) FROM items i WHERE i.feed_id = f.id AND i.read = 0) AS unread "
                "FROM feeds f ORDER BY f.position, f.title COLLATE NOCASE")
            tags = self._rows("SELECT id, name, color FROM tags ORDER BY name COLLATE NOCASE")
            totals = self._one(
                "SELECT COUNT(*) AS items, COALESCE(SUM(read = 0), 0) AS unread, "
                "COALESCE(SUM(starred), 0) AS starred, "
                "COALESCE(SUM(saved_path IS NOT NULL), 0) AS saved FROM items")
        for folder in folders:
            folder["collapsed"] = bool(folder["collapsed"])
        for feed in feeds:
            feed["full_text"] = bool(feed["full_text"])
        return {"folders": folders, "feeds": feeds, "tags": tags, "totals": totals,
                "version": self.version}

    # ---- folders --------------------------------------------------------------

    def _check_folder(self, folder_id):
        if folder_id is not None and not self._one("SELECT id FROM folders WHERE id = ?", (folder_id,)):
            raise NotFound("Folder not found")

    def add_folder(self, name: str, parent_id=None) -> int:
        name = _clean_name(name)
        with self.tx() as db:
            self._check_folder(parent_id)
            pos = db.execute("SELECT COALESCE(MAX(position) + 1, 0) FROM folders WHERE parent_id IS ?",
                             (parent_id,)).fetchone()[0]
            return db.execute("INSERT INTO folders (name, parent_id, position) VALUES (?, ?, ?)",
                              (name, parent_id, pos)).lastrowid

    def update_folder(self, folder_id: int, name=None, collapsed=None):
        with self.tx() as db:
            self._check_folder(folder_id)
            if name is not None:
                db.execute("UPDATE folders SET name = ? WHERE id = ?", (_clean_name(name), folder_id))
            if collapsed is not None:
                db.execute("UPDATE folders SET collapsed = ? WHERE id = ?", (int(bool(collapsed)), folder_id))

    def delete_folder(self, folder_id: int):
        """Delete a folder; its feeds and subfolders move up to its parent."""
        with self.tx() as db:
            row = db.execute("SELECT parent_id FROM folders WHERE id = ?", (folder_id,)).fetchone()
            if not row:
                raise NotFound("Folder not found")
            parent = row["parent_id"]
            db.execute("UPDATE folders SET parent_id = ? WHERE parent_id = ?", (parent, folder_id))
            db.execute("UPDATE feeds SET folder_id = ? WHERE folder_id = ?", (parent, folder_id))
            db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))

    def folder_tree_ids(self, folder_id: int) -> list:
        rows = self._rows(
            "WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL "
            "SELECT f.id FROM folders f JOIN sub ON f.parent_id = sub.id) SELECT id FROM sub",
            (folder_id,))
        return [r["id"] for r in rows]

    def move(self, kind: str, node_id: int, parent_id, index: int):
        """Move a feed or folder under parent_id at position index among same-kind siblings."""
        table, parent_col = {"feed": ("feeds", "folder_id"), "folder": ("folders", "parent_id")}[kind]
        with self.tx() as db:
            if not db.execute(f"SELECT id FROM {table} WHERE id = ?", (node_id,)).fetchone():
                raise NotFound(f"{kind.title()} not found")
            self._check_folder(parent_id)
            if kind == "folder" and parent_id is not None and parent_id in self.folder_tree_ids(node_id):
                raise ValueError("A folder can't be moved inside itself")
            siblings = [r[0] for r in db.execute(
                f"SELECT id FROM {table} WHERE {parent_col} IS ? AND id != ? ORDER BY position, id",
                (parent_id, node_id))]
            index = max(0, min(int(index), len(siblings)))
            siblings.insert(index, node_id)
            db.execute(f"UPDATE {table} SET {parent_col} = ? WHERE id = ?", (parent_id, node_id))
            db.executemany(f"UPDATE {table} SET position = ? WHERE id = ?",
                           [(pos, sid) for pos, sid in enumerate(siblings)])

    # ---- feeds ----------------------------------------------------------------

    def feed(self, feed_id: int) -> dict:
        row = self._one("SELECT * FROM feeds WHERE id = ?", (feed_id,))
        if not row:
            raise NotFound("Feed not found")
        return row

    def feed_by_url(self, url: str):
        return self._one("SELECT * FROM feeds WHERE url = ?", (url,))

    def add_feed(self, url, title, folder_id=None, site_url=None, icon_url=None) -> int:
        url = _feed_url(url)
        site_url, icon_url = _web_url(site_url), _web_url(icon_url)
        with self.tx() as db:
            self._check_folder(folder_id)
            if db.execute("SELECT id FROM feeds WHERE url = ?", (url,)).fetchone():
                raise ValueError("You already follow this feed")
            pos = db.execute("SELECT COALESCE(MAX(position) + 1, 0) FROM feeds WHERE folder_id IS ?",
                             (folder_id,)).fetchone()[0]
            return db.execute(
                "INSERT INTO feeds (url, title, folder_id, site_url, icon_url, position) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (url, _clean_name(title or url), folder_id, site_url, icon_url, pos)).lastrowid

    def update_feed(self, feed_id: int, **changes):
        with self.tx() as db:
            self.feed(feed_id)
            if "title" in changes:
                db.execute("UPDATE feeds SET title = ? WHERE id = ?", (_clean_name(changes["title"]), feed_id))
            if "url" in changes:
                url = _feed_url(changes["url"])
                other = db.execute("SELECT id FROM feeds WHERE url = ? AND id != ?", (url, feed_id)).fetchone()
                if other:
                    raise ValueError("Another feed already uses that address")
                db.execute("UPDATE feeds SET url = ?, etag = NULL, last_modified = NULL, last_error = NULL "
                           "WHERE id = ?", (url, feed_id))
            if "full_text" in changes:
                db.execute("UPDATE feeds SET full_text = ? WHERE id = ?", (int(bool(changes["full_text"])), feed_id))
        if "folder_id" in changes:
            self.move("feed", feed_id, changes["folder_id"], 1 << 30)

    def delete_feed(self, feed_id: int):
        with self.tx() as db:
            if not db.execute("DELETE FROM feeds WHERE id = ?", (feed_id,)).rowcount:
                raise NotFound("Feed not found")

    def feeds_for_fetch(self, ids=None) -> list:
        sql = "SELECT id, url, title, etag, last_modified FROM feeds"
        if ids:
            ids = [int(i) for i in ids]
            return self._rows(f"{sql} WHERE id IN ({','.join('?' * len(ids))})", ids)
        return self._rows(sql)

    def record_fetch(self, feed_id, meta: dict, entries: list, etag=None, last_modified=None, now=None) -> int:
        """Store a successful fetch. Returns the number of new articles."""
        now = now or time.time()
        with self.tx() as db:
            before = db.execute("SELECT COUNT(*) FROM items WHERE feed_id = ?", (feed_id,)).fetchone()[0]
            had_error = db.execute("SELECT last_error IS NOT NULL FROM feeds WHERE id = ?",
                                   (feed_id,)).fetchone()
            if had_error is None:
                return 0  # feed was deleted while it was being fetched
            for e in entries:
                db.execute(
                    "INSERT INTO items (feed_id, guid, url, title, author, published, summary, content, "
                    "snippet, image_url, added, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (feed_id, guid) DO UPDATE SET last_seen = excluded.last_seen, "
                    "title = excluded.title, url = excluded.url, author = excluded.author, "
                    "summary = excluded.summary, content = excluded.content, snippet = excluded.snippet, "
                    "image_url = COALESCE(excluded.image_url, items.image_url)",
                    (feed_id, e["guid"], e["url"], e["title"], e["author"], e["published"] or now,
                     e["summary"], e["content"], e["snippet"], e["image_url"], now, now))
            after = db.execute("SELECT COUNT(*) FROM items WHERE feed_id = ?", (feed_id,)).fetchone()[0]
            db.execute(
                "UPDATE feeds SET etag = ?, last_modified = ?, last_fetched = ?, last_error = NULL, "
                "site_url = COALESCE(site_url, ?), icon_url = COALESCE(icon_url, ?) WHERE id = ?",
                (etag, last_modified, now, _web_url(meta.get("site_url")), _web_url(meta.get("icon_url")), feed_id))
            new = after - before
            if new or had_error[0]:
                self.version += 1
            return new

    def record_not_modified(self, feed_id, now=None):
        now = now or time.time()
        with self.tx() as db:
            had_error = db.execute("SELECT last_error IS NOT NULL FROM feeds WHERE id = ?", (feed_id,)).fetchone()
            db.execute("UPDATE feeds SET last_fetched = ?, last_error = NULL WHERE id = ?", (now, feed_id))
            # Items from the last full fetch are still in the feed.
            db.execute("UPDATE items SET last_seen = ? WHERE feed_id = ? AND last_seen = "
                       "(SELECT MAX(last_seen) FROM items WHERE feed_id = ?)", (now, feed_id, feed_id))
            if had_error and had_error[0]:
                self.version += 1

    def record_error(self, feed_id, message: str, now=None):
        with self.tx() as db:
            row = db.execute("SELECT last_error FROM feeds WHERE id = ?", (feed_id,)).fetchone()
            db.execute("UPDATE feeds SET last_error = ?, last_fetched = ? WHERE id = ?",
                       (message[:500], now or time.time(), feed_id))
            if row and row[0] != message:
                self.version += 1

    # ---- articles -------------------------------------------------------------

    def _where(self, f: dict):
        where, args = [], []
        view = str(f.get("view") or "all")
        if view == "starred":
            where.append("i.starred = 1")
        elif view == "saved":
            where.append("i.saved_path IS NOT NULL")
        elif view.startswith("feed:"):
            where.append("i.feed_id = ?")
            args.append(int(view[5:]))
        elif view.startswith("folder:"):
            ids = self.folder_tree_ids(int(view[7:]))
            where.append(f"f.folder_id IN ({','.join('?' * len(ids))})")
            args += ids
        status = f.get("status") or "all"
        if status == "unread":
            where.append("i.read = 0")
        elif status == "read":
            where.append("i.read = 1")
        if f.get("starred"):
            where.append("i.starred = 1")
        tag_ids = [int(t) for t in f.get("tags") or []]
        if tag_ids:
            where.append(f"i.id IN (SELECT item_id FROM item_tags WHERE tag_id IN ({','.join('?' * len(tag_ids))}))")
            args += tag_ids
        if f.get("since"):
            where.append("i.published >= ?")
            args.append(float(f["since"]))
        if f.get("until"):
            where.append("i.published <= ?")
            args.append(float(f["until"]))
        for word in str(f.get("q") or "").split()[:8]:
            like = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(i.title LIKE ? ESCAPE '\\' OR i.snippet LIKE ? ESCAPE '\\' "
                         "OR f.title LIKE ? ESCAPE '\\')")
            args += [like, like, like]
        return (" WHERE " + " AND ".join(where)) if where else "", args

    def list_items(self, filters: dict, sort="new", page=1, size=20) -> dict:
        where, args = self._where(filters)
        order = "DESC" if sort != "old" else "ASC"
        size = max(1, min(int(size), 200))
        page = max(1, int(page))
        base = f"FROM items i JOIN feeds f ON f.id = i.feed_id{where}"
        with self.lock:
            total = self.db.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
            items = self._rows(
                f"SELECT {LIST_COLUMNS} {base} ORDER BY i.published {order}, i.id {order} LIMIT ? OFFSET ?",
                args + [size, (page - 1) * size])
            self._attach_tags(items)
        for item in items:
            item["read"], item["starred"], item["saved"] = bool(item["read"]), bool(item["starred"]), bool(item["saved"])
        return {"items": items, "total": total, "page": page, "size": size}

    def _attach_tags(self, items):
        by_id = {item["id"]: item for item in items}
        for item in items:
            item["tags"] = []
        if not by_id:
            return
        ids = list(by_id)
        for row in self.db.execute(
                f"SELECT item_id, tag_id FROM item_tags WHERE item_id IN ({','.join('?' * len(ids))}) "
                "ORDER BY tag_id", ids):
            by_id[row[0]]["tags"].append(row[1])

    def item(self, item_id: int) -> dict:
        with self.lock:
            row = self._one(
                "SELECT i.*, f.title AS feed_title, f.icon_url AS feed_icon, f.site_url, "
                "f.full_text AS feed_full_text FROM items i JOIN feeds f ON f.id = i.feed_id WHERE i.id = ?",
                (item_id,))
            if not row:
                raise NotFound("Article not found")
            self._attach_tags([row])
        for key in ("read", "starred", "feed_full_text"):
            row[key] = bool(row[key])
        row["saved"] = row["saved_path"] is not None
        return row

    def update_item(self, item_id: int, read=None, starred=None, tags=None):
        with self.tx() as db:
            if not db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone():
                raise NotFound("Article not found")
            if read is not None:
                db.execute("UPDATE items SET read = ?, read_at = ? WHERE id = ?",
                           (int(bool(read)), time.time() if read else None, item_id))
            if starred is not None:
                db.execute("UPDATE items SET starred = ? WHERE id = ?", (int(bool(starred)), item_id))
            if tags is not None:
                db.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
                db.executemany("INSERT OR IGNORE INTO item_tags (item_id, tag_id) "
                               "SELECT ?, id FROM tags WHERE id = ?", [(item_id, int(t)) for t in tags])

    def mark_read(self, ids=None, filters=None, read=True) -> int:
        """Mark specific ids, or everything matching filters, as read/unread."""
        stamp = time.time() if read else None
        with self.tx() as db:
            if ids is not None:
                ids = [int(i) for i in ids][:5000]
                if not ids:
                    return 0
                return db.execute(
                    f"UPDATE items SET read = ?, read_at = ? WHERE read != ? AND id IN ({','.join('?' * len(ids))})",
                    [int(read), stamp, int(read)] + ids).rowcount
            where, args = self._where(filters or {})
            return db.execute(
                f"UPDATE items SET read = ?, read_at = ? WHERE read != ? AND id IN "
                f"(SELECT i.id FROM items i JOIN feeds f ON f.id = i.feed_id{where})",
                [int(read), stamp, int(read)] + args).rowcount

    def set_full_content(self, item_id: int, html: str):
        with self.tx() as db:
            if not db.execute("UPDATE items SET content_full = ? WHERE id = ?", (html, item_id)).rowcount:
                raise NotFound("Article not found")

    def set_saved(self, item_id: int, path: str):
        with self.tx() as db:
            db.execute("UPDATE items SET saved_path = ? WHERE id = ?", (path, item_id))

    def prune(self, retention_days: int, now=None) -> int:
        """Drop old read articles nobody kept (starred, saved or tagged)."""
        now = now or time.time()
        with self.tx() as db:
            return db.execute(
                "DELETE FROM items WHERE read = 1 AND starred = 0 AND saved_path IS NULL "
                "AND published < ? AND last_seen < ? "
                "AND id NOT IN (SELECT item_id FROM item_tags)",
                (now - retention_days * 86400, now - UNSEEN_GRACE)).rowcount

    # ---- tags -----------------------------------------------------------------

    def add_tag(self, name: str, color=None) -> int:
        name = _clean_name(name, 40)
        with self.tx() as db:
            if db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone():
                raise ValueError("A tag with that name already exists")
            count = db.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
            color = _clean_color(color) or TAG_COLORS[count % len(TAG_COLORS)]
            return db.execute("INSERT INTO tags (name, color) VALUES (?, ?)", (name, color)).lastrowid

    def update_tag(self, tag_id: int, name=None, color=None):
        with self.tx() as db:
            if not db.execute("SELECT id FROM tags WHERE id = ?", (tag_id,)).fetchone():
                raise NotFound("Tag not found")
            if name is not None:
                name = _clean_name(name, 40)
                if db.execute("SELECT id FROM tags WHERE name = ? AND id != ?", (name, tag_id)).fetchone():
                    raise ValueError("A tag with that name already exists")
                db.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag_id))
            if color is not None:
                if not _clean_color(color):
                    raise ValueError("Invalid color")
                db.execute("UPDATE tags SET color = ? WHERE id = ?", (color, tag_id))

    def delete_tag(self, tag_id: int):
        with self.tx() as db:
            if not db.execute("DELETE FROM tags WHERE id = ?", (tag_id,)).rowcount:
                raise NotFound("Tag not found")

    def tag_names(self, ids) -> list:
        ids = [int(i) for i in ids]
        if not ids:
            return []
        return [r["name"] for r in self._rows(
            f"SELECT name FROM tags WHERE id IN ({','.join('?' * len(ids))}) ORDER BY name COLLATE NOCASE", ids)]

    # ---- OPML -----------------------------------------------------------------

    def export_tree(self) -> list:
        folders = self._rows("SELECT id, name, parent_id FROM folders ORDER BY position, name COLLATE NOCASE")
        feeds = self._rows("SELECT title, url, site_url, folder_id FROM feeds ORDER BY position, title COLLATE NOCASE")

        def build(parent):
            nodes = [{"type": "folder", "title": f["name"], "children": build(f["id"])}
                     for f in folders if f["parent_id"] == parent]
            nodes += [{"type": "feed", "title": f["title"], "url": f["url"], "site_url": f["site_url"]}
                      for f in feeds if f["folder_id"] == parent]
            return nodes

        return build(None)

    def import_tree(self, nodes: list, parent_id=None) -> dict:
        """Add folders and feeds from an OPML tree, reusing same-named folders and skipping known feeds."""
        result = {"folders": 0, "feeds": 0, "skipped": 0, "feed_ids": []}

        def walk(nodes, parent):
            for node in nodes:
                if node["type"] == "folder":
                    row = self._one("SELECT id FROM folders WHERE parent_id IS ? AND name = ? COLLATE NOCASE",
                                    (parent, node["title"]))
                    if row:
                        folder_id = row["id"]
                    else:
                        folder_id = self.add_folder(node["title"], parent)
                        result["folders"] += 1
                    walk(node["children"], folder_id)
                elif self.feed_by_url(node["url"]):
                    result["skipped"] += 1
                else:
                    result["feed_ids"].append(self.add_feed(node["url"], node["title"], parent, node.get("site_url")))
                    result["feeds"] += 1

        with self.lock:
            walk(nodes, parent_id)
        if result["feeds"] or result["folders"]:
            self.version += 1
        return result


def _clean_name(name, limit=200) -> str:
    name = " ".join(str(name or "").split())[:limit]
    if not name:
        raise ValueError("Name can't be empty")
    return name


def _web_url(url):
    """The URL if it is http(s), else None. Stored links end up in href and window.open."""
    url = str(url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) and len(url) <= 2048 else None


def _feed_url(url) -> str:
    checked = _web_url(url)
    if not checked:
        raise ValueError("Feed addresses must start with http:// or https://")
    return checked


def _clean_color(color):
    if isinstance(color, str) and len(color) == 7 and color[0] == "#":
        try:
            int(color[1:], 16)
            return color
        except ValueError:
            pass
    return None
