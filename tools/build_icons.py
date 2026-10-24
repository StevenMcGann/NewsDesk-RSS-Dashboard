"""Render the logo SVG to PNGs, a Windows .ico and a GitHub social-preview image.

Uses headless Microsoft Edge as the SVG renderer, so there is nothing to install.
Run from anywhere:  py -3 tools/build_icons.py

Source:   assets/logo.svg
Outputs:  assets/png/newsdesk-<size>.png, assets/newsdesk.ico, assets/social-preview.png,
          app/static/favicon.svg, app/static/icon-256.png
"""
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
STATIC = ROOT / "app" / "static"
sys.path.insert(0, str(ROOT / "app"))
from launcher import find_edge  # noqa: E402

PNG_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; padding: 0; background: transparent; overflow: hidden; }}
svg {{ display: block; }}
</style></head><body>{body}</body></html>"""

SOCIAL = """<div style="display:flex; align-items:center; gap:64px; padding:0 120px; height:640px;
  background:#1e1e1e; font-family:'Segoe UI Variable Display','Segoe UI',sans-serif;">
  {logo}
  <div>
    <div style="color:#ffffff; font-size:84px; font-weight:700; line-height:1.05;">NewsDesk</div>
    <div style="color:#b9afff; font-size:46px; font-weight:600; margin-top:4px;">RSS Dashboard</div>
    <div style="color:#a3a3a8; font-size:30px; margin-top:30px;">A simple, private feed reader for Windows</div>
  </div>
</div>"""


def sized(svg_path: Path, px: int) -> str:
    svg = svg_path.read_text(encoding="utf-8")
    return svg.replace("<svg ", f'<svg width="{px}" height="{px}" ', 1)


def png_info(data: bytes):
    width, height = struct.unpack(">II", data[16:24])
    return width, height, data[25]  # color type 6 = RGBA


class Renderer:
    """Screenshots HTML pages with headless Edge.

    msedge.exe hands the work to a child process and exits early, so wait for the
    screenshot file itself rather than for the process.
    """

    def __init__(self, edge: str, work: Path):
        self.edge, self.work = edge, work

    def render(self, body: str, width: int, height: int, out: Path, transparent=True):
        out.parent.mkdir(parents=True, exist_ok=True)
        page = self.work / "page.html"
        page.write_text(PAGE.format(body=body), encoding="utf-8")
        shot = self.work / "shot.png"
        shot.unlink(missing_ok=True)
        subprocess.run([
            self.edge, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
            "--default-background-color=00000000", f"--user-data-dir={self.work / 'profile'}",
            f"--window-size={width},{height}", f"--screenshot={shot}", page.as_uri(),
        ], check=True, capture_output=True, timeout=120)
        data = self._wait_for(shot)
        w, h, color_type = png_info(data)
        if (w, h) != (width, height) or (transparent and color_type != 6):
            raise RuntimeError(f"{out.name}: got {w}x{h} color type {color_type}, expected {width}x{height}"
                               f"{' with alpha' if transparent else ''}")
        out.write_bytes(data)

    @staticmethod
    def _wait_for(path: Path, timeout=60) -> bytes:
        deadline, last = time.monotonic() + timeout, -1
        while time.monotonic() < deadline:
            if path.exists():
                size = path.stat().st_size
                if size and size == last:  # finished writing
                    return path.read_bytes()
                last = size
            time.sleep(0.3)
        raise RuntimeError("Edge did not produce a screenshot")


def write_ico(images: list, out: Path):
    """An .ico with PNG-compressed entries (supported since Windows Vista)."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    out.write_bytes(header + entries + blobs)


def main():
    edge = find_edge()
    if not edge:
        sys.exit("Microsoft Edge was not found; it is used to render the SVG.")
    logo = ASSETS / "logo.svg"
    png_dir = ASSETS / "png"
    work = Path(tempfile.mkdtemp(prefix="newsdesk-icons-"))
    try:
        r = Renderer(edge, work)
        for size in PNG_SIZES:
            r.render(sized(logo, size), size, size, png_dir / f"newsdesk-{size}.png")
            print(f"assets/png/newsdesk-{size}.png")
        r.render(SOCIAL.format(logo=sized(logo, 300)), 1280, 640, ASSETS / "social-preview.png", transparent=False)
        print("assets/social-preview.png")
    finally:
        time.sleep(2)  # let Edge's child processes release the profile
        shutil.rmtree(work, ignore_errors=True)
    write_ico([(s, (png_dir / f"newsdesk-{s}.png").read_bytes()) for s in ICO_SIZES], ASSETS / "newsdesk.ico")
    print("assets/newsdesk.ico")
    shutil.copyfile(logo, STATIC / "favicon.svg")
    shutil.copyfile(png_dir / "newsdesk-256.png", STATIC / "icon-256.png")
    print("app/static/favicon.svg, app/static/icon-256.png")


if __name__ == "__main__":
    main()
