"""Display page tests: Playwright at 800x480 with touch, the brain's socket mocked.

Run: uv run python test_display.py
Screenshots: set SCREENS_DIR (and optionally SCREENS_PREFIX) to save one PNG per state.
"""
import datetime as dt
import io
import json
import mimetypes
import os
import pathlib
import time
import urllib.parse

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

DISPLAY = pathlib.Path(__file__).parent / "display"
ORIGIN = "http://display.test"
SHOTS = os.environ.get("SCREENS_DIR")
PREFIX = os.environ.get("SCREENS_PREFIX", "display")
START = dt.datetime(2026, 10, 7, 9, 41, 0)  # a Wednesday morning, local time


class Display:
    """One page with a fake brain on the other end of its socket."""

    def __init__(self, browser, start=START, ws=None, photos=None):
        """ws: point the page's socket at this URL instead of the fake brain (e.g. a port nobody listens on).
        Refusing a mocked socket from inside its handler deadlocks Playwright's sync API, so offline is real.
        photos: {name: JPEG bytes} served at /photos, in this order (default: none)."""
        self.sent, self.sockets, self.errors = [], [], []
        self.photos = photos or {}
        self.ctx = browser.new_context(viewport={"width": 800, "height": 480}, has_touch=True,
                                       locale="en-US", timezone_id="America/New_York")
        self.page = self.ctx.new_page()
        self.page.on("console", lambda m: m.type == "error" and not (ws and "WebSocket" in m.text)
                     and self.errors.append(m.text))
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.route(f"{ORIGIN}/**", self._serve)
        if not ws:  # the pattern would also catch a custom ws URL
            self.page.route_web_socket("**/display?*", self._socket)
        self.page.clock.install(time=start)
        self.page.goto(f"{ORIGIN}/display/index.html?device=test" + (f"&ws={ws}" if ws else ""))
        self.page.wait_for_function("document.fonts.status === 'loaded'")
        if not ws:
            self.wait_until(lambda: self.sockets, "page never connected")
        else:
            self.page.wait_for_timeout(500)  # real time for the first refusal to arrive before fast-forwarding

    def _serve(self, route):
        rel = route.request.url.split("?")[0].removeprefix(ORIGIN)
        if rel == "/photos":
            return route.fulfill(json=list(self.photos))
        if rel.startswith("/photos/"):
            name = urllib.parse.unquote(rel.removeprefix("/photos/"))
            return route.fulfill(body=self.photos[name], content_type="image/jpeg")
        path = rel.removeprefix("/display/")
        file = (DISPLAY / path).resolve()
        if DISPLAY.resolve() not in file.parents or not file.is_file():
            return route.fulfill(status=404, body="not found")
        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream")

    def _socket(self, ws):
        self.sockets.append(ws)
        ws.on_message(lambda m: self.sent.append(json.loads(m)))

    def push(self, *msgs):
        for m in msgs:
            self.sockets[-1].send(json.dumps(m))
        self.page.wait_for_timeout(50)

    def tap(self, selector):
        self.page.tap(selector)
        self.page.wait_for_timeout(50)

    def wait_until(self, cond, why, timeout=5):
        deadline = time.time() + timeout
        while not cond():
            assert time.time() < deadline, why
            self.page.wait_for_timeout(50)

    def visible(self, selector) -> bool:
        return self.page.is_visible(selector)

    def text(self, selector) -> str:
        return self.page.inner_text(selector)

    def shot(self, name, settle=300):
        """settle: real milliseconds for CSS transitions to finish (they don't follow page.clock)."""
        if SHOTS:
            self.page.wait_for_timeout(settle)
            self.page.screenshot(path=os.path.join(SHOTS, f"{PREFIX}-{name}.png"))

    def close(self):
        assert not self.errors, self.errors
        self.page.unroute_all(behavior="ignoreErrors")  # no late socket events after the page is gone
        self.ctx.close()


THREE = ["Sunny and mild, topping out around seventy-one.", "A light breeze picks up after four.",
         "Tomorrow looks much the same."]
LONG = ("Pancakes need one cup of flour, two tablespoons of sugar, two teaspoons of baking powder and a pinch "
        "of salt, then a cup of milk, one egg and two tablespoons of melted butter, whisked until just combined "
        "and cooked on a medium griddle until bubbles form, then flipped once.")


def test_home_and_clock(b):
    d = Display(b)
    d.push({"type": "hello", "device": "test", "night": {"start": "22:00", "end": "07:00"}},
           {"type": "state", "state": "idle"})
    assert d.text("#time") == "9:41", d.text("#time")
    assert d.text("#date") == "Wednesday, October 7", d.text("#date")
    assert not d.visible("#overlay") and not d.visible("#offline")
    d.shot("home")
    d.page.clock.run_for(60_000)
    assert d.text("#time") == "9:42", "clock didn't tick over"
    d.close()


def test_conversation(b):
    d = Display(b)
    d.push({"type": "state", "state": "listening"})
    assert d.visible("#overlay") and d.page.get_attribute("#overlay", "data-state") == "listening"
    assert d.page.evaluate("document.body.classList.contains('talking')"), "home text should hide under the overlay"
    d.shot("listening")

    d.push({"type": "state", "state": "thinking"}, {"type": "heard", "text": "What's the weather like this afternoon?"})
    assert d.page.get_attribute("#overlay", "data-state") == "thinking"
    assert d.text("#heard") == "What's the weather like this afternoon?"
    d.shot("thinking")

    d.push({"type": "state", "state": "speaking"}, *[{"type": "reply", "text": s} for s in THREE])
    assert d.text("#reply") == " ".join(THREE), d.text("#reply")
    d.shot("speaking")

    d.tap("#stop")
    assert {"type": "stop"} in d.sent, d.sent

    d.push({"type": "state", "state": "idle"})
    assert d.visible("#overlay"), "overlay should linger a moment after idle"
    d.page.clock.run_for(4100)
    assert not d.visible("#overlay"), "overlay didn't return home"
    d.shot("back-home")

    # The next conversation starts clean
    d.push({"type": "state", "state": "listening"})
    assert d.text("#reply") == "" and d.text("#heard") == ""
    d.close()


def test_long_reply_fits(b):
    d = Display(b)
    d.push({"type": "state", "state": "thinking"}, {"type": "heard", "text": "How do I make pancakes?"},
           {"type": "state", "state": "speaking"}, {"type": "reply", "text": LONG})
    fits = d.page.evaluate("""() => { const r = document.getElementById('reply');
        return { fits: r.scrollHeight <= r.clientHeight + 1, size: parseFloat(getComputedStyle(r).fontSize) }; }""")
    assert fits["fits"] and fits["size"] >= 24, fits
    d.shot("long-reply")
    d.close()


WEATHER = {"place": "New York", "now": {"temp": 47, "feels": 43, "condition": "clear", "code": 0, "wind": 3},
           "days": [{"label": "Today", "condition": "clear", "code": 0, "high": 65, "low": 44, "rain": 1},
                    {"label": "Tomorrow", "condition": "overcast", "code": 3, "high": 76, "low": 56, "rain": 2},
                    {"label": "Friday", "condition": "overcast", "code": 3, "high": 74, "low": 61, "rain": 0}]}


def ambient(timers=(), weather=WEATHER):
    return {"type": "ambient", "weather": weather, "timers": list(timers), "next_alarm": None, "agenda": []}


def test_weather_and_timers(b):
    d = Display(b)
    t0 = START.timestamp()
    d.push(ambient([{"id": 2, "label": "", "ends_at": t0 + 75}, {"id": 1, "label": "pasta", "ends_at": t0 + 252}]))
    assert d.text(".weather .temp") == "47°" and d.text(".weather .condition") == "Clear, high 65"
    nums, labels = d.page.locator(".strip .num").all_inner_texts(), d.page.locator(".strip .label").all_inner_texts()
    assert labels == ["Timer", "Pasta"] and nums == ["1:15", "4:12"], (labels, nums)
    for selector in (".weather-button", ".strip .item"):
        box = d.page.locator(selector).first.bounding_box()
        assert box["height"] >= 64 and box["width"] >= 64, (selector, box)
    d.shot("weather-timers")

    d.page.clock.run_for(60_000)
    assert d.page.locator(".strip .num").all_inner_texts() == ["0:15", "3:12"]
    d.page.clock.run_for(20_000)  # the unnamed timer finishes and leaves the strip
    assert d.page.locator(".strip .label").all_inner_texts() == ["Pasta"]

    d.tap(".weather-button")
    d.tap(".strip .item")
    assert {"type": "open", "sheet": "weather"} in d.sent and {"type": "open", "sheet": "timers"} in d.sent, d.sent

    # Long timers read as h:mm:ss; no weather yet means no weather block at all
    d.push(ambient([{"id": 3, "label": "roast", "ends_at": t0 + 80 + 3725}], weather=None))
    assert d.page.locator(".strip .num").all_inner_texts() == ["1:02:05"]
    assert d.page.locator(".weather-button").count() == 0
    d.close()


def photo(top, bottom) -> bytes:
    """A screen-sized JPEG fading from one color to another, like a sky over ground."""
    im = Image.new("RGB", (800, 480))
    draw = ImageDraw.Draw(im)
    for y in range(480):
        t = y / 479
        draw.line([(0, y), (800, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()


def accent(d) -> str:
    return d.page.evaluate("getComputedStyle(document.getElementById('mic')).backgroundColor")


MARIGOLD = "rgb(242, 181, 68)"


def test_slideshow_and_accent(b):
    # No photos: the night gradient, marigold, no photo layer shown
    d = Display(b)
    d.page.wait_for_timeout(300)
    assert d.page.locator(".photo.shown").count() == 0 and accent(d) == MARIGOLD
    d.close()

    sunset = photo((236, 120, 60), (60, 30, 50))
    lake = photo((90, 150, 220), (20, 40, 70))
    d = Display(b, photos={"sunset.jpg": sunset, "lake.jpg": lake})
    d.wait_until(lambda: d.page.locator(".photo.shown").count() == 1, "first photo never appeared")
    assert "sunset.jpg" in d.page.get_attribute(".photo.shown", "style")
    warm = accent(d)
    assert warm != MARIGOLD, "accent didn't follow the photo"
    r, g, bl = (int(x) for x in warm[4:-1].split(","))
    assert r > bl, f"a sunset should give a warm accent, got {warm}"
    d.push(ambient([{"id": 1, "label": "pasta", "ends_at": START.timestamp() + 252}]))
    d.shot("photo-home", settle=2300)

    d.page.clock.run_for(PHOTO_SECONDS * 1000 + 100)
    d.wait_until(lambda: "lake.jpg" in (d.page.get_attribute(".photo.shown", "style") or ""), "slideshow didn't advance")
    assert d.page.locator(".photo.shown").count() == 1, "crossfade should leave exactly one photo shown"
    cool = accent(d)
    r, g, bl = (int(x) for x in cool[4:-1].split(","))
    assert bl > r, f"a lake should give a cool accent, got {cool}"
    d.shot("photo-lake", settle=2300)
    d.close()


PHOTO_SECONDS = 30


def test_touch_controls(b):
    d = Display(b)
    d.tap("#mic")
    assert {"type": "talk"} in d.sent, d.sent
    box = d.page.locator("#mic").bounding_box()
    assert box["width"] >= 64 and box["height"] >= 64, box
    d.push({"type": "state", "state": "speaking"})
    d.tap("#close-overlay")
    assert not d.visible("#overlay"), "close didn't dismiss the overlay"
    d.close()


def test_reloads_for_new_display_code(b):
    d = Display(b)
    d.push({"type": "hello", "device": "test", "version": "100", "night": {"start": "22:00", "end": "07:00"}})
    d.page.evaluate("window.__sameDocument = true")
    d.push({"type": "hello", "device": "test", "version": "100", "night": {"start": "22:00", "end": "07:00"}})
    assert d.page.evaluate("window.__sameDocument === true"), "reloaded although the version didn't change"
    d.push({"type": "hello", "device": "test", "version": "101", "night": {"start": "22:00", "end": "07:00"}})
    d.wait_until(lambda: d.page.evaluate("window.__sameDocument !== true"), "didn't reload for new display code")
    d.close()


def test_offline_note(b):
    # Connected: no note
    d = Display(b)
    assert not d.visible("#offline")
    d.close()
    # Nothing listening: the note appears after 5 s of retrying, not before
    d = Display(b, ws="ws://127.0.0.1:9/display")
    d.page.clock.run_for(3000)
    assert not d.visible("#offline"), "offline note shouldn't flash on a brief drop"
    d.page.clock.run_for(2500)
    assert d.visible("#offline"), "offline note didn't appear"
    top = d.page.evaluate("document.getElementById('home').getBoundingClientRect().top")
    assert top == 0, f"the page moved while offline (home top {top})"
    d.shot("offline")
    d.close()


if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn(browser)
                print(f"  {name}: ok")
        browser.close()
    print("ok")
