"""Hide the mouse cursor on the kiosk: cage draws one in the middle of the screen, and with only a touchscreen
nothing ever moves it onto the page (whose CSS hides it). This writes a fully transparent cursor into the user's
own "default" cursor theme (~/.icons/default), which cage loads ahead of the system theme. No root needed.

Run on the Pi as the kiosk's user:  python3 blank_cursor.py   then restart the display service.
Undo:  rm -r ~/.icons/default
"""

import struct
from pathlib import Path

SIZE = 24  # the nominal size cage asks for; one transparent pixel is enough to draw nothing
IMAGE = 0xFFFD0002  # Xcursor image chunk type

pixels = struct.pack("<I", 0)  # one ARGB pixel, fully transparent
chunk = struct.pack("<9I", 36, IMAGE, SIZE, 1, 1, 1, 0, 0, 0) + pixels  # header, type, size, version, w, h, hot x/y, delay
cursor = b"Xcur" + struct.pack("<3I", 16, 0x10000, 1) + struct.pack("<3I", IMAGE, SIZE, 16 + 12) + chunk

folder = Path.home() / ".icons" / "default" / "cursors"
folder.mkdir(parents=True, exist_ok=True)
for name in ("default", "left_ptr"):  # what cage asks for, and the older name some lookups fall back to
    (folder / name).write_bytes(cursor)
print(f"blank cursor written to {folder}")
