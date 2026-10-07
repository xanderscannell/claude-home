"""Run: uv run python test_brain.py"""
import asyncio
import io
import json
import os
import pathlib
import tempfile

os.environ["CLAUDE_HOME_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")  # before importing tools

from PIL import Image

import photos
import tools
from brain import for_speech, pop_sentences

assert pop_sentences("Hi there. How are") == (["Hi there."], "How are")
assert pop_sentences("It costs 3.50 dollars! Okay") == (["It costs 3.50 dollars!"], "Okay")
assert pop_sentences("Really?! Yes. ") == (["Really?!", "Yes."], "")
assert pop_sentences('She said "stop." Then') == (['She said "stop."'], "Then")
assert pop_sentences("Line one\nLine two") == (["Line one"], "Line two")
assert pop_sentences("no end yet") == ([], "no end yet")
assert pop_sentences("Then Dr. Martin Luther King Jr. spoke. After") == (["Then Dr. Martin Luther King Jr. spoke."], "After")
assert pop_sentences("Bring snacks, e.g. chips. Ok") == (["Bring snacks, e.g. chips."], "Ok")
assert pop_sentences("Washington D.C. is the capital. It") == (["Washington D.C. is the capital."], "It")
assert pop_sentences("So did I. Then") == (["So did I."], "Then")  # "I." is not an initial
assert pop_sentences("Turn left on Main St. ") == ([], "Turn left on Main St. ")  # held until more text arrives
assert for_speech("Dr. Martin Luther King Jr. spoke.") == "Doctor Martin Luther King Junior spoke."
assert for_speech("Snacks, e.g. chips, etc.") == "Snacks, for example chips, et cetera"
assert for_speech("Drive to Mt. Hood vs. Mt. Rainier") == "Drive to Mount Hood versus Mount Rainier"
assert for_speech("Drs. and Mrs.Smith") == "Drs. and Mrs.Smith"  # only whole tokens

# Shopping list: no duplicates, case-insensitive removal, reports what wasn't there
tools.shopping_add(["Milk", "eggs", "milk"])
assert tools.shopping_items() == ["Milk", "eggs"]
assert tools.shopping_remove(["MILK", "bread"]) == ["bread"]
assert tools.shopping_items() == ["eggs"]

# Memory: forget matches any fact containing the text, case-insensitive
tools.remember("Sam's birthday is May 3")
tools.remember("The wifi password is on the fridge")
assert tools.forget("birthday") == ["Sam's birthday is May 3"]
assert tools.facts() == ["The wifi password is on the fridge"]

# Timers: per device, cancel by label or all, due ones are popped once
tools.timer_add("kitchen", "pasta", 600)
tools.timer_add("kitchen", "eggs", 0)
tools.timer_add("office", "", 300)
assert [label for label, _ in tools.timers("kitchen")] == ["eggs", "pasta"]
assert tools.timers_due() == [("kitchen", "eggs")] and tools.timers_due() == []
assert tools.timer_cancel("kitchen", "PAS") == 1 and tools.timers("kitchen") == []
assert tools.timer_cancel("office", "") == 1
assert tools.spoken_duration(3725) == "1 hour 2 minutes 5 seconds"
assert tools.spoken_duration(60) == "1 minute"

# Timers for the screen: ids, labels, end times, soonest first, per device
tools.timer_add("hall", "tea", 300)
tools.timer_add("hall", "", 60)
listed = tools.timer_list("hall")
assert [t["label"] for t in listed] == ["", "tea"] and all({"id", "label", "ends_at"} == t.keys() for t in listed)
assert listed[0]["ends_at"] < listed[1]["ends_at"] and tools.timer_list("nowhere") == []
tools.timer_cancel("hall", "")

# Weather: a real Open-Meteo response (New York, captured 2026-10-07) parsed for the screen and for Claude
raw = json.load(open(pathlib.Path(__file__).parent / "test_data" / "openmeteo.json"))
w = tools.parse_forecast("New York", raw)
assert w["now"] == {"temp": 47, "feels": 43, "condition": "clear", "code": 0, "wind": 3}, w["now"]
assert [d["label"] for d in w["days"]] == ["Today", "Tomorrow", "Friday"], w["days"]
assert w["days"][1] == {"label": "Tomorrow", "condition": "overcast", "code": 3, "high": 76, "low": 56, "rain": 2}
assert tools.weather_text(w).splitlines() == [
    "New York, now: 47F (feels 43F), clear, wind 3 mph",
    "today: clear, high 65F, low 44F, 1% chance of rain",
    "tomorrow: overcast, high 76F, low 56F, 2% chance of rain",
    "friday: overcast, high 74F, low 61F, 0% chance of rain"]
raw["daily"]["precipitation_probability_max"][0] = None  # Open-Meteo sends null when it has no estimate
assert tools.parse_forecast("X", raw)["days"][0]["rain"] == 0
assert asyncio.run(tools.locate(None, "40.71, -74.01")) == ("Home", 40.71, -74.01)  # coordinates: no lookup

# Screen cards from tools, and cancelling one timer by id
tools.timer_add("den", "tea", 120)
tea = tools.timer_list("den")[0]
assert tools.timers_card("den") == {"kind": "timers", "timers": [tea], "alarms": []}
assert tools.timer_cancel_id("den", tea["id"]) and not tools.timer_cancel_id("den", tea["id"])
shown_cards = []


async def capture(card):
    shown_cards.append(card)

by_name = {t.name: t for t in tools.tool_list("den", capture)}
asyncio.run(by_name["set_timer"].handler({"seconds": 90, "label": "toast"}))
asyncio.run(by_name["show_on_screen"].handler({"title": "Ingredients", "lines": ["eggs", "flour"]}))
assert [c["kind"] for c in shown_cards] == ["timers", "text"] and shown_cards[0]["timers"][0]["label"] == "toast"
assert shown_cards[1] == {"kind": "text", "title": "Ingredients", "lines": ["eggs", "flour"]}
tools.timer_cancel("den", "")

# Photos: listing, safe lookup, cover-resize with EXIF rotation, caching
pics = pathlib.Path(tempfile.mkdtemp())
Image.new("RGB", (1600, 900), "green").save(pics / "land.jpg")
Image.new("RGB", (900, 1600), "green").save(pics / "tall.png")
sideways = Image.new("RGB", (1600, 900), "red")
sideways.paste(Image.new("RGB", (800, 900), "blue"), (800, 0))  # stored: red left half, blue right half
exif = Image.Exif()
exif[0x0112] = 6  # orientation 6: rotate 90 degrees clockwise to view, so the stored left becomes the top
sideways.save(pics / "phone.jpg", exif=exif)
(pics / "notes.txt").write_text("not a photo")
(pics / "live.heic").write_bytes(b"\0")
assert sorted(photos.names(pics)) == ["land.jpg", "phone.jpg", "tall.png"]
assert photos.find("land.jpg", pics) and photos.find("../land.jpg", pics) is None
assert photos.find("notes.txt", pics) is None and photos.find("missing.jpg", pics) is None
for name in ("land.jpg", "tall.png", "phone.jpg"):
    assert Image.open(io.BytesIO(photos.render(pics / name))).size == (800, 480), name
upright = Image.open(io.BytesIO(photos.render(pics / "phone.jpg")))
top, bottom = upright.getpixel((400, 20)), upright.getpixel((400, 460))
assert top[0] > 200 and top[2] < 60 and bottom[2] > 200 and bottom[0] < 60, (top, bottom)  # red over blue
cache = pathlib.Path(tempfile.mkdtemp())
first = photos.cached(pics / "land.jpg", cache)
stamp = first.stat().st_mtime_ns
assert photos.cached(pics / "land.jpg", cache) == first and first.stat().st_mtime_ns == stamp  # not re-rendered
print("ok")
