"""Put app/ and app/vendor/ on sys.path the same way launcher.py does."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
DATA = Path(__file__).resolve().parent / "data"

for path in (APP / "vendor", APP):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def sample(name: str) -> bytes:
    return (DATA / name).read_bytes()
