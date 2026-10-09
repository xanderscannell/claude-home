"""Hide the mouse cursor on the kiosk: cage draws one in the middle of the screen, and with only a touchscreen
nothing ever moves it onto the page (whose CSS hides it). After a reload Chromium also draws its own arrow there,
and with no mouse motion it never rechecks the CSS; that arrow comes from Adwaita (by theme name, or by a cursor
name "default" lacks and inherits). So this writes a fully transparent copy of every cursor Adwaita has into the user's own "default" and "Adwaita"
themes (~/.icons), which load ahead of the system ones. No root needed.

Run on the Pi as the kiosk's user:  python3 blank_cursor.py   then restart the display service.
Undo:  rm -r ~/.icons/default ~/.icons/Adwaita
"""

import struct
from pathlib import Path

SIZE = 24  # the nominal size cage asks for; one transparent pixel is enough to draw nothing
IMAGE = 0xFFFD0002  # Xcursor image chunk type

pixels = struct.pack("<I", 0)  # one ARGB pixel, fully transparent
chunk = struct.pack("<9I", 36, IMAGE, SIZE, 1, 1, 1, 0, 0, 0) + pixels  # header, type, size, version, w, h, hot x/y, delay
cursor = b"Xcur" + struct.pack("<3I", 16, 0x10000, 1) + struct.pack("<3I", IMAGE, SIZE, 16 + 12) + chunk

names = {"default", "left_ptr"} | {p.name for p in Path("/usr/share/icons/Adwaita/cursors").glob("*")}
for theme in ("default", "Adwaita"):
    folder = Path.home() / ".icons" / theme / "cursors"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).unlink(missing_ok=True)  # Adwaita's own names include symlinks; write real files
        (folder / name).write_bytes(cursor)
    print(f"{len(names)} blank cursors written to {folder}")
