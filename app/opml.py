"""OPML import and export, keeping nested folders."""
import xml.etree.ElementTree as ET
from email.utils import formatdate


def parse(data: bytes) -> list:
    """OPML bytes -> tree of {"type": "folder"|"feed", ...} nodes."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"That file isn't valid OPML ({e})") from None
    body = root.find("body")
    if root.tag.lower() != "opml" or body is None:
        raise ValueError("That file isn't an OPML feed list")
    return [n for n in (_node(o) for o in body.findall("outline")) if n]


def _node(outline):
    url = (outline.get("xmlUrl") or outline.get("xmlurl") or "").strip()
    title = (outline.get("title") or outline.get("text") or url or "Untitled").strip()
    if url:
        if not url.lower().startswith(("http://", "https://")):
            return None
        site = (outline.get("htmlUrl") or outline.get("htmlurl") or "").strip()
        if not site.lower().startswith(("http://", "https://")):
            site = None  # a "javascript:" site link would end up in window.open
        return {"type": "feed", "title": title, "url": url, "site_url": site}
    children = [n for n in (_node(o) for o in outline.findall("outline")) if n]
    return {"type": "folder", "title": title, "children": children} if children else None


def build(tree: list, title="NewsDesk feeds") -> bytes:
    root = ET.Element("opml", version="2.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = title
    ET.SubElement(head, "dateCreated").text = formatdate(usegmt=True)
    body = ET.SubElement(root, "body")

    def add(parent, nodes):
        for node in nodes:
            if node["type"] == "folder":
                add(ET.SubElement(parent, "outline", text=node["title"], title=node["title"]), node["children"])
            else:
                attrs = {"type": "rss", "text": node["title"], "title": node["title"], "xmlUrl": node["url"]}
                if node.get("site_url"):
                    attrs["htmlUrl"] = node["site_url"]
                ET.SubElement(parent, "outline", attrs)

    add(body, tree)
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def count_feeds(tree: list) -> int:
    return sum(count_feeds(n["children"]) if n["type"] == "folder" else 1 for n in tree)
