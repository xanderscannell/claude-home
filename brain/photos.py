"""Slideshow photos: a folder on this PC, resized for the 1280x720 screen and cached.

PHOTOS_DIR overrides the folder (default: Pictures/Claude Home in your home folder). Drop photos in to add
them; HEIC isn't supported (Pillow can't read it without another library), so export those as JPEG.
"""

import hashlib
import io
import os
import random
from pathlib import Path

from PIL import Image, ImageOps

SIZE = (1280, 720)
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
CACHE = Path(__file__).parent / ".cache" / "photos"


def folder() -> Path:
    return Path(os.environ.get("PHOTOS_DIR") or Path.home() / "Pictures" / "Claude Home")


def names(where: Path | None = None) -> list[str]:
    """Photo file names in the folder (not subfolders), shuffled."""
    where = where or folder()
    found = [p.name for p in where.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS] if where.is_dir() else []
    random.shuffle(found)
    return found


def find(name: str, where: Path | None = None) -> Path | None:
    """The photo called name, or None if it isn't a photo directly inside the folder (no ../ escapes)."""
    where = (where or folder()).resolve()
    path = (where / name).resolve()
    if path.parent != where or path.suffix.lower() not in EXTENSIONS or not path.is_file():
        return None
    return path


def render(src: Path) -> bytes:
    """A JPEG that covers the screen exactly: rotated upright from EXIF, cropped to fill, quality 82."""
    with Image.open(src) as im:
        im = ImageOps.fit(ImageOps.exif_transpose(im).convert("RGB"), SIZE, Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=82)
        return out.getvalue()


def cached(src: Path, cache: Path = CACHE) -> Path:
    """The screen-sized copy of src, rendered once per version of the file and screen size."""
    stat = src.stat()
    key = hashlib.sha1(f"{src}|{stat.st_mtime_ns}|{stat.st_size}|{SIZE}".encode()).hexdigest()[:20]
    out = cache / f"{key}.jpg"
    if not out.exists():
        cache.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(render(src))
        tmp.replace(out)  # never serve a half-written file
    return out
