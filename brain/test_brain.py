"""Run: uv run python test_brain.py"""
import asyncio
import datetime
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

# Alarms: when they ring next (2026-10-07 is a Wednesday; Monday = 0)
D = datetime.datetime
wed_8am, fri_8pm = D(2026, 10, 7, 8, 0), D(2026, 10, 9, 20, 0)
assert tools.next_ring("09:30", "", wed_8am) == D(2026, 10, 7, 9, 30), "one-time, later today"
assert tools.next_ring("07:00", "", wed_8am) == D(2026, 10, 8, 7, 0), "one-time, already passed: tomorrow"
assert tools.next_ring("08:00", "", wed_8am) == D(2026, 10, 8, 8, 0), "exactly now doesn't count"
assert tools.next_ring("07:00", "01234", fri_8pm) == D(2026, 10, 12, 7, 0), "weekdays from Friday night: Monday"
assert tools.next_ring("00:05", "0123456", D(2026, 10, 7, 23, 59)) == D(2026, 10, 8, 0, 5), "daily across midnight"
assert tools.next_ring("07:00", "2", wed_8am) == D(2026, 10, 14, 7, 0), "Wednesdays, today's passed: a week away"
assert tools.next_ring("09:00", "56", wed_8am) == D(2026, 10, 10, 9, 0), "weekends: Saturday"
assert tools.day_digits(["Weekdays"]) == "01234" and tools.day_digits(["saturday", "Sun"]) == "56"
assert tools.day_digits([]) == "" and tools.day_digits(["daily"]) == "0123456" and tools.day_digits(["tue", "Thursday"]) == "13"
assert tools.spoken_time("07:30") == "7:30 AM" and tools.spoken_time("12:00") == "12 PM" and tools.spoken_time("00:15") == "12:15 AM"
assert tools.spoken_days("01234") == "on weekdays" and tools.spoken_days("") == "once" and tools.spoken_days("13") == "on Tuesday, Thursday"

# Alarm storage: due, done (one-time removed, recurring moved on), snooze, disabled never due
gym = tools.alarm_add("hall", "06:00", "01234", "gym", now=wed_8am)
once = tools.alarm_add("hall", "09:30", "", "", now=wed_8am)
off = tools.alarm_add("hall", "09:00", "", "off one", now=wed_8am)
assert tools.alarm_toggle("hall", off, False, now=wed_8am)
assert tools.next_alarm("hall")["id"] == once
at_930 = D(2026, 10, 7, 9, 30).timestamp()
assert [a["id"] for a in tools.alarms_due(at_930)] == [once], "the disabled 9:00 alarm must not be due"
tools.alarm_done(once, now=D(2026, 10, 7, 9, 30))
assert [a["id"] for a in tools.alarm_list("hall")] == [gym, off], "one-time alarm removed after ringing"
tools.q("UPDATE alarms SET next_ring = ? WHERE id = ?", D(2026, 10, 8, 6, 0).timestamp(), gym)
tools.alarm_done(gym, now=D(2026, 10, 8, 6, 0))
assert tools.alarm_list("hall")[0]["next_ring"] == D(2026, 10, 9, 6, 0).timestamp(), "recurring moves to the next weekday"
tools.alarm_snooze(gym, 9, now=1000.0)
assert [a["next_ring"] for a in tools.alarm_list("hall") if a["id"] == gym] == [1540.0]
assert tools.alarm_cancel("hall", "GYM") == 1 and tools.alarm_cancel("hall", "09:00") == 1 and tools.alarm_list("hall") == []

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

# Calendar: feeds from CALENDAR_* variables, recurrence, cancellations, all-day, another time zone
import agenda  # noqa: E402
import httpx  # noqa: E402

assert agenda.feeds({"CALENDAR_WORK": "https://a", "CALENDAR_KIDS_SPORTS": "https://b", "CALENDAR_EMPTY": " ",
                     "HOME_LOCATION": "x"}) == {"Kids Sports": "https://b", "Work": "https://a"}
ICS = (pathlib.Path(__file__).parent / "test_data" / "calendar.ics").read_bytes()
WED = datetime.date(2026, 10, 7)
local = lambda *a: datetime.datetime(*a).timestamp()  # noqa: E731  (naive = this machine's local time)
week = agenda.expand("Family", ICS, WED)
got = sorted((e["title"], e["start"]) for e in week)
assert got == sorted([
    ("Swim lessons", local(2026, 10, 7, 17, 30)),  # weekly from September; Oct 12 is an EXDATE, Oct 14 is CANCELLED
    ("Grandma's birthday", local(2026, 10, 9)),  # all day: local midnight
    ("Call with Sam", datetime.datetime(2026, 10, 8, 14, 0, tzinfo=datetime.UTC).timestamp()),  # 15:00 London (BST)
    ("Dentist", local(2026, 10, 7, 15, 0)),  # floating time: local
]), got
[bday] = [e for e in week if e["all_day"]]
assert bday["title"] == "Grandma's birthday" and bday["end"] == local(2026, 10, 10) and bday["calendar"] == "Family"
two_weeks = [e["start"] for e in agenda.expand("Family", ICS, WED, 14) if e["title"] == "Swim lessons"]
assert two_weeks == [local(2026, 10, 7, 17, 30), local(2026, 10, 19, 17, 30), local(2026, 10, 21, 17, 30)], two_weeks


def feed_server(failing: set):
    def handle(request):
        if request.url.host in failing:
            return httpx.Response(500)
        return httpx.Response(200, content=ICS if request.url.host == "family" else
                              ICS.replace(b"Dentist", b"Standup").replace(b"Family", b"Work"))
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


env = {"CALENDAR_FAMILY": "https://family/basic.ics", "CALENDAR_WORK": "webcal://work/basic.ics"}
asyncio.run(agenda.refresh(feed_server(set()), env))
assert {e["calendar"] for e in agenda.events} == {"Family", "Work"} and len(agenda.events) == 8
asyncio.run(agenda.refresh(feed_server({"work"}), env))  # Work fails: its last good copy stays
assert sum(e["calendar"] == "Work" for e in agenda.events) == 4, agenda.events
asyncio.run(agenda.refresh(feed_server(set()), {"CALENDAR_FAMILY": "https://family/basic.ics"}))  # Work removed
assert {e["calendar"] for e in agenda.events} == {"Family"}

# Today, upcoming, and how they're read out. At 3:30 pm: the dentist is under way, swim is next.
agenda.events[:] = sorted(agenda.expand("Family", ICS, WED), key=lambda e: e["start"])
at_330 = local(2026, 10, 7, 15, 30)
assert [e["title"] for e in agenda.today(at_330)] == ["Dentist", "Swim lessons"]
assert [e["title"] for e in agenda.today(local(2026, 10, 9, 9, 0))] == ["Grandma's birthday"]
assert [e["title"] for e in agenda.upcoming(at_330, 2)] == ["Dentist", "Swim lessons", "Call with Sam"]
assert tools.agenda_card(2, at_330)["events"] == agenda.upcoming(at_330, 2)
assert tools.agenda_text(agenda.upcoming(at_330, 3), at_330) == (
    "Today: 3 PM: Dentist (Family); 5:30 PM: Swim lessons (Family). Tomorrow: 10 AM: Call with Sam (Family). "
    "Friday, October 9: all day: Grandma's birthday (Family).")
assert tools.agenda_text([], at_330) == "Nothing on the calendar."
print("ok")
