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


def big(box, w=64, h=64) -> bool:
    """A touch target at least w x h px. Rounded: boxes measured mid-slide come out a hair under (63.99997)."""
    return bool(box) and round(box["width"]) >= w and round(box["height"]) >= h


class Display:
    """One page with a fake brain on the other end of its socket."""

    def __init__(self, browser, start=START, ws=None, photos=None):
        """ws: point the page's socket at this URL instead of the fake brain (e.g. a port nobody listens on).
        Refusing a mocked socket from inside its handler deadlocks Playwright's sync API, so offline is real.
        photos: {name: JPEG bytes} served at /photos, in this order (default: none)."""
        self.sent, self.sockets, self.errors, self.requests = [], [], [], []
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
        self.requests.append(rel)
        if rel == "/photos":
            return route.fulfill(json=list(self.photos))
        if rel == "/art.jpg":  # album art for the music tests
            return route.fulfill(body=ALBUM_ART, content_type="image/jpeg")
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


def test_clock_comes_down_when_the_strip_is_empty(b):
    """Nothing on the strip, the clock comes down into the room, the date where the strip's text would be; something
    on it, the clock goes back up over it. The night clock comes down with it, so the two still line up."""
    d = Display(b)
    shift = lambda sel: d.page.eval_on_selector(sel, "e => new DOMMatrix(getComputedStyle(e).transform).f")  # noqa: E731
    middle = lambda sel: (lambda r: r["y"] + r["height"] / 2)(d.page.locator(sel).bounding_box())  # noqa: E731
    d.push(ambient([], weather=None))
    d.page.wait_for_timeout(500)  # (its move is real time)
    assert shift(".clock") == 62
    down = middle("#date")

    pasta = {"id": 1, "label": "pasta", "ends_at": START.timestamp() + 300, "duration": 600, "paused_left": None}
    d.push(ambient([pasta], weather=None))
    d.page.wait_for_timeout(500)
    assert shift(".clock") == 0
    assert abs(middle(".strip .item .label") - down) < 6, "the date sat where the strip's text is"
    assert middle("#date") < middle(".strip .item"), "the clock is back up over the strip"

    d.push(ambient([], weather=None))
    d.page.wait_for_timeout(500)
    assert shift(".clock") == 62, "and down again when the strip empties"
    d.close()

    # At night (the night clock is only drawn then), it comes down and goes up the same way
    d = Display(b, start=dt.datetime(2026, 10, 7, 23, 0, 0))
    d.push(hello(), ambient([], weather=None))
    d.page.wait_for_timeout(500)
    assert is_night(d) and shift(".night-time") == 62
    d.push(ambient([dict(pasta, ends_at=dt.datetime(2026, 10, 7, 23, 5).timestamp())], weather=None))
    d.page.wait_for_timeout(500)
    assert shift(".night-time") == 0
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
        assert big(box), (selector, box)
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

    # A paused timer holds its time, greyed, and keeps its place by time left
    d.push(ambient([{"id": 4, "label": "tea", "ends_at": t0, "duration": 300, "paused_left": 200},
                    {"id": 5, "label": "rice", "ends_at": t0 + 80 + 600, "duration": 900, "paused_left": None}]))
    assert d.page.locator(".strip .label").all_inner_texts() == ["Tea", "Rice"]
    d.page.clock.run_for(5000)
    assert d.page.locator(".strip .num").all_inner_texts() == ["3:20", "9:55"]
    assert d.page.locator(".strip .timer.paused").count() == 1

    # Lots of timers: the strip scrolls sideways, and ticking never shifts anything (the digits aren't tabular)
    now = t0 + 85
    many = [{"id": 10 + i, "label": f"batch {i}", "ends_at": now + 73 + i * 41, "duration": 600, "paused_left": None}
            for i in range(6)]
    d.push(ambient(many))
    width, room = d.page.eval_on_selector(".strip", "s => [s.scrollWidth, s.clientWidth]")
    assert width > room, (width, room)
    xs = lambda: [round(b["x"]) for b in (d.page.locator(".strip .label").nth(i).bounding_box() for i in range(3))]  # noqa: E731
    before = xs()
    d.page.clock.run_for(3000)  # 1:13 ticks down through 1:11, the narrowest
    assert xs() == before, (before, xs())
    assert d.page.eval_on_selector(".strip", "s => { s.scrollLeft = 10000; return s.scrollLeft; }") > 0
    d.shot("strip-many-timers")
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
    d.push(hello())
    d.page.wait_for_timeout(300)
    assert d.page.locator(".photo.shown").count() == 0 and accent(d) == MARIGOLD
    d.close()

    sunset = photo((236, 120, 60), (60, 30, 50))
    lake = photo((90, 150, 220), (20, 40, 70))
    d = Display(b, photos={"sunset.jpg": sunset, "lake.jpg": lake})
    d.push(hello())  # the slideshow starts once the brain says hello (it might be night)
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
    # Swiped aside, the dashboard is drawn as a sheet with the photo showing now as the one thing on it
    swipe(d, 500, frm=700, y=200, release=False)
    assert "lake.jpg" in d.page.get_attribute("#dash-pic", "style")
    d.page.mouse.up()
    d.close()


PHOTO_SECONDS = 30


def hello(start="22:00", end="07:00") -> dict:
    return {"type": "hello", "device": "test", "version": "1", "night": {"start": start, "end": end}}


def is_night(d) -> bool:
    return d.page.evaluate("document.body.classList.contains('is-night')")


def photo_requests(d) -> list:
    return [r for r in d.requests if r.startswith("/photos")]


def test_night_mode(b):
    pics = {"sunset.jpg": photo((236, 120, 60), (60, 30, 50))}

    # 23:00: pure black, the time in dim ember, nothing else, and no photos fetched (not even after 30 s)
    d = Display(b, start=dt.datetime(2026, 10, 7, 23, 0, 0), photos=pics)
    d.push(hello())
    assert is_night(d) and d.visible("#night") and not d.visible("#home")
    assert d.text("#night-time") == "11:00", d.text("#night-time")
    style = d.page.evaluate("""() => { const s = getComputedStyle(document.getElementById('night-time'));
        return [s.color, s.opacity, getComputedStyle(document.getElementById('night')).backgroundColor]; }""")
    assert style == ["rgb(255, 138, 76)", "0.35", "rgb(0, 0, 0)"], style
    d.page.clock.run_for(PHOTO_SECONDS * 1000 + 1000)
    d.page.wait_for_timeout(200)
    assert photo_requests(d) == [], photo_requests(d)
    assert not d.visible(".photo.shown")
    d.shot("night", settle=200)

    # A tap brings the normal home back (and the room with it) for 30 s; another touch keeps it 30 s more
    d.tap("#night")
    assert not is_night(d) and d.visible("#home") and d.visible("#mic")
    d.wait_until(lambda: d.page.locator(".photo.shown").count() == 1, "the room didn't come back on a tap")
    assert [m for m in d.sent if m["type"] != "fetch"] == [], "the waking tap mustn't press anything"
    d.shot("night-woken", settle=2300)
    d.page.clock.run_for(20_000)
    d.tap("#date")
    d.page.clock.run_for(20_000)
    assert not is_night(d), "a touch should keep home up another 30 s"
    d.page.clock.run_for(11_000)
    assert is_night(d), "home didn't go back to the night clock after 30 s"

    # Ringing still shows at night, on top; afterwards it's the night clock again
    d.push({"type": "ringing", "ringing": {"kind": "alarm", "id": 1, "label": "", "time": "06:30"}})
    assert d.page.evaluate("document.elementFromPoint(400, 240).closest('section').id") == "ringing"
    d.shot("night-ringing", settle=200)
    d.push({"type": "ringing", "ringing": None})
    assert d.visible("#night") and not d.visible("#ringing")

    # A conversation comes up over the night clock and leaves it behind
    d.push({"type": "state", "state": "listening"})
    assert d.page.evaluate("document.elementFromPoint(400, 240).closest('section').id") == "overlay"
    d.push({"type": "state", "state": "idle"})
    d.page.clock.run_for(4100)
    assert not d.visible("#overlay") and d.visible("#night")
    d.close()

    # Noon: the normal home
    d = Display(b, start=dt.datetime(2026, 10, 7, 12, 0, 0))
    d.push(hello())
    assert not is_night(d) and not d.visible("#night") and d.visible("#home")
    d.close()

    # 06:59 is still night; at 07:00 the room comes back by itself
    d = Display(b, start=dt.datetime(2026, 10, 8, 6, 59, 30), photos=pics)
    d.push(hello())
    assert is_night(d) and d.text("#night-time") == "6:59"
    d.page.clock.run_for(31_000)
    assert not is_night(d) and d.text("#time") == "7:00"
    d.wait_until(lambda: d.page.locator(".photo.shown").count() == 1, "no photo at 7:00")
    d.close()

    # Night hours that don't cross midnight work too (13:00 to 15:00 at 14:00)
    d = Display(b, start=dt.datetime(2026, 10, 7, 14, 0, 0))
    d.push(hello("13:00", "15:00"))
    assert is_night(d)
    d.close()


def test_forcing_night(b):
    """Swiped in from its left edge, the dashboard slides away over the night screen's black, and the dim clock comes
    up once it's gone. The next touch brings the dashboard back, for good (it isn't night); left alone, it's night
    until the night hours end."""
    d = Display(b)
    d.push(hello())  # night is 22:00 to 07:00; it's 9:41 in the morning
    clock = lambda: d.page.eval_on_selector("#night-time", "t => getComputedStyle(t).opacity")  # noqa: E731
    assert not is_night(d)
    edge, menu = d.page.locator("#dash-edge").bounding_box(), d.page.locator("#menu").bounding_box()
    assert edge["x"] == 0 and edge["y"] >= menu["y"] + menu["height"], "the edge leaves the menu button alone"

    swipe(d, 300, y=300, release=False)
    assert d.visible("#night") and not is_night(d)
    assert round(d.page.eval_on_selector("#dashboard", "e => e.getBoundingClientRect().left")) == 300 - 8
    assert clock() == "0", "the clock waits for the dashboard to be gone"
    d.shot("forcing-night", settle=0)
    d.page.mouse.up()
    d.page.clock.run_for(600)
    d.page.wait_for_timeout(900)  # the clock coming up (real time)
    assert is_night(d) and clock() == "0.35"
    d.shot("forced-night")

    d.tap("#night")
    assert not is_night(d) and d.visible("#home")
    assert [m for m in d.sent if m["type"] != "fetch"] == [], "the waking tap mustn't press anything"
    d.page.clock.run_for(60_000)
    assert not is_night(d), "a forced night ends with a touch, not for 30 s like a real one"

    swipe(d, 150, y=300, pause=40)  # short and slow: springs back
    assert not is_night(d) and not d.visible("#night")
    assert d.page.eval_on_selector("#dashboard", "e => e.getBoundingClientRect().left") == 0
    d.close()

    # Left alone, a forced night ends when the night hours do: forced at 9:41, it's day again at 7:00 tomorrow
    d = Display(b)
    d.push(hello())
    swipe(d, 400, y=300)
    assert is_night(d)
    d.page.clock.run_for((21 * 60 + 18) * 60_000)  # to 6:59 tomorrow
    assert is_night(d), "night all through the night hours"
    d.page.clock.run_for(2 * 60_000)
    assert not is_night(d) and d.text("#time") == "7:01", "day at the night hours' end"
    d.close()


def sheet_open(d) -> bool:
    return d.visible("#sheet")


def test_sheets(b):
    d = Display(b)
    t0 = START.timestamp()

    # Weather: the place as the title, now as the hero, three days; rain only when it's likely
    w = dict(WEATHER, place="New York, New York, United States")
    w["days"] = [dict(WEATHER["days"][0]), dict(WEATHER["days"][1], rain=60), dict(WEATHER["days"][2])]
    d.push({"type": "card", "card": {"kind": "weather", **w}})
    assert d.page.eval_on_selector("#sheet", "s => s.getBoundingClientRect().top") > 0, "it slides up from the bottom"
    assert sheet_open(d) and d.text("#sheet-title") == "New York"
    assert d.text(".wx-temp") == "47°" and d.text(".wx-condition") == "Clear"
    assert d.text(".wx-detail") == "Feels like 43°, wind 3 mph"
    assert d.page.locator(".wx-label").all_inner_texts() == ["Today", "Tomorrow", "Friday"]
    assert d.page.locator(".wx-rain").all_inner_texts() == ["60% chance of rain"], "rain shown only when likely"
    d.shot("sheet-weather", settle=400)
    assert d.page.locator("#sheet-edge").is_visible(), "a card over the dashboard goes back to it from the edge"
    swipe(d, 500)
    assert not sheet_open(d), "the edge swipe didn't return home"
    t0 = d.page.evaluate("Date.now() / 1000")  # (the swipe's settle moved the clock on)

    # Timers: one gets a big dial, its arc what's left of it, with Pause and Stop stacked beside it
    pasta = {"id": 7, "label": "pasta", "ends_at": t0 + 252, "duration": 504, "paused_left": None}
    d.push({"type": "card", "card": {"kind": "timers", "timers": [pasta]}})
    assert d.text("#sheet-title") == "Timers" and d.page.locator(".timers.one .tmr").count() == 1
    assert d.text(".dial-time") == "4:12" and d.text(".dial-name") == "Pasta"
    assert d.page.locator(".tmr-btn").all_inner_texts() == ["Pause", "Stop"]
    offset = lambda: float(d.page.eval_on_selector(".dial-arc", "a => a.style.strokeDashoffset"))  # noqa: E731
    assert abs(offset() - 0.5) < 0.01, offset()
    dial, pause = d.page.locator(".dial").bounding_box(), d.page.locator(".tmr-btn.tmr-main").bounding_box()
    assert pause["x"] > dial["x"] + dial["width"] and big(pause, w=240, h=96), (dial, pause)
    d.shot("sheet-timer", settle=400)
    d.page.clock.run_for(2000)
    assert d.text(".dial-time") == "4:10", "the dial isn't ticking"
    assert abs(offset() - (1 - 250 / 504)) < 0.01, offset()
    d.page.wait_for_timeout(300)  # the sheet's slide up runs in real time: let it finish, or the box moves with it
    box = d.page.locator(".dial-time").bounding_box()
    d.page.clock.run_for(3000)  # 4:10 down to 4:07: different digit widths, same box
    assert d.page.locator(".dial-time").bounding_box() == box, "the dial's time shouldn't shift as it ticks"
    off_center = d.page.evaluate("""() => {  // measured together: the sheet may still be sliding
      const text = document.createRange();  // the digits themselves, not the box around them
      text.selectNodeContents(document.querySelector(".dial-time"));
      const t = text.getBoundingClientRect(), r = document.querySelector(".dial").getBoundingClientRect();
      return (t.left + t.width / 2) - (r.left + r.width / 2); }""")
    assert abs(off_center) < 1, f"the dial's time should be centered, it's {off_center}px off"

    # Pause shows at once and tells the brain; the time holds and blinks; Resume picks it up again
    d.page.locator(".tmr-btn.tmr-main").tap()
    d.page.wait_for_timeout(50)
    assert {"type": "timer_pause", "id": 7} in d.sent, d.sent
    assert d.page.locator(".tmr.paused").count() == 1 and d.text(".tmr-btn.tmr-main") == "Resume"
    held = d.text(".dial-time")
    d.page.clock.run_for(5000)
    assert d.text(".dial-time") == held, "a paused timer shouldn't count down"
    d.shot("sheet-timer-paused", settle=400)
    d.page.locator(".tmr-btn.tmr-main").tap()
    d.page.wait_for_timeout(50)
    assert {"type": "timer_resume", "id": 7} in d.sent and d.page.locator(".tmr.paused").count() == 0

    # Two: smaller dials side by side, round Pause and Stop under each
    now = t0 + 7
    pasta = dict(pasta, ends_at=now + 250)
    eggs = {"id": 8, "label": "eggs", "ends_at": now + 400, "duration": 420, "paused_left": None}
    d.push(ambient([pasta, eggs]))
    assert d.page.locator(".timers.two .tmr").count() == 2
    first, second = (d.page.locator(".dial").nth(i).bounding_box() for i in (0, 1))
    assert first["y"] == second["y"] and second["x"] > first["x"] + first["width"], (first, second)
    under = d.page.locator(".tmr").nth(0).locator(".tmr-btn")
    assert under.count() == 2 and under.first.bounding_box()["y"] > first["y"] + first["height"]
    assert under.all_inner_texts() == ["", ""] and big(under.first.bounding_box(), w=72, h=72)
    d.shot("sheet-timers-two", settle=400)

    # Three: smaller still, all on screen
    rice = {"id": 9, "label": "", "ends_at": now + 900, "duration": 5400, "paused_left": None}
    d.push(ambient([pasta, eggs, dict(rice, paused_left=600)]))
    assert d.page.locator(".timers.many .tmr").count() == 3 and d.page.locator(".tmr.paused").count() == 1
    assert d.page.locator(".dial-name").all_inner_texts() == ["Pasta", "Eggs", "1 hr 30 min timer"]
    last = d.page.locator(".tmr").nth(2).bounding_box()
    assert last["x"] + last["width"] <= 800, last
    d.shot("sheet-timers-three", settle=400)

    # Stop sends the timer's id; the list follows ambient updates
    d.page.locator(".tmr-btn.tmr-stop").first.tap()
    d.page.wait_for_timeout(50)
    assert {"type": "timer_cancel", "id": 7} in d.sent, d.sent
    d.push(ambient([eggs]))  # the brain confirms the stop
    assert d.page.locator(".timers.one .tmr").count() == 1
    d.push(ambient([]))
    assert d.text("#sheet-body .quiet") == "No timers running."
    d.shot("sheet-timers-empty", settle=400)

    # Untouched for 45 s, the sheet goes home; a touch restarts the wait
    d.page.clock.run_for(30_000)
    d.page.locator("#sheet-body").tap()
    d.page.clock.run_for(30_000)
    assert sheet_open(d), "a touch should restart the 45 s wait"
    d.page.clock.run_for(16_000)
    assert not sheet_open(d), "sheet didn't return home after 45 s untouched"

    # Text from Claude: few short lines get the big type, long lists stay readable
    d.push({"type": "card", "card": {"kind": "text", "title": "Pancakes",
                                     "lines": ["1 cup flour", "2 tbsp sugar", "1 egg", "1 cup milk"]}})
    assert d.text("#sheet-title") == "Pancakes" and d.page.locator(".text-lines.roomy p").count() == 4
    d.shot("sheet-text-short", settle=400)
    long = [f"Step {i}: whisk the dry ingredients, then fold in the wet ones gently" for i in range(1, 9)]
    d.push({"type": "card", "card": {"kind": "text", "title": "How to make pancakes", "lines": long}})
    assert d.page.locator(".text-lines.roomy").count() == 0 and d.page.locator(".text-lines p").count() == 8
    d.shot("sheet-text-long", settle=400)

    # A conversation shows over an open sheet, and the sheet is still there afterwards
    d.push({"type": "state", "state": "speaking"}, {"type": "reply", "text": "Here's the recipe."})
    assert d.visible("#overlay") and sheet_open(d)
    bg = d.page.evaluate("getComputedStyle(document.getElementById('overlay')).backgroundColor")
    assert bg == "rgb(19, 32, 46)", f"over a sheet the overlay must be solid, or the sheet's text shows through: {bg}"
    d.shot("overlay-over-sheet", settle=300)
    d.push({"type": "state", "state": "idle"})
    d.page.clock.run_for(4100)
    assert not d.visible("#overlay") and sheet_open(d)
    d.close()


def test_shopping_list(b):
    d = Display(b)
    d.push({"type": "card", "card": {"kind": "shopping", "groups": [
        {"name": "", "items": ["Milk"]}, {"name": "Meat & Dairy", "items": ["Eggs"]},
        {"name": "Pantry", "items": ["Sourdough bread", "Bananas"]}]}})
    assert d.text("#sheet-title") == "Shopping list"
    assert d.page.locator(".shop-name").all_inner_texts() == ["Milk", "Eggs", "Sourdough bread", "Bananas"]
    assert d.page.locator("#sheet-body .group-label").all_inner_texts() == ["Meat & Dairy", "Pantry"]  # none for no category
    box = d.page.locator(".shop-item").first.bounding_box()
    assert big(box, w=0), box
    d.shot("shopping", settle=400)

    # Check off Eggs: struck through at once, with Undo; nothing sent yet
    d.page.locator(".shop-item").nth(1).tap()
    assert "done" in d.page.get_attribute(".shop-row >> nth=1", "class") and d.visible(".shop-row.done .shop-undo")
    d.shot("shopping-checked", settle=200)
    d.page.clock.run_for(1500)
    d.page.locator(".shop-row.done .shop-undo").tap()  # changed my mind
    d.page.clock.run_for(3000)
    assert not any(m.get("type") == "shopping_remove" for m in d.sent), "undo should cancel the removal"
    assert d.page.locator(".shop-row.done").count() == 0

    # Check off Milk and let it go: removed after 3 s
    d.page.locator(".shop-item").first.tap()
    d.page.clock.run_for(3100)
    assert {"type": "shopping_remove", "item": "Milk"} in d.sent, d.sent
    d.push({"type": "card", "card": {"kind": "shopping", "groups": [{"name": "", "items": ["Eggs", "Sourdough bread", "Bananas"]}]}})
    assert d.page.locator(".shop-name").all_inner_texts() == ["Eggs", "Sourdough bread", "Bananas"]

    # Items are kept as said ("oat milk") but shown in sentence case; removal uses the item as stored
    d.push({"type": "card", "card": {"kind": "shopping", "groups": [{"name": "", "items": ["oat milk"]}]}})
    assert d.text(".shop-name") == "Oat milk"
    d.page.locator(".shop-item").first.tap()
    d.page.clock.run_for(3100)
    assert {"type": "shopping_remove", "item": "oat milk"} in d.sent

    # Empty list says how to add things
    d.push({"type": "card", "card": {"kind": "shopping", "groups": []}})
    assert d.text("#sheet-body .quiet") == "The shopping list is empty." and "add milk" in d.text(".hint")
    d.shot("shopping-empty", settle=200)
    d.close()


def test_menu(b):
    d = Display(b)
    d.tap("#menu")
    assert d.visible("#sheet") and d.text("#sheet-title") == "Menu"
    assert d.page.locator(".menu-label").all_inner_texts() == ["Home", "Timers", "Alarms", "Shopping list", "Music"]
    overhang = d.page.evaluate("""() => {  // both measured at once: the sheet may still be sliding up
      const rows = document.querySelectorAll(".menu-row"), body = document.querySelector("#sheet-body");
      return rows[rows.length - 1].getBoundingClientRect().bottom - body.getBoundingClientRect().bottom; }""")
    assert overhang <= 0, f"every menu row should fit; the last hangs {overhang}px below the sheet"
    d.shot("menu", settle=400)
    for i in range(4):
        d.page.locator(".menu-row").nth(i).tap()
    d.page.wait_for_timeout(50)
    assert [{"type": "open", "sheet": s} for s in ("house", "timers", "alarms", "shopping")] == [
        m for m in d.sent if m.get("type") == "open"], d.sent
    d.page.locator(".menu-row").nth(4).tap()  # Music: opens straight away, the page knows what's playing
    d.page.wait_for_timeout(50)
    assert d.text("#sheet-title") == "Music" and d.text("#sheet-body .quiet") == "Nothing is playing."
    d.close()


def volume_up(d, to=380, pause=0, release=True):
    """A finger up from the bottom edge to y=to. pause: real ms between moves (slow, so not a flick)."""
    d.page.mouse.move(400, 474)
    d.page.mouse.down()
    for y in range(474, to, -16):
        d.page.mouse.move(400, y)
        if pause:
            d.page.wait_for_timeout(pause)
    d.page.mouse.move(400, to)
    if release:
        d.page.mouse.up()
        d.page.clock.run_for(300)  # past the settle's timer
        d.page.wait_for_timeout(300)  # and its transition (CSS runs in real time)


def test_volume(b):
    """Swiped up from the bottom edge, the volume bar floats over whatever's showing: the speaker, the slider, the
    level. A tap anywhere else, or 5 s untouched, puts it away. Claude changing the volume puts it up."""
    d = Display(b)
    volumes = lambda: [m["level"] for m in d.sent if m.get("type") == "volume"]  # noqa: E731
    bar_up = lambda: d.visible("#vol-bar") and d.page.eval_on_selector(  # noqa: E731
        "#vol-bar", "b => Math.round(b.getBoundingClientRect().bottom)") == 480 - 24

    # Before the speaker has reported a level: say so
    edge = d.page.locator("#vol-edge").bounding_box()
    assert edge["y"] + edge["height"] == 480 and edge["width"] == 800 and edge["height"] <= 20, edge
    top = lambda: d.page.eval_on_selector("#vol-bar", "b => b.getBoundingClientRect().top")  # noqa: E731
    volume_up(d, release=False)
    held = top()
    assert held < 480, held
    d.page.mouse.up()
    tops = [d.page.wait_for_timeout(30) or top() for _ in range(6)]
    assert all(t <= held + 1 for t in tops), f"let go, it carries on up from where it was, not from below: {held} {tops}"
    d.page.clock.run_for(300)
    d.page.wait_for_timeout(300)
    assert bar_up() and d.text("#vol-none") == "The speaker isn't connected." and not d.visible("#vol-slider")
    d.shot("volume-unknown")

    # The level arrives: the speaker on the left, the slider across the middle, the level on the right
    d.push({"type": "volume", "level": 40})
    assert d.text("#vol-level") == "40" and d.page.input_value("#vol-slider") == "40" and not d.visible("#vol-none")
    bar = d.page.locator("#vol-bar").bounding_box()
    icon, slider, level = (d.page.locator(sel).bounding_box() for sel in (".vol-icon", "#vol-slider", "#vol-level"))
    assert round(bar["x"]) == 36 and round(bar["width"]) == 800 - 2 * 36, bar
    assert icon["x"] < slider["x"] < slider["x"] + slider["width"] < level["x"], (icon, slider, level)
    assert big(slider, w=480, h=64), slider
    d.shot("volume")

    # Drag from 40 to 80: the number follows the finger; one message, sent when the finger lifts
    y = slider["y"] + slider["height"] / 2
    at = lambda v: slider["x"] + 24 + v / 100 * (slider["width"] - 48)  # noqa: E731  (thumb centre for a value)
    d.page.mouse.move(at(40), y)
    d.page.mouse.down()
    d.page.mouse.move(at(80), y, steps=12)
    d.page.wait_for_timeout(50)
    assert volumes() == [], "nothing is sent while the finger is still down"
    d.push({"type": "volume", "level": 10})  # a report arriving mid-drag must not yank the slider
    assert abs(int(d.page.input_value("#vol-slider")) - 80) <= 1, d.page.input_value("#vol-slider")
    d.page.mouse.up()
    d.page.wait_for_timeout(50)
    assert len(volumes()) == 1 and abs(volumes()[0] - 80) <= 1, volumes()
    assert d.text("#vol-level") == str(volumes()[0])

    # With no finger on it, the brain's report moves it
    d.push({"type": "volume", "level": 55})
    assert d.text("#vol-level") == "55" and d.page.input_value("#vol-slider") == "55"
    assert len(volumes()) == 1, "a report from the brain must not echo back"

    # Left alone 5 s, it goes away
    d.page.clock.run_for(5_500)
    d.page.wait_for_timeout(300)
    assert not d.visible("#vol-bar")

    # A tap anywhere else puts it away, and doesn't press what's under it
    volume_up(d)
    assert bar_up()
    menu = d.page.locator("#menu").bounding_box()
    d.page.mouse.click(menu["x"] + menu["width"] / 2, menu["y"] + menu["height"] / 2)  # on the menu button
    d.page.wait_for_timeout(300)
    assert not d.visible("#vol-bar") and not d.visible("#sheet"), "the tap only put the bar away, the menu didn't open"

    # A short, slow swipe drops it back down; over a sheet it works the same
    volume_up(d, to=450, pause=30)
    assert not d.visible("#vol-bar")
    d.push({"type": "card", "card": HOUSE})
    volume_up(d)
    assert bar_up()
    d.page.clock.run_for(5_500)
    d.page.wait_for_timeout(300)

    # Claude turning it up puts the bar up at the new level (there's no volume sheet any more)
    d.push({"type": "card", "card": {"kind": "volume", "level": 65}})
    d.page.wait_for_timeout(300)
    assert bar_up() and d.text("#vol-level") == "65" and d.text("#sheet-title") == "Home"
    d.close()


HOUSE = {"kind": "house", "lights": {"on": 3, "total": 11, "rooms": ["Kitchen", "Living room"]},
         "climate": {"temp": 68, "target": 70, "action": "heating"},
         "media": {"playing": ["Living room TV"], "total": 4},
         "rooms": ["Kitchen", "Living room", "Porch"], "automations": {"on": 12, "total": 14}}


def test_home_control(b):
    d = Display(b)
    opened = lambda: [m["sheet"] for m in d.sent if m.get("type") == "open"]  # noqa: E731
    lights_sent = lambda: [m for m in d.sent if m.get("type") in ("light", "lights_off")]  # noqa: E731

    # Home: a tile each, its state at a glance; Lights is lit while any light is on
    d.push({"type": "card", "card": HOUSE})
    assert d.text("#sheet-title") == "Home"
    assert d.page.locator(".tile-num").all_inner_texts() == ["3", "68°", "1", "3", "12"]
    assert d.page.locator(".tile-detail").all_inner_texts() == [
        "Kitchen, Living room", "Heating to 70°", "Living room TV", "Kitchen, Living room, Porch", "2 off"]
    assert d.page.locator(".tile.lit").count() == 1 and d.page.locator(".tile-lights.lit").count() == 1
    for tile in d.page.locator(".tile").all():
        assert big(tile.bounding_box(), w=200, h=160), tile.bounding_box()
    d.shot("house", settle=400)
    d.tap(".tile-lights")
    d.tap(".tile-rooms")
    assert opened() == ["lights", "rooms"]
    off = dict(HOUSE, lights={"on": 0, "total": 11, "rooms": []}, media={"playing": [], "total": 4},
               climate={"temp": 66, "target": 66, "action": "idle"})
    d.push({"type": "card", "card": off})
    assert not d.page.locator(".tile.lit").count()
    assert d.page.locator(".tile-num").all_inner_texts()[:3] == ["Off", "66°", "Off"]
    assert d.page.locator(".tile-detail").all_inner_texts()[:3] == ["All 11 lights", "Set to 66°", "Nothing playing"]

    # Lights: by room; each tile filled as high as it's bright. Tap switches, a drag dims, All off clears them.
    lamp = {"entity": "light.lamp", "name": "Reading lamp", "on": True, "brightness": 40}
    floor = {"entity": "light.floor", "name": "Floor lamp", "on": False, "brightness": 70}
    porch = {"entity": "light.porch", "name": "Porch light", "on": False, "brightness": None}
    d.push({"type": "card", "card": {"kind": "lights", "rooms": [{"name": "Living room", "lights": [lamp, floor]},
                                                                   {"name": "Porch", "lights": [porch]}]}})
    assert d.text("#sheet-title") == "Lights"
    assert d.page.locator(".dev-group-name").all_inner_texts() == ["Living room", "Porch"]
    tile = lambda name: d.page.locator(f'.dev[aria-label="{name}"]')  # noqa: E731
    level = lambda name: tile(name).locator(".dev-level").first.inner_text()  # noqa: E731
    fill = lambda name: tile(name).evaluate("t => t.style.getPropertyValue('--b')")  # noqa: E731
    assert (level("Reading lamp"), fill("Reading lamp")) == ("40%", "40%")
    assert (level("Floor lamp"), fill("Floor lamp")) == ("Off", "0%")
    for name in ("Reading lamp", "Floor lamp", "Porch light"):
        assert big(tile(name).bounding_box(), w=140, h=140), name
    porch_box, floor_box = tile("Porch light").bounding_box(), tile("Floor lamp").bounding_box()
    assert porch_box["height"] > 2 * floor_box["height"], "a room's only light should be one tall tile"
    d.shot("lights", settle=400)

    tile("Floor lamp").tap()
    tile("Porch light").tap()
    d.page.wait_for_timeout(50)
    assert (level("Floor lamp"), level("Porch light"), fill("Porch light")) == ("70%", "On", "100%")
    assert lights_sent() == [{"type": "light", "entity": "light.floor", "on": True},
                             {"type": "light", "entity": "light.porch", "on": True}]

    box = tile("Reading lamp").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] - 20
    d.page.mouse.move(x, y)
    d.page.mouse.down()
    d.page.mouse.move(x, y - box["height"] * .3, steps=5)  # up 30% of the tile
    d.page.mouse.up()
    d.page.wait_for_timeout(50)
    assert level("Reading lamp") == "70%", level("Reading lamp")
    assert lights_sent()[-1] == {"type": "light", "entity": "light.lamp", "on": True, "brightness": 70}, d.sent
    assert len(lights_sent()) == 3, "the drag's own click shouldn't switch the light too"
    d.page.mouse.move(x, y - box["height"] * .3)
    d.page.mouse.down()
    d.page.mouse.move(x, y + box["height"], steps=5)  # all the way down: off
    d.page.mouse.up()
    d.page.wait_for_timeout(50)
    assert (level("Reading lamp"), lights_sent()[-1]["on"]) == ("Off", False)
    tile("Reading lamp").tap()
    d.page.wait_for_timeout(50)
    assert level("Reading lamp") == "70%", "back on at the level it had before the drag"

    assert d.visible(".lights-off") and big(d.page.locator(".lights-off").bounding_box())
    d.tap(".lights-off")
    assert lights_sent()[-1] == {"type": "lights_off"}
    assert not d.page.locator('.dev[aria-checked="true"]').count() and not d.visible(".lights-off")
    d.close()


def swipe(d, to, frm=8, y=240, pause=0, release=True):
    """A finger from x=frm (the left edge) to x=to. pause: real ms between moves (slow, so not a flick)."""
    if frm < 30:
        d.page.wait_for_timeout(300)  # a sheet just opened is still sliding up (real time), its edge not there yet
    d.page.mouse.move(frm, y)
    d.page.mouse.down()
    for x in range(frm, to, 24 if to > frm else -24):
        d.page.mouse.move(x, y)
        if pause:
            d.page.wait_for_timeout(pause)
    d.page.mouse.move(to, y)
    if release:
        d.page.mouse.up()
        d.page.clock.run_for(600)  # past the settle's timer (and the dashboard's, landing there)
        d.page.wait_for_timeout(300)  # and their transitions (CSS runs in real time)


def test_rooms_and_back(b):
    d = Display(b)
    opened = lambda: [m for m in d.sent if m.get("type") == "open"]  # noqa: E731

    # Rooms: each room's name, and what's on in it; a room with a light on is lit
    d.tap("#menu")
    assert d.page.locator("#sheet-edge").is_visible(), "the menu goes back to the dashboard from the edge"
    d.page.locator(".menu-row").first.tap()  # Home
    d.push({"type": "card", "card": HOUSE})
    assert d.text("#sheet-title") == "Home" and d.page.locator("#sheet-edge").is_hidden(), "a row page has no back"
    d.tap(".tile-rooms")
    rooms = [{"id": "living_room", "name": "Living room", "lights": 3, "lights_on": 1, "playing": ["TV"], "switched_on": []},
             {"id": "bedroom", "name": "Bedroom", "lights": 2, "lights_on": 0, "playing": [], "switched_on": ["Ceiling fan"]},
             {"id": "garage", "name": "Garage", "lights": 0, "lights_on": 0, "playing": [], "switched_on": []}]
    d.push({"type": "card", "card": {"kind": "rooms", "rooms": rooms}})
    assert d.text("#sheet-title") == "Rooms"
    assert d.page.locator(".room-name").all_inner_texts() == ["Living room", "Bedroom", "Garage"]
    assert [r.inner_text().split("\n")[1:] for r in d.page.locator(".room").all()] == [
        ["1 light on", "TV playing"], ["Lights off", "Ceiling fan on"], ["Nothing on"]]
    assert d.page.locator(".room.lit").all_inner_texts()[0].startswith("Living room")
    assert d.page.locator(".room.lit").count() == 1
    d.shot("rooms", settle=400)

    # A room: its devices by kind. Lights and switches switch; players and thermostats open their own pages.
    d.page.locator(".room").nth(1).tap()
    assert opened()[-1] == {"type": "open", "sheet": "room", "room": "bedroom"}
    room = {"kind": "room", "name": "Bedroom",
            "lights": [{"entity": "light.bed", "name": "Bedside", "on": False, "brightness": 20}],
            "media": [{"entity": "media_player.tv", "name": "TV", "state": "playing"},
                      {"entity": "media_player.sonos", "name": "Sonos", "state": "idle"}],
            "climate": [{"entity": "climate.t", "name": "Thermostat", "temp": 68, "target": 70, "action": "heating"}],
            "switches": [{"entity": "switch.fan", "name": "Ceiling fan", "on": True, "brightness": None}]}
    d.push({"type": "card", "card": room})
    assert d.text("#sheet-title") == "Bedroom"
    assert d.page.locator(".dev-group-name").all_inner_texts() == ["Lights", "Speakers & TVs", "Climate", "Other"]
    level = lambda name: d.page.locator(f'.dev[aria-label="{name}"] .dev-level').first.inner_text()  # noqa: E731
    lit = lambda name: d.page.locator(f'.dev[aria-label="{name}"]').evaluate("t => t.style.getPropertyValue('--b')")  # noqa: E731
    assert [(level(n), lit(n)) for n in ("TV", "Sonos", "Thermostat", "Ceiling fan")] == [
        ("Playing", "100%"), ("Idle", "0%"), ("68°", "0%"), ("On", "100%")]
    d.shot("room", settle=400)
    d.page.locator('.dev[aria-label="Ceiling fan"]').tap()
    d.page.locator('.dev[aria-label="TV"]').tap()
    d.page.wait_for_timeout(50)
    assert {"type": "light", "entity": "switch.fan", "on": False} in d.sent
    assert opened()[-1] == {"type": "open", "sheet": "media", "entity": "media_player.tv"}

    # A refresh of the same page doesn't stack. Swiping in from the left edge slides the page off over the one it
    # opened over, drawn underneath while the finger is down; let go past a third and it goes back, asking for that
    # page again, fresh.
    d.push({"type": "card", "card": dict(room, switches=[])})
    edge = d.page.locator("#sheet-edge").bounding_box()
    assert edge["x"] == 0 and edge["width"] <= 36, "the edge stays inside the sheet's padding, covering nothing"
    swipe(d, 320, release=False)
    assert d.visible("#sheet-under") and d.text("#under-title") == "Rooms"
    assert d.page.locator("#under-body .room-name").all_inner_texts() == ["Living room", "Bedroom", "Garage"]
    moved = d.page.eval_on_selector("#sheet", "s => s.getBoundingClientRect().left")
    assert 300 <= moved <= 330, moved
    d.shot("swipe-back", settle=0)
    d.page.mouse.up()
    d.page.clock.run_for(300)
    d.page.wait_for_timeout(50)
    assert d.text("#sheet-title") == "Rooms" and opened()[-1] == {"type": "open", "sheet": "rooms"}
    assert not d.visible("#sheet-under") and d.page.eval_on_selector("#sheet", "s => s.getBoundingClientRect().left") == 0
    swipe(d, 150, pause=40)  # short and slow: springs back
    assert d.text("#sheet-title") == "Rooms" and not d.visible("#sheet-under")
    assert d.page.eval_on_selector("#sheet", "s => s.getBoundingClientRect().left") == 0
    swipe(d, 150)  # just as short, but a flick: goes back
    assert d.text("#sheet-title") == "Home" and opened()[-1] == {"type": "open", "sheet": "house"}
    assert d.page.locator("#sheet-edge").is_hidden(), "Home is top level: nothing to go back to"
    d.tap(".tile-rooms")
    d.push({"type": "card", "card": {"kind": "rooms", "rooms": rooms}})
    d.page.focus("#sheet-edge")
    d.page.keyboard.press("Enter")  # no swiping from a keyboard: the edge is a Back button
    d.page.wait_for_timeout(50)
    assert d.text("#sheet-title") == "Home"

    # Going home forgets the way back: the next sheet opens over the dashboard, not the Home it was over before
    d.tap(".tile-rooms")
    d.push({"type": "card", "card": {"kind": "rooms", "rooms": rooms}})
    d.page.clock.run_for(46_000)  # (left alone, the sheet goes home)
    d.push({"type": "card", "card": {"kind": "rooms", "rooms": rooms}})
    swipe(d, 500)
    assert not d.visible("#sheet")
    d.close()


def test_swiping_between_sheets(b):
    """Right to left from the dashboard goes along the row (the menu's order), left to right comes back; it stops
    at both ends. Pages the brain serves are fetched ahead (as the page connects, and either side of each landing),
    show what was fetched while swiped in (just a title until it's back), and are asked for again on landing."""
    d = Display(b)
    opened = lambda: [m["sheet"] for m in d.sent if m.get("type") == "open"]  # noqa: E731
    fetched = lambda: [m["sheet"] for m in d.sent if m.get("type") == "fetch"]  # noqa: E731
    title = lambda: d.text("#sheet-title") if d.visible("#sheet") else "dashboard"  # noqa: E731
    left = lambda sel: d.page.eval_on_selector(sel, "s => s.getBoundingClientRect().left")  # noqa: E731
    d.push(ambient([{"id": 1, "label": "pasta", "ends_at": START.timestamp() + 300, "duration": 600,
                     "paused_left": None}], weather=None))

    assert fetched() == ["house", "alarms", "shopping"], "the row's pages from the brain, fetched as the page connects"

    # Partway: the dashboard, drawn as a sheet (its title, the room's photo in a sheet's margins), and Home side by
    # side, both 250px along; Home just its title, its fetch not back yet...
    swipe(d, 450, frm=700, y=200, release=False)
    assert d.visible("#sheet-under") and d.text("#under-title") == "Home" and not d.text("#under-body")
    d.push({"type": "page", "card": HOUSE})  # ...until it is: drawn at once, and nothing opens
    assert d.page.locator("#under-body .tile").count() == 5 and not d.visible("#sheet")
    assert d.visible("#sheet-dash") and d.text("#sheet-dash .sheet-title") == "Dashboard"
    assert 530 <= left("#sheet-under") <= 570 and -270 <= left("#sheet-dash") <= -230, (left("#sheet-under"), left("#sheet-dash"))
    assert d.page.eval_on_selector("#home", "h => getComputedStyle(h).opacity") != "1", "the clock and the rest fade away"
    assert d.page.eval_on_selector(".dash-bg", "b => getComputedStyle(b).opacity") == "1", \
        "solid from the start: the shrinking photo uncovers it, never the real photo fading out behind"
    d.page.wait_for_timeout(350)  # the photo shrinking into its frame (real time)
    photo = d.page.locator("#dash-photo").bounding_box()
    assert round(photo["x"] - left("#sheet-dash")) == 36 and round(photo["width"]) == 800 - 2 * 36, photo
    assert d.page.eval_on_selector("#home", "h => getComputedStyle(h).opacity") == "0"
    home = d.page.locator("#home").bounding_box()
    assert home["x"] >= photo["x"] - 1 and home["x"] + home["width"] <= photo["x"] + photo["width"] + 1 and \
        home["y"] >= photo["y"] - 1 and home["y"] + home["height"] <= photo["y"] + photo["height"] + 1, \
        f"the clock and the rest shrink to fit inside the photo: {home} in {photo}"
    assert not d.visible("#swipe-dim"), "side by side, nothing is covered, so nothing dims"
    d.page.mouse.up()
    d.page.clock.run_for(300)
    top = lambda: d.page.eval_on_selector("#sheet", "s => s.getBoundingClientRect().top")  # noqa: E731
    tops = [top()] + [d.page.wait_for_timeout(40) or top() for _ in range(5)]
    assert tops == [0] * 6, f"landed in place, not sliding up from the bottom: {tops}"
    d.page.wait_for_timeout(100)
    assert title() == "Home" and opened() == ["house"] and left("#sheet") == 0 and not d.visible("#swipe-dim")
    assert not d.visible("#sheet-dash") and d.page.locator("#sheet-body .tile").count() == 5
    assert d.page.locator("#sheet-edge").is_hidden(), "a row page has no back swipe"

    # On along the row; a swipe that starts on a tile doesn't tap it
    seen = []
    for _ in range(6):
        swipe(d, 300, frm=700, y=200)
        seen.append(title())
    assert seen == ["Timers", "Alarms", "Shopping list", "Music", "Music", "Music"], seen
    swipe(d, 300, frm=700, y=200, release=False)
    assert left("#sheet") == 0 and not d.visible("#sheet-under"), "past the end nothing moves, nothing shows behind"
    d.page.mouse.up()
    d.page.clock.run_for(600)
    assert title() == "Music"
    assert opened() == ["house", "alarms", "shopping"], "only pages the brain serves are asked for, on landing"
    assert fetched()[3:] == ["house", "alarms", "shopping", "alarms", "shopping"], "and the ones either side of each landing"
    assert d.page.locator("#sheet-edge").is_hidden()

    # And back, to the dashboard and no further
    seen = []
    for _ in range(7):
        swipe(d, 500, frm=100, y=200)
        seen.append(title())
    assert seen == ["Shopping list", "Alarms", "Timers", "Home", "dashboard", "dashboard", "dashboard"], seen
    assert not d.visible("#sheet-dash") and d.page.eval_on_selector("#home", "h => getComputedStyle(h).opacity") == "1", \
        "landed: the photo grows back out and the clock and the rest come back"
    assert d.page.eval_on_selector("#home", "h => getComputedStyle(h).transform") == "none"

    # Coming back from Home the dashboard arrives already framed: nothing plays while it slides in
    swipe(d, 300, frm=700, y=200)
    swipe(d, 350, frm=100, y=200, release=False)
    photo = d.page.locator("#dash-photo").bounding_box()
    fit = min(photo["width"] / 800, photo["height"] / 480)
    assert d.page.eval_on_selector("#home", "h => getComputedStyle(h).opacity") == "0"
    shrunk = d.page.eval_on_selector("#home", "h => new DOMMatrix(getComputedStyle(h).transform).a")
    assert abs(shrunk - fit) < 0.005, f"already shrunk to {fit}, not shrinking ({shrunk})"
    d.page.mouse.up()
    d.page.clock.run_for(600)
    d.page.wait_for_timeout(300)
    assert title() == "dashboard"

    # A page seen before shows its last card while it's swiped in
    swipe(d, 300, frm=700, y=200)
    swipe(d, 450, frm=700, y=200, release=False)
    assert d.text("#under-title") == "Timers" and d.page.locator("#under-body .tmr").count() == 1
    d.page.mouse.up()
    d.page.clock.run_for(300)
    d.page.wait_for_timeout(300)
    assert title() == "Timers"
    d.push({"type": "card", "card": {"kind": "alarms", "alarms": [alarm(1, "06:30", "01234", "gym")]}})
    assert title() == "Alarms"
    swipe(d, 500, frm=100, y=200)
    assert title() == "Timers"
    swipe(d, 450, frm=700, y=200, release=False)
    assert d.text("#under-title") == "Alarms" and d.page.locator("#under-body .alarm-row").count() == 1
    d.page.mouse.up()
    d.page.clock.run_for(300)
    d.page.wait_for_timeout(300)
    assert title() == "Alarms"
    swipe(d, 300, frm=700, y=200)
    assert title() == "Shopping list"

    # Up and down is a scroll, not a swipe; a short slow drag springs back
    d.push({"type": "card", "card": {"kind": "shopping", "groups": [{"name": "", "items": [f"Item {i}" for i in range(12)]}]}})
    d.page.mouse.move(400, 300)
    d.page.mouse.down()
    d.page.mouse.move(430, 200, steps=5)
    d.page.mouse.move(440, 100, steps=5)
    d.page.mouse.up()
    d.page.wait_for_timeout(50)
    assert title() == "Shopping list" and not d.visible("#sheet-under")
    swipe(d, 600, frm=700, y=200, pause=40)
    assert title() == "Shopping list" and left("#sheet") == 0

    # A subpage has only its edge; a card Claude puts up isn't in the row; the menu still works
    d.page.clock.run_for(46_000)  # (left alone, the sheet goes home)
    d.push({"type": "card", "card": HOUSE})
    d.tap(".tile-lights")
    d.push({"type": "card", "card": {"kind": "lights", "rooms": []}})
    swipe(d, 300, frm=700, y=200)
    assert title() == "Lights"
    d.push({"type": "card", "card": {"kind": "weather", **WEATHER}})
    swipe(d, 300, frm=700, y=200)
    assert title() == "New York"
    d.page.clock.run_for(46_000)
    d.tap("#menu")
    assert title() == "Menu"
    swipe(d, 300, frm=700, y=200)
    assert title() == "Menu", "the menu isn't in the row"
    d.close()


def thermostat(entity, name, temp, target, mode, action, modes=("heat", "cool", "off"), humidity=None):
    return {"entity": entity, "name": name, "temp": temp, "target": target, "mode": mode, "action": action,
            "modes": list(modes), "min": 50, "max": 90, "step": 1, "humidity": humidity}


def test_climate(b):
    d = Display(b)
    climate_sent = lambda: [m for m in d.sent if m.get("type") == "climate"]  # noqa: E731
    living = thermostat("climate.living", "Thermostat", 68, 70, "heat", "heating", humidity=41)
    ac = thermostat("climate.ac", "Air conditioner", 74, 71, "cool", "cooling", modes=("cool", "fan_only", "off"))
    d.push({"type": "card", "card": {"kind": "climate", "thermostats": [living, ac]}})
    assert d.text("#sheet-title") == "Climate"
    page = lambda i: d.page.locator(".thermo").nth(i)  # noqa: E731
    assert page(0).locator(".th-target").inner_text() == "70°"
    assert page(0).locator(".th-status").inner_text() == "Heating, 68° now, 41% humidity"
    assert page(1).locator(".th-status").inner_text() == "Cooling, 74° now"
    assert page(0).locator(".pill").all_inner_texts() == ["Heat", "Cool", "Off"]
    assert page(1).locator(".pill").all_inner_texts() == ["Cool", "Fan", "Off"]
    assert page(0).locator('.pill[aria-pressed="true"]').inner_text() == "Heat"
    assert "heating" in page(0).get_attribute("class") and "cooling" in page(1).get_attribute("class")
    # The scale is 20° around the room and the setting; the fill runs from the room's dot to the setting's mark
    assert page(0).locator(".th-scale").inner_text().split() == ["60°", "80°"]
    fill = page(0).locator(".th-fill").bounding_box()
    dot, mark = page(0).locator(".th-now").bounding_box(), page(0).locator(".th-set").bounding_box()
    assert abs(fill["x"] - (dot["x"] + dot["width"] / 2)) < 1 and abs(fill["x"] + fill["width"] - (mark["x"] + 2)) < 1
    for sel in ('[aria-label^="Lower"]', '[aria-label^="Raise"]'):
        assert big(page(0).locator(sel).bounding_box(), w=80, h=80)
    d.shot("climate", settle=400)

    # + three times is one change, sent once the taps stop
    for _ in range(3):
        page(0).locator('[aria-label^="Raise"]').tap()
    assert page(0).locator(".th-target").inner_text() == "73°" and not climate_sent()
    d.page.clock.run_for(800)
    assert climate_sent() == [{"type": "climate", "entity": "climate.living", "target": 73}]

    # A mode; then the brain's fresh card keeps the page showing (the AC, swiped to) rather than jumping back
    d.page.locator(".thermostats").evaluate("p => { p.style.scrollSnapType = 'none'; p.scrollLeft = p.scrollWidth; }")
    page(1).locator(".pill", has_text="Off").tap()
    d.page.wait_for_timeout(50)
    assert climate_sent()[-1] == {"type": "climate", "entity": "climate.ac", "mode": "off"}
    assert page(1).locator(".th-target").inner_text() == "Off" and not page(1).locator(".th-steps").is_visible()
    left = d.page.locator(".thermostats").evaluate("p => p.scrollLeft")
    d.push({"type": "card", "card": {"kind": "climate", "thermostats": [living, dict(ac, mode="off", action="off")]}})
    d.page.wait_for_timeout(50)
    assert d.page.locator(".thermostats").evaluate("p => p.scrollLeft") == left > 0

    # Opened from a room's thermostat tile, that thermostat shows first
    swipe(d, 500)  # (back from the edge, to the dashboard it opened over)
    d.push({"type": "card", "card": {"kind": "room", "name": "Bedroom", "lights": [], "media": [],
                                     "climate": [ac], "switches": []}})
    d.page.locator('.dev[aria-label="Air conditioner"]').tap()
    assert d.sent[-1] == {"type": "open", "sheet": "climate", "entity": "climate.ac"}
    d.push({"type": "card", "card": {"kind": "climate", "thermostats": [living, ac]}})
    d.page.wait_for_timeout(50)
    pages = d.page.locator(".thermostats")
    assert pages.evaluate("p => p.scrollLeft") == pages.evaluate("p => p.children[1].offsetLeft - p.offsetLeft")

    d.push({"type": "card", "card": {"kind": "climate", "thermostats": []}})
    assert d.text("#sheet-body .quiet") == "No thermostats."
    d.close()


def player(entity, room, name, state, title=None, artist=None, app=None, volume=30):
    return {"entity": entity, "room": room, "name": name, "state": state, "title": title, "artist": artist,
            "app": app, "volume": volume}


def test_speakers_and_tvs(b):
    d = Display(b)
    media_sent = lambda: [m for m in d.sent if m.get("type") == "media"]  # noqa: E731
    players = [player("media_player.kitchen", "Kitchen", "Speaker", "paused", "Harvest Moon", "Neil Young", volume=40),
               player("media_player.tv", "Living room", "TV", "playing", "The Bear", app="Netflix", volume=22),
               player("media_player.sonos", "Living room", "Sonos", "idle", volume=35),
               player("media_player.bedroom", "Bedroom", "TV", "off")]
    d.push({"type": "card", "card": {"kind": "media", "players": players}})
    assert d.text("#sheet-title") == "Speakers & TVs"
    panel = lambda i: d.page.locator(".player").nth(i)  # noqa: E731
    lines = lambda p: [line for line in p.inner_text().split("\n") if line.strip()]  # noqa: E731
    assert [lines(p)[1:] for p in d.page.locator(".player").all()][:2] == [
        ["Speaker", "Harvest Moon", "Neil Young", "Paused", "40"], ["TV", "The Bear", "Netflix", "22"]]
    assert d.page.locator(".player.lit").count() == 1 and "lit" in panel(1).get_attribute("class")
    assert panel(2).locator(".pl-state").inner_text() == "Nothing on" and panel(2).locator(".pl-btn.main").is_hidden()
    assert panel(3).locator(".pl-state").inner_text() == "Off" and panel(3).locator(".pl-vol").is_hidden()
    assert panel(3).locator(".pl-btn:visible").count() == 1, "off: only power"
    for btn in panel(0).locator(".pl-btn").all():
        assert big(btn.bounding_box()), btn.get_attribute("aria-label")
    d.shot("media", settle=400)

    for _ in range(3):
        panel(1).locator('[aria-label="Turn up the TV"]').tap()
    assert panel(1).locator(".pl-volume").inner_text() == "37" and not media_sent()
    d.page.clock.run_for(800)
    assert media_sent() == [{"type": "media", "entity": "media_player.tv", "volume": 37}]
    panel(1).locator('[aria-label="Pause the TV"]').tap()
    panel(0).locator('[aria-label="Play the Speaker"]').tap()
    panel(3).locator('[aria-label="Turn on the TV"]').tap()
    d.page.wait_for_timeout(50)
    assert [m.get("action") for m in media_sent()[1:]] == ["pause", "play", "turn_on"]
    assert d.page.locator(".player.lit").count() == 1 and "lit" in panel(0).get_attribute("class")

    # Opened from a room's TV, that one shows first
    swipe(d, 500)  # (back from the edge, to the dashboard it opened over)
    d.push({"type": "card", "card": {"kind": "room", "name": "Bedroom", "lights": [], "climate": [], "switches": [],
                                     "media": [{"entity": "media_player.bedroom", "name": "TV", "state": "off"}]}})
    d.page.locator('.dev[aria-label="TV"]').tap()
    assert d.sent[-1] == {"type": "open", "sheet": "media", "entity": "media_player.bedroom"}
    d.push({"type": "card", "card": {"kind": "media", "players": players}})
    d.page.wait_for_timeout(50)
    row = d.page.locator(".players")
    assert row.evaluate("r => r.scrollLeft") == row.evaluate("r => r.scrollWidth - r.clientWidth") > 0
    d.close()


def test_home_updates_and_unavailable(b):
    """The house changing refreshes the Home page that's open, and only that, never opening one; a device Home
    Assistant can't reach looks it and takes no taps; an off or missing thermostat reads as such."""
    d = Display(b)
    taps = lambda: [m for m in d.sent if m.get("type") in ("light", "lights_off", "media", "climate")]  # noqa: E731
    level = lambda name: d.page.locator(f'.dev[aria-label="{name}"] .dev-level').first.inner_text()  # noqa: E731
    lamp = {"entity": "light.lamp", "name": "Reading lamp", "on": True, "brightness": 40}
    lantern = {"entity": "light.lantern", "name": "Lantern", "on": False, "brightness": 60, "unavailable": True}
    rooms = [{"name": f"Room {i}", "lights": [{"entity": f"light.r{i}", "name": f"Light {i}", "on": False,
                                               "brightness": 50}]} for i in range(8)]

    def lights(lamp_now):
        return {"kind": "lights", "rooms": [{"name": "Living room", "lights": [lamp_now]},
                                            {"name": "Porch", "lights": [lantern]}, *rooms]}

    # Nothing open: an update opens nothing
    d.push({"type": "update", "card": lights(lamp)})
    assert not sheet_open(d)

    # Lights open: an update redraws it in place, where it was scrolled to
    d.push({"type": "card", "card": lights(lamp)})
    assert level("Reading lamp") == "40%"
    assert d.page.locator(".dev.gone").count() == 1 and level("Lantern") == "Unavailable"
    d.shot("lights-unavailable", settle=400)
    d.page.eval_on_selector(".dev-groups", "e => { e.scrollLeft = 200 }")
    d.push({"type": "update", "card": lights({**lamp, "brightness": 90})})
    assert level("Reading lamp") == "90%"
    assert d.page.eval_on_selector(".dev-groups", "e => e.scrollLeft") == 200, "the update kept the scroll"
    d.page.eval_on_selector(".dev-groups", "e => { e.scrollLeft = 0 }")

    # Unavailable: no tap, no drag, nothing sent
    d.page.tap(".dev.gone", force=True)
    d.page.eval_on_selector(".dev.gone", """b => {
        const at = (type, y) => b.dispatchEvent(new PointerEvent(type, {bubbles: true, isPrimary: true, clientX: b.getBoundingClientRect().x + 20, clientY: y}));
        at("pointerdown", 300); at("pointermove", 200); at("pointerup", 200); }""")
    d.page.wait_for_timeout(100)
    assert taps() == [] and level("Lantern") == "Unavailable"

    # A finger on the sheet: the update waits for the lift
    d.page.dispatch_event("#sheet", "pointerdown")
    d.push({"type": "update", "card": lights({**lamp, "brightness": 20})})
    assert level("Reading lamp") == "90%", "applied mid-touch"
    d.page.evaluate("document.dispatchEvent(new PointerEvent('pointerup'))")
    d.page.wait_for_timeout(100)
    assert level("Reading lamp") == "20%"
    # ...and a tap sent meanwhile drops it: the tap's own update brings everything since
    d.page.dispatch_event("#sheet", "pointerdown")
    d.push({"type": "update", "card": lights({**lamp, "brightness": 70})})
    d.tap('.dev[aria-label="Reading lamp"]')
    d.page.wait_for_timeout(100)
    assert taps()[-1] == {"type": "light", "entity": "light.lamp", "on": False} and level("Reading lamp") == "Off"

    # A room: an update for another room is ignored, its own applied; an unavailable player is a dark window too
    speaker = {"entity": "media_player.speaker", "name": "Speaker", "state": "unavailable", "title": None,
               "artist": None, "app": None, "volume": None, "unavailable": True}
    bedroom = {"kind": "room", "name": "Bedroom", "lights": [lamp], "media": [speaker], "climate": [], "switches": []}
    d.push({"type": "card", "card": bedroom})
    d.push({"type": "update", "card": {**bedroom, "name": "Kitchen", "lights": [{**lamp, "on": False}]}})
    assert d.text("#sheet-title") == "Bedroom" and level("Reading lamp") == "40%"
    d.push({"type": "update", "card": {**bedroom, "lights": [{**lamp, "on": False}]}})
    assert level("Reading lamp") == "Off" and level("Speaker") == "Unavailable"
    d.shot("room-unavailable", settle=400)

    # Speakers & TVs: the unavailable one says so, with no buttons
    d.push({"type": "card", "card": {"kind": "media", "players": [{**speaker, "room": "Bedroom"}]}})
    gone = d.page.locator(".player.gone")
    assert gone.count() == 1 and gone.locator(".pl-state").inner_text() == "Unavailable"
    assert not gone.locator(".pl-btn").first.is_visible()
    d.shot("media-unavailable", settle=400)

    # Climate: off hides − and +; unavailable hides everything but the word
    off = thermostat("climate.house", "Thermostat", 71, 71, "off", "off", modes=("off", "cool", "heat"), humidity=44)
    gone_t = {**thermostat("climate.ac", "Window AC", None, None, "unavailable", "off", modes=()), "unavailable": True}
    d.push({"type": "card", "card": {"kind": "climate", "thermostats": [off, gone_t]}})
    page = lambda i: d.page.locator(".thermo").nth(i)  # noqa: E731
    assert page(0).locator(".th-target").inner_text() == "Off"
    assert not page(0).locator('[aria-label^="Raise"]').is_visible() and page(0).locator(".pill").first.is_visible()
    d.shot("climate-off", settle=400)
    assert page(1).locator(".th-target").inner_text() == "Unavailable"
    assert not page(1).locator('[aria-label^="Raise"]').is_visible() and not page(1).locator(".th-track").is_visible()
    d.page.eval_on_selector(".thermostats", "e => { e.scrollLeft = e.scrollWidth }")
    d.shot("climate-unavailable", settle=400)

    # Set to cool above the room but between cycles (hvac_action "fan"): still the ice stretch; the status says fan
    waiting = thermostat("climate.house", "Thermostat", 71, 69, "cool", "fan", modes=("off", "cool", "heat"))
    d.push({"type": "card", "card": {"kind": "climate", "thermostats": [waiting]}})
    assert "cooling" in page(0).get_attribute("class") and page(0).locator(".th-fill").is_visible()
    assert page(0).locator(".th-status").inner_text() == "Fan running, 71° now"
    d.shot("climate-cooling-fan", settle=400)
    # A window AC that only ever says idle: heat below the setting is ember, at the setting nothing
    d.push({"type": "card", "card": {"kind": "climate", "thermostats": [
        thermostat("climate.ac", "Window AC", 67, 70, "heat", "idle"), thermostat("climate.ac2", "AC", 68, 68, "cool", "idle")]}})
    assert "heating" in page(0).get_attribute("class") and not page(1).locator(".th-fill").is_visible()

    # Home: an off thermostat reads Off; between cycles it reads its setting; none at all reads None
    d.push({"type": "card", "card": {**HOUSE, "climate": {"temp": 71, "target": 69, "action": "fan"}}})
    assert d.page.locator(".tile-detail").all_inner_texts()[1] == "Set to 69°"
    d.push({"type": "card", "card": {**HOUSE, "climate": {"temp": 71, "target": 71, "action": "off"}}})
    assert d.page.locator(".tile-num").all_inner_texts()[1] == "71°"
    assert d.page.locator(".tile-detail").all_inner_texts()[1] == "Off"
    d.shot("house-climate-off", settle=400)
    d.push({"type": "update", "card": {**HOUSE, "climate": None}})
    assert d.page.locator(".tile-num").all_inner_texts()[1] == "None"
    assert d.page.locator(".tile-detail").all_inner_texts()[1] == "No thermostats"
    d.shot("house-no-climate", settle=400)
    d.close()


def test_automations(b):
    d = Display(b)
    yesterday_2302 = dt.datetime(2026, 10, 6, 23, 2).timestamp()
    autos = [{"entity": "automation.goodnight", "name": "Goodnight: everything off", "on": True, "last_ran": yesterday_2302},
             {"entity": "automation.porch", "name": "Porch light at dusk", "on": True,
              "last_ran": dt.datetime(2026, 10, 7, 6, 30).timestamp()},
             {"entity": "automation.ac", "name": "AC off when everyone leaves", "on": True,
              "last_ran": dt.datetime(2026, 10, 5, 8, 15).timestamp()},
             {"entity": "automation.holiday", "name": "Holiday lights", "on": False,
              "last_ran": dt.datetime(2026, 1, 2, 17, 0).timestamp()},
             {"entity": "automation.vacation", "name": "Vacation lights", "on": False, "last_ran": None}]
    d.push({"type": "card", "card": {"kind": "automations", "automations": autos}})
    assert d.text("#sheet-title") == "Automations"
    assert d.page.locator(".auto-when").all_inner_texts() == [
        "Ran yesterday at 11:02pm", "Ran today at 6:30am", "Ran Monday", "Ran January 2", "Hasn't run yet"]
    assert d.page.locator(".auto-row.off .auto-name").all_inner_texts() == ["Holiday lights", "Vacation lights"]
    for row in d.page.locator(".auto-row").all()[:3]:
        assert big(row.locator(".pill").bounding_box()) and big(row.locator(".switch").bounding_box(), w=64, h=64)
    d.shot("automations", settle=400)

    d.page.locator('[aria-label="Run AC off when everyone leaves"]').tap()
    d.page.locator('.switch[aria-label="Vacation lights"]').tap()
    d.page.wait_for_timeout(50)
    assert [m for m in d.sent if m["type"].startswith("automation")] == [
        {"type": "automation_run", "entity": "automation.ac"},
        {"type": "automation_toggle", "entity": "automation.vacation", "enabled": True}]
    assert d.page.locator(".auto-when").nth(2).inner_text() == "Ran today at 9:41am"
    assert d.page.locator(".auto-row.off").count() == 1

    d.push({"type": "card", "card": {"kind": "automations", "automations": []}})
    assert d.text("#sheet-body .quiet") == "No automations."
    d.close()


def test_header(b):
    """Over every sheet a header with the time on the right. Along the row: every row page's title, the one you're on
    at the left and underlined, sliding under the underline as you swipe. Elsewhere: the sheet's own title."""
    d = Display(b)
    opacity = lambda sel: d.page.eval_on_selector(sel, "e => getComputedStyle(e).opacity")  # noqa: E731
    tab = lambda i: d.page.locator(".tab").nth(i).bounding_box()  # noqa: E731
    line = lambda: d.page.locator("#tab-line").bounding_box()  # noqa: E731
    assert opacity("#header-home") == "0" and opacity("#header-tabs") == "0", "the dashboard has no header"

    d.push({"type": "card", "card": HOUSE})
    d.page.wait_for_timeout(300)  # its fade (real time)
    assert opacity("#header-home") == "1" and d.text("#header-time") == "9:41"
    assert d.page.locator(".tab").all_inner_texts() == [
        "Dashboard", "Home", "Timers", "Alarms", "Shopping list", "Music"]
    assert round(tab(1)["x"]) == 36 and round(line()["x"]) == 36 and abs(line()["width"] - tab(1)["width"]) < 1
    assert d.page.eval_on_selector("#sheet-title", "t => getComputedStyle(t).color") == "rgba(0, 0, 0, 0)", \
        "a row page's own title gives way to the header's"
    d.page.clock.run_for(60_000)  # (the sheet goes home after 45 s of it: open it again)
    d.push({"type": "card", "card": HOUSE})
    assert d.text("#header-time") == "9:42", "the time keeps up"

    # Halfway through a swipe the titles are halfway along, and the underline halfway between the two widths
    home_w, timers_x, timers_w = tab(1)["width"], tab(2)["x"], tab(2)["width"]
    swipe(d, 300, frm=700, y=200, release=False)
    assert abs(tab(2)["x"] - (timers_x - (timers_x - 36) / 2)) < 2, tab(2)
    assert abs(line()["width"] - (home_w + timers_w) / 2) < 2 and round(line()["x"]) == 36
    d.shot("header-swipe", settle=0)
    d.page.mouse.up()
    d.page.clock.run_for(600)
    d.page.wait_for_timeout(300)
    assert d.text("#sheet-title") == "Timers" and round(tab(2)["x"]) == 36 and abs(line()["width"] - timers_w) < 1
    assert opacity(".tab:nth-child(3)") == "1" and float(opacity(".tab:nth-child(2)")) < 1

    # A subpage, or a card: its own title, no row titles, the time still there
    d.push({"type": "card", "card": {"kind": "lights", "rooms": []}})
    assert d.text("#sheet-title") == "Lights" and opacity(".tabs-wrap") == "0" and opacity("#header-home") == "1"
    assert d.page.eval_on_selector("#sheet-title", "t => getComputedStyle(t).color") != "rgba(0, 0, 0, 0)"
    d.shot("header-subpage", settle=300)
    # Back from the edge: the page sliding off takes its own copy of the time; the header's is under it with Timers
    swipe(d, 300, release=False)
    sheet_time = d.page.locator("#sheet-time").bounding_box()
    assert d.text("#sheet-time") == "9:42" and round(sheet_time["x"] + sheet_time["width"]) == (300 - 8) + 800 - 36, sheet_time
    assert d.page.eval_on_selector("#header-home", "h => getComputedStyle(h).zIndex") == "2"
    d.page.mouse.up()
    d.page.clock.run_for(600)
    d.page.wait_for_timeout(300)
    assert d.text("#sheet-title") == "Timers" and opacity(".tabs-wrap") == "1"
    assert not d.visible("#sheet-time") and d.page.eval_on_selector("#header-home", "h => getComputedStyle(h).zIndex") == "5"

    # The time takes you back to the dashboard, from anywhere along the row
    swipe(d, 300, frm=700, y=200)
    swipe(d, 300, frm=700, y=200)
    assert d.text("#sheet-title") == "Shopping list"
    assert big(d.page.locator("#header-home").bounding_box(), w=64, h=64)
    d.tap("#header-home")
    assert not d.visible("#sheet")

    # The menu, and a card Claude puts up, open over the dashboard and go back to it from the edge
    d.page.clock.run_for(46_000)
    d.tap("#menu")
    assert d.page.locator("#sheet-edge").is_visible() and opacity(".tabs-wrap") == "0"
    swipe(d, 500)
    assert not d.visible("#sheet")
    d.push({"type": "card", "card": {"kind": "text", "title": "Note", "lines": ["Hello"]}})
    assert d.page.locator("#sheet-edge").is_visible()
    swipe(d, 500)
    assert not d.visible("#sheet")
    d.page.wait_for_timeout(300)
    assert opacity("#header-home") == "0", "back on the dashboard, no header"
    d.close()


def test_navigation_every_screen_gets_home(b):
    """From every screen there is a visible way home, and every sheet goes home on its own when left alone."""
    screens = {
        "weather": {"kind": "weather", **WEATHER},
        "timers": {"kind": "timers", "timers": []},
        "alarms": {"kind": "alarms", "alarms": []},
        "shopping": {"kind": "shopping", "groups": [{"name": "", "items": ["Milk"]}]},
        "text": {"kind": "text", "title": "Note", "lines": ["Hello"]},
        "agenda": {"kind": "agenda", "events": []},
        "briefing": briefing()["card"],
        "music": {"kind": "music"},
        "house": HOUSE,
        "lights": {"kind": "lights", "rooms": []},
        "rooms": {"kind": "rooms", "rooms": []},
        "media": {"kind": "media", "players": [player("media_player.tv", "Living room", "TV", "playing", "The Bear")]},
        "automations": {"kind": "automations", "automations": []},
        "climate": {"kind": "climate", "thermostats": [thermostat("climate.t", "Thermostat", 68, 70, "heat", "heating")]},
        "room": {"kind": "room", "name": "Porch", "lights": [], "media": [], "climate": [], "switches": []},
    }
    d = Display(b)
    home = lambda: not d.visible("#sheet") and not d.visible("#overlay")  # noqa: E731

    def open_screen(name):
        if name == "menu":
            d.tap("#menu")
        else:
            d.push({"type": "card", "card": screens[name]})
        assert d.visible("#sheet"), name

    for name in [*screens, "menu"]:
        open_screen(name)
        if d.page.locator("#sheet-edge").is_visible():  # a card, a subpage, the menu: back from the edge
            box = d.page.locator("#sheet-edge").bounding_box()
            assert round(box["height"]) == 480, (name, box)  # (may be mid-slide: sub-pixel sizes)
            swipe(d, 500)
        else:  # a row page: along the row, left to right, to the dashboard
            for _ in range(7):
                if home():
                    break
                swipe(d, 500, frm=100, y=200)
        assert home(), f"no way home from {name}"
        open_screen(name)
        d.page.clock.run_for(45_500)
        assert home(), f"{name} didn't return home after 45 s"

    # The conversation overlay: a close button while it's up, and home 4 s after the conversation ends
    for state in ("listening", "thinking", "speaking"):
        d.push({"type": "state", "state": state})
        assert d.visible("#close-overlay"), state
        d.tap("#close-overlay")
        assert home(), f"close didn't dismiss the {state} overlay"
    d.push({"type": "state", "state": "speaking"}, {"type": "state", "state": "idle"})
    d.page.clock.run_for(4100)
    assert home()
    d.close()


def alarm(id, time_, days="", label="", enabled=True, next_ring=None):
    return {"id": id, "time": time_, "days": days, "label": label, "enabled": enabled,
            "next_ring": next_ring or (START + dt.timedelta(days=1)).timestamp()}


def test_alarms_on_screen(b):
    d = Display(b)
    tomorrow_630 = dt.datetime(2026, 10, 8, 6, 30).timestamp()
    monday_7 = dt.datetime(2026, 10, 12, 7, 0).timestamp()

    # The next alarm on the home strip: the time, then which day
    d.push(dict(ambient([], weather=None), next_alarm=alarm(1, "06:30", next_ring=tomorrow_630)))
    assert d.text(".strip .alarm .num") == "6:30am" and d.text(".strip .alarm .label") == "Alarm tomorrow"
    d.push(dict(ambient([], weather=None), next_alarm=alarm(2, "07:00", "0", "gym", next_ring=monday_7)))
    assert d.text(".strip .alarm .label") == "Gym Monday"
    d.tap(".strip .alarm")
    assert {"type": "open", "sheet": "alarms"} in d.sent

    # The alarms sheet: each alarm with its days, a switch, Delete
    alarms = [alarm(1, "06:30", "01234", "", next_ring=tomorrow_630), alarm(2, "21:15", "56", "reading", enabled=False)]
    d.push({"type": "card", "card": {"kind": "alarms", "alarms": alarms}})
    assert d.text("#sheet-title") == "Alarms" and d.page.locator(".group-label").count() == 0
    assert d.page.locator(".alarm-time").all_inner_texts() == ["6:30am", "9:15pm"]
    assert d.page.locator(".alarm-days").all_inner_texts() == ["Weekdays", "Weekends"]
    assert d.page.locator(".alarm-name").all_inner_texts() == ["Alarm", "Reading"]
    assert d.page.locator(".alarm-row.off").count() == 1
    for sel in (".switch", ".alarm-row .pill"):
        box = d.page.locator(sel).first.bounding_box()
        assert big(box), (sel, box)
    d.shot("sheet-alarms", settle=400)
    d.page.locator(".switch").first.tap()
    d.page.wait_for_timeout(50)
    assert {"type": "alarm_toggle", "id": 1, "enabled": False} in d.sent, d.sent
    assert d.page.get_attribute(".switch >> nth=0", "aria-checked") == "false", "the switch should move at once"
    d.page.locator(".alarm-row .pill").nth(1).tap()
    d.page.wait_for_timeout(50)
    assert {"type": "alarm_delete", "id": 2} in d.sent, d.sent

    # No alarms: says so, and how to set one
    d.push({"type": "card", "card": {"kind": "alarms", "alarms": []}})
    assert d.text("#sheet-body .quiet") == "No alarms set." and "wake me up" in d.text(".hint")

    # Ringing: over everything (even an open sheet), Stop and Snooze send their messages, null clears it
    d.push({"type": "ringing", "ringing": {"kind": "alarm", "id": 1, "label": "", "time": "06:30"}})
    assert d.visible("#ringing") and d.text("#ringing-time") == "6:30am" and d.text("#ringing-label") == "Alarm"
    top = d.page.evaluate("document.elementFromPoint(400, 240).closest('section').id")
    assert top == "ringing", f"ringing should be on top, found {top}"
    stop_box = d.page.locator("#ringing-stop").bounding_box()
    assert big(stop_box, w=240, h=96), stop_box
    d.shot("ringing", settle=400)
    d.tap("#snooze")
    d.tap("#ringing-stop")
    assert {"type": "snooze"} in d.sent and {"type": "stop"} in d.sent, d.sent
    d.page.clock.run_for(60_000)
    assert d.visible("#ringing"), "ringing must not time out on its own; the brain ends it"
    d.push({"type": "ringing", "ringing": None})
    assert not d.visible("#ringing")
    d.push({"type": "ringing", "ringing": {"kind": "alarm", "id": 2, "label": "time for the gym", "time": "07:00"}})
    assert d.text("#ringing-label") == "Time for the gym"
    d.shot("ringing-label", settle=400)

    # A finished timer: no time of day, its name, and nothing to snooze; an alarm after it gets Snooze back
    d.push({"type": "ringing", "ringing": {"kind": "timer", "label": "pasta"}})
    assert d.text("#ringing-time") == "Time’s up" and d.text("#ringing-label") == "Pasta timer"
    assert not d.visible("#snooze") and d.visible("#ringing-stop")
    d.shot("ringing-timer", settle=400)
    d.push({"type": "ringing", "ringing": {"kind": "timer", "label": "", "duration": 300}})
    assert d.text("#ringing-label") == "5 min timer"
    d.push({"type": "ringing", "ringing": {"kind": "timer", "label": "", "duration": None}})
    assert d.text("#ringing-label") == "Timer"
    d.push({"type": "ringing", "ringing": {"kind": "alarm", "id": 3, "label": "", "time": "07:00"}})
    assert d.visible("#snooze") and d.text("#ringing-time") == "7:00am"
    d.close()


def event(title, calendar, start, end, all_day=False):
    return {"title": title, "calendar": calendar, "start": start.timestamp(), "end": end.timestamp(), "all_day": all_day}


def test_agenda(b):
    d = Display(b)
    at = lambda day, h=0, m=0: dt.datetime(2026, 10, day, h, m)  # noqa: E731
    recycling = event("Recycling day", "Personal", at(7), at(8), all_day=True)
    standup = event("Standup", "Work", at(7, 9, 30), at(7, 9, 50))  # under way at 9:41
    dentist = event("Dentist", "Personal", at(7, 15), at(7, 16))
    swim = event("Swim lessons", "Kids Sports", at(7, 17, 30), at(7, 18, 15))
    call = event("Call with Sam", "Work", at(8, 10), at(8, 11))
    bday = event("Grandma's birthday", "Family", at(9), at(10), all_day=True)

    # Home strip: the event under way says Now; when it ends, the next one takes its place with its time
    timers = [{"id": 1, "label": "pasta", "ends_at": START.timestamp() + 252}]
    alarm_next = alarm(1, "06:30", next_ring=dt.datetime(2026, 10, 8, 6, 30).timestamp())
    d.push(dict(ambient(timers), next_alarm=alarm_next, agenda=[recycling, standup, dentist]))
    assert d.text(".strip .event .num") == "Now" and d.text(".strip .event .label") == "Standup"
    assert big(d.page.locator(".strip .event").bounding_box(), w=0)
    d.shot("agenda-strip")
    d.page.clock.run_for(10 * 60_000)  # 9:51: standup is over
    assert d.text(".strip .event .num") == "3:00pm" and d.text(".strip .event .label") == "Dentist"
    d.tap(".strip .event")
    assert {"type": "open", "sheet": "agenda"} in d.sent, d.sent

    # Only an all-day event left today: it shows by name alone
    d.push(dict(ambient([], weather=None), agenda=[recycling]))
    assert d.page.locator(".strip .event .num").count() == 0 and d.text(".strip .event .label") == "Recycling day"

    # The sheet: a heading per day, times in one column, the calendar under each title
    d.push({"type": "card", "card": {"kind": "agenda", "events": [recycling, standup, dentist, swim, call, bday]}})
    assert d.text("#sheet-title") == "Calendar"
    assert d.page.locator(".day-title").all_inner_texts() == ["Today", "Tomorrow", "Friday, October 9"]
    assert d.page.locator(".event-title").all_inner_texts() == ["Recycling day", "Dentist", "Swim lessons",
                                                                 "Call with Sam", "Grandma's birthday"], "ended events drop"
    assert d.page.locator(".event-when").all_inner_texts() == ["All day", "3:00pm", "5:30pm", "10:00am", "All day"]
    assert d.page.locator(".event-cal").all_inner_texts() == ["Personal", "Personal", "Kids Sports", "Work", "Family"]
    assert d.page.locator(".event-when.next").all_inner_texts() == ["3:00pm"], "the next thing today stands out"
    d.shot("agenda-sheet", settle=400)
    d.page.locator("#sheet-body").evaluate("e => e.scrollTop = e.scrollHeight")
    d.shot("agenda-sheet-scrolled", settle=100)

    # Under way: Now, in the accent
    d.page.clock.run_for(int((at(7, 15, 5) - at(7, 9, 51)).total_seconds() * 1000))
    d.push({"type": "card", "card": {"kind": "agenda", "events": [dentist, swim]}})
    assert d.page.locator(".event-when").all_inner_texts() == ["Now", "5:30pm"]
    assert d.page.locator(".event-when.next").all_inner_texts() == ["5:30pm"]

    # Nothing coming up
    d.push({"type": "card", "card": {"kind": "agenda", "events": []}})
    assert d.text("#sheet-body .quiet") == "Nothing on the calendar this week."
    d.shot("agenda-empty", settle=200)
    d.close()


def briefing(**over):
    at = lambda day, h=0, m=0: dt.datetime(2026, 10, day, h, m)  # noqa: E731
    card = {"kind": "briefing", "greeting": "Good morning",
            "weather": dict(WEATHER, days=[dict(WEATHER["days"][0], rain=40), *WEATHER["days"][1:]]),
            "events": [event("Recycling day", "Personal", at(7), at(8), all_day=True),
                       event("Standup", "Work", at(7, 9, 30), at(7, 9, 50)),
                       event("Dentist", "Personal", at(7, 15), at(7, 16)),
                       event("Swim lessons", "Kids Sports", at(7, 17, 30), at(7, 18, 15))],
            "next_alarm": alarm(1, "06:30", "01234", "gym", next_ring=at(8, 6, 30).timestamp()),
            "timers": [{"id": 1, "label": "pasta", "ends_at": START.timestamp() + 252}], "shopping": 3}
    return {"type": "card", "card": {**card, **over}}


def test_briefing(b):
    d = Display(b)
    d.push(briefing())
    assert d.text("#sheet-title") == "Good morning" and d.text(".brief-date") == "Wednesday, October 7"
    assert d.text(".brief-temp") == "47°" and d.text(".brief-cond") == "Clear, high 65, low 44"
    assert d.text(".brief-rain") == "40% chance of rain"
    assert d.page.locator(".brief-title").all_inner_texts() == ["Recycling day", "Standup", "Dentist", "Swim lessons"]
    assert d.page.locator(".brief-when").all_inner_texts() == ["All day", "Now", "3:00pm", "5:30pm"]
    assert d.page.locator(".brief-facts .label").all_inner_texts() == ["Gym tomorrow", "Pasta",
                                                                       "things on the shopping list"]
    assert d.page.locator(".brief-facts .num").all_inner_texts() == ["6:30am", "4:12", "3"]
    d.shot("briefing", settle=400)

    # A busy day: four events, then how many more; two timers are counted, one thing is singular
    at = lambda h: dt.datetime(2026, 10, 7, h)  # noqa: E731
    busy = [event(f"Meeting {h}", "Work", at(h), at(h + 1)) for h in range(10, 17)]
    two = [{"id": 1, "label": "pasta", "ends_at": START.timestamp() + 252},
           {"id": 2, "label": "", "ends_at": START.timestamp() + 600}]
    d.push(briefing(events=busy, timers=two, shopping=1, next_alarm=None, greeting="Good afternoon"))
    assert d.text("#sheet-title") == "Good afternoon" and d.page.locator(".brief-event").count() == 4
    assert d.text(".brief-more") == "and 3 more"
    assert d.page.locator(".brief-facts .num").all_inner_texts() == ["2", "1"]
    assert d.page.locator(".brief-facts .label").all_inner_texts() == ["timers running", "thing on the shopping list"]
    d.shot("briefing-busy", settle=200)

    # Nothing at all: says so plainly, and no empty row of facts
    d.push(briefing(weather=None, events=[], next_alarm=None, timers=[], shopping=0))
    assert d.text(".brief-cond") == "No forecast right now." and d.text(".brief-free") == "Nothing on the calendar."
    assert d.page.locator(".brief-facts").count() == 0
    d.shot("briefing-empty", settle=200)
    d.close()


PANCAKES = {"kind": "recipe", "title": "Pancakes", "step": -1,
            "ingredients": ["1 cup flour", "2 tbsp sugar", "2 tsp baking powder", "Pinch of salt", "1 cup milk",
                            "1 egg", "2 tbsp melted butter", "Butter for the pan"],
            "steps": ["Whisk the flour, sugar, baking powder and salt in a large bowl.",
                      "Whisk the milk, egg and melted butter, then stir into the dry ingredients until just combined; "
                      "a few lumps are fine, and overmixing makes the pancakes tough, so stop as soon as no dry flour "
                      "remains and let the batter rest for five minutes while the pan heats over a medium flame.",
                      "Pour a quarter cup of batter per pancake onto the buttered pan.",
                      "Flip when bubbles form, and cook until golden."]}


def test_recipe(b):
    d = Display(b)
    recipe = lambda: [m for m in d.sent if m.get("type") == "recipe"]  # noqa: E731
    d.push({"type": "card", "card": PANCAKES})
    assert d.text("#sheet-title") == "Pancakes" and d.page.locator(".ingredients.two-col li").count() == 8
    assert not d.visible(".recipe-prev") and d.text(".recipe-next") == "Next"
    assert big(d.page.locator(".recipe-next").bounding_box(), w=140)
    d.shot("recipe-ingredients", settle=400)

    # Next shows step 1 at once and tells the brain; the brain's echo changes nothing
    d.tap(".recipe-next")
    assert recipe() == [{"type": "recipe", "action": "next"}]
    assert d.text(".step-n") == "1" and d.text(".step-of") == "of 4" and d.visible(".recipe-prev")
    d.push({"type": "card", "card": dict(PANCAKES, step=0)})
    assert d.text(".step-n") == "1" and "Whisk the flour" in d.text(".step-text")
    d.shot("recipe-step", settle=400)

    # A long step shrinks to fit rather than running off the screen
    d.tap(".recipe-next")
    fit = d.page.evaluate("""() => { const s = document.querySelector('.step-text');
        return { fits: s.scrollHeight <= s.clientHeight + 1, size: parseFloat(getComputedStyle(s).fontSize) }; }""")
    assert fit["fits"] and fit["size"] >= 24, fit
    d.shot("recipe-long-step", settle=200)

    # Previous goes back; Claude moving it by voice moves the screen too
    d.tap(".recipe-prev")
    assert d.text(".step-n") == "1" and recipe()[-1] == {"type": "recipe", "action": "previous"}
    d.push({"type": "card", "card": dict(PANCAKES, step=3)})
    assert d.text(".step-n") == "4" and d.text(".recipe-next") == "Done"
    d.shot("recipe-last-step", settle=200)

    # A recipe outlasts the usual 45 s: a step can simmer a long time untouched
    d.page.clock.run_for(60_000)
    assert d.visible("#sheet"), "a recipe shouldn't leave after 45 s"
    d.page.clock.run_for(30 * 60_000)
    assert not d.visible("#sheet") and recipe()[-1] == {"type": "recipe", "action": "close"}

    # Done on the last step closes it and ends recipe mode
    d.push({"type": "card", "card": dict(PANCAKES, step=3)})
    before = len(recipe())
    d.tap(".recipe-next")
    assert not d.visible("#sheet") and recipe()[before:] == [{"type": "recipe", "action": "close"}]

    # Previous from step 1 returns to the ingredients
    d.push({"type": "card", "card": dict(PANCAKES, step=0)})
    d.tap(".recipe-prev")
    assert d.page.locator(".ingredients li").count() == 8 and not d.visible(".recipe-prev")
    swipe(d, 500)  # back from the edge, to the dashboard: that ends recipe mode
    assert recipe()[-1] == {"type": "recipe", "action": "close"}
    d.close()


ALBUM_ART = photo((40, 90, 160), (230, 200, 120))
BLUE = {"title": "A Case of You", "artist": "Joni Mitchell", "album": "Blue", "art": f"{ORIGIN}/art.jpg",
        "uri": "spotify:track:1", "is_playing": True, "progress_ms": 61000, "duration_ms": 263000}


def test_music(b):
    d = Display(b)
    media = lambda: [m["action"] for m in d.sent if m.get("type") == "media"]  # noqa: E731

    # Playing: art and title join the home strip
    d.push(dict(ambient([], weather=WEATHER)), {"type": "now_playing", "track": BLUE})
    assert d.text(".strip .music .label") == "A Case of You" and d.visible(".strip .music .np-thumb")
    d.shot("music-strip", settle=300)

    # Its sheet: art, title, artist, album, where it is, and the controls
    d.tap(".strip .music")
    assert d.text("#sheet-title") == "Music" and d.text(".np-title") == "A Case of You"
    assert d.text(".np-artist") == "Joni Mitchell" and d.text(".np-album") == "Blue"
    assert d.text(".np-at") == "1:01" and d.page.locator(".np-times span").nth(1).inner_text() == "4:23"
    assert d.page.get_attribute(".np-btn.main", "aria-label") == "Pause"
    for sel in (".np-btn >> nth=0", ".np-btn.main", ".np-btn >> nth=2"):
        assert big(d.page.locator(sel).bounding_box()), sel
    d.page.wait_for_function("document.querySelector('.np-art img').complete")
    d.shot("music-playing", settle=400)
    d.page.clock.run_for(10_000)
    assert d.text(".np-at") == "1:11", "the position should advance while playing"

    # Pause shows at once and leaves the strip; the position holds
    d.tap(".np-btn.main")
    assert media() == ["pause"] and d.page.get_attribute(".np-btn.main", "aria-label") == "Play"
    assert d.page.locator(".strip .music").count() == 0
    d.page.clock.run_for(10_000)
    assert d.text(".np-at") == "1:11"
    d.shot("music-paused", settle=200)
    d.tap(".np-btn >> nth=2")
    d.tap(".np-btn >> nth=0")
    d.tap(".np-btn.main")
    assert media() == ["pause", "next", "previous", "play"], media()

    # The brain reports a new track (a skip): the sheet follows
    d.push({"type": "now_playing", "track": dict(BLUE, title="River", progress_ms=0, duration_ms=240000)})
    assert d.text(".np-title") == "River" and d.text(".np-at") == "0:00"

    # Nothing playing: says so, and says how to start something
    d.push({"type": "now_playing", "track": None})
    assert d.text("#sheet-body .quiet") == "Nothing is playing." and d.page.locator(".np-btn").count() == 0
    d.shot("music-nothing", settle=200)
    d.close()


def test_touch_controls(b):
    d = Display(b)
    d.tap("#mic")
    assert {"type": "talk"} in d.sent, d.sent
    box = d.page.locator("#mic").bounding_box()
    assert big(box), box
    d.push({"type": "state", "state": "speaking"})
    d.tap("#close-overlay")
    assert not d.visible("#overlay"), "close didn't dismiss the overlay"
    d.close()


def test_camera(b):
    d = Display(b)
    cams = []  # the page's /camera sockets: what it sent, and whether it closed
    d.page.route_web_socket("**/camera?*", lambda ws: (cams.append({"url": ws.url, "sent": [], "closed": False}),
                                                        ws.on_message(lambda m: cams[-1]["sent"].append(json.loads(m))),
                                                        ws.on_close(lambda *_: cams[-1].update(closed=True)),
                                                        ws.send(json.dumps({"type": "config", "config": {}}))))
    # Playwright's mocked socket breaks when a page closes it without a code and reason; supply them
    d.page.evaluate("() => { const close = WebSocket.prototype.close;"
                    " WebSocket.prototype.close = function () { return close.call(this, 1000, 'closed'); }; }")
    d.push({"type": "camera", "entity": "camera.side_live_view", "name": "Side"})
    assert d.visible("#camera") and d.text("#camera-name") == "Side" and d.text("#camera-note") == "Connecting…"
    d.wait_until(lambda: cams and cams[0]["sent"], "no WebRTC offer sent")
    assert "entity=camera.side_live_view" in cams[0]["url"] and "offer" in cams[0]["sent"][0], cams
    assert big(d.page.locator("#camera-close").bounding_box())
    d.shot("camera")
    d.tap("#camera-close")
    assert not d.visible("#camera"), "X didn't close the camera"
    d.wait_until(lambda: cams[0]["closed"], "closing didn't end the stream")
    # A forgotten live view closes itself after two minutes
    d.push({"type": "camera", "entity": "camera.side_live_view", "name": "Side"})
    d.page.clock.run_for(119000)
    assert d.visible("#camera"), "closed early"
    d.page.clock.run_for(2000)
    assert not d.visible("#camera"), "still open after two minutes"
    # Closed by voice: the brain sends a camera message with no entity
    d.push({"type": "camera", "entity": "camera.side_live_view", "name": "Side"})
    d.push({"type": "camera", "entity": None})
    assert not d.visible("#camera"), "a camera message with no entity didn't close it"
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
