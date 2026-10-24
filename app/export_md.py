"""Save an article as a Markdown file with YAML frontmatter."""
import json
import os
import re
import time
from pathlib import Path

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(title: str, published=None, max_len=100) -> str:
    name = " ".join(INVALID_CHARS.sub(" ", title or "").split())
    name = name[:max_len].rstrip(". ") or "Untitled"
    stem = f"{time.strftime('%Y-%m-%d', time.localtime(published or time.time()))} {name}"
    if stem.split(".")[0].upper() in RESERVED:
        stem = "_" + stem
    return stem + ".md"


def tag_slug(name: str) -> str:
    """Obsidian-style tag: no spaces."""
    return re.sub(r"[^\w/-]+", "-", name.strip().lower()).strip("-") or "tag"


def render(item: dict, markdown: str, tags: list, template: str) -> str:
    published = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["published"])) if item.get("published") else ""
    front = {
        "title": item.get("title") or "",
        "url": item.get("url") or "",
        "source": item.get("feed_title") or "",
        "author": item.get("author") or "",
        "published": published,
        "saved": time.strftime("%Y-%m-%d %H:%M"),
    }
    lines = [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items() if v]
    lines.append("tags: " + json.dumps([tag_slug(t) for t in tags], ensure_ascii=False))
    values = dict(front, content=markdown.strip(), tags=" ".join("#" + tag_slug(t) for t in tags))
    # Single pass over the template, so "{{...}}" inside the article text is left alone.
    body = re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body.strip() + "\n"


def save(folder: Path, item: dict, markdown: str, tags: list, template: str) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    root = folder.resolve()
    previous = Path(item["saved_path"]) if item.get("saved_path") else None
    if previous and previous.exists() and previous.resolve().parent == root:
        target = previous  # saving the same article again updates its file
    else:
        name = safe_filename(item.get("title"), item.get("published"))
        target = folder / name
        n = 2
        while target.exists():
            target = folder / f"{name[:-3]} ({n}).md"
            n += 1
    if target.resolve().parent != root:
        raise ValueError("Refusing to write outside the save folder")
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render(item, markdown, tags, template), encoding="utf-8")
    os.replace(tmp, target)
    return target
