"""Claude's tools: timers, alarms, memory, shopping list, weather, calendar, briefing, recipes, music, volume.

To add a tool: write an async function with @tool (name, description for Claude, JSON schema of its
arguments) inside make_tools(), and add it to the list at the bottom. Storage helpers are plain functions
so they can be tested without Claude.
"""

import asyncio
import datetime
import os
import sqlite3
import time
from contextlib import closing
from itertools import groupby
from pathlib import Path

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

import agenda
import spotify
import todoist

DB_PATH = Path(os.environ.get("CLAUDE_HOME_DB", Path(__file__).parent / "home.db"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS timers (id INTEGER PRIMARY KEY, device TEXT, label TEXT, ends_at REAL, duration REAL,
                                   paused_left REAL);
CREATE TABLE IF NOT EXISTS memory (fact TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS alarms (id INTEGER PRIMARY KEY, device TEXT, time TEXT, days TEXT, label TEXT,
                                   enabled INTEGER DEFAULT 1, next_ring REAL);
"""


def q(sql: str, *args) -> list[tuple]:
    with closing(sqlite3.connect(DB_PATH)) as con, con:  # inner `with con` commits
        con.executescript(SCHEMA)
        return con.execute(sql, args).fetchall()


def upgrade() -> None:
    """Add columns that came after a database was made (CREATE TABLE IF NOT EXISTS leaves an old table alone)."""
    have = {row[1] for row in q("PRAGMA table_info(timers)")}
    for column in ("duration REAL", "paused_left REAL"):
        if column.split()[0] not in have:
            q(f"ALTER TABLE timers ADD COLUMN {column}")


upgrade()


# --- Storage -----------------------------------------------------------------------------------

# The shopping list is a Todoist project (see todoist.py). These raise todoist.TodoistError when it can't be reached.

async def shopping_add(items: list[tuple[str, str]]) -> list[str]:
    """Add (item, category) pairs. Returns the items whose category isn't one of the list's, added with none."""
    return await todoist.add(todoist.shopping(), items)


async def shopping_remove(items: list[str]) -> list[str]:
    """Check items off (case-insensitive). Returns the ones that weren't on the list."""
    return await todoist.check_off(todoist.shopping(), items)


async def shopping_items() -> list[str]:
    return [t["content"] for t in await todoist.tasks(todoist.shopping())]


async def shopping_categories() -> list[str]:
    """The list's categories (its Todoist sections), or none when Todoist can't be reached."""
    try:
        return [x["name"] for x in await todoist.sections(todoist.shopping())]
    except todoist.TodoistError:
        return []


async def shopping_groups() -> list[dict]:
    """The list by category, in Todoist's order, uncategorized first: [{"name" ("" for none), "items"}]."""
    return [{"name": name, "items": [t["content"] for t in group]}
            for name, group in groupby(await todoist.tasks(todoist.shopping()), key=lambda t: t["section"])]


def remember(fact: str) -> None:
    q("INSERT OR IGNORE INTO memory VALUES (?)", fact.strip())


def forget(text: str) -> list[str]:
    """Forget every fact containing text (case-insensitive). Returns what was forgotten."""
    return [r[0] for r in q("DELETE FROM memory WHERE instr(lower(fact), lower(?)) RETURNING fact", text)]


def facts() -> list[str]:
    return [r[0] for r in q("SELECT fact FROM memory ORDER BY rowid")]


# Words that only say how long something is ("five minute", "an hour and a half"), never what it's for
LENGTH_WORDS = {"a", "an", "and", "half", "quarter", "one", "two", "three", "four", "five", "six", "seven", "eight",
                "nine", "ten", "eleven", "twelve", "fifteen", "twenty", "thirty", "forty", "fifty",
                "sixty", "ninety", "hour", "hours", "hr", "hrs", "minute", "minutes", "min", "mins", "second",
                "seconds", "sec", "secs"}


def timer_label(label: str) -> str:
    """The name worth keeping: without a trailing "timer", and none at all when it only says how long the timer
    is ('5 minute timer'): the timer is then named by its length, which can't disagree with it."""
    label = label.strip()
    if label.casefold().endswith("timer"):
        label = label[:-5].strip()
    words = label.casefold().replace("-", " ").split()
    return "" if all(w.isdigit() or w in LENGTH_WORDS for w in words) else label


def timer_add(device: str, label: str, seconds: float) -> None:
    q("INSERT INTO timers (device, label, ends_at, duration) VALUES (?, ?, ?, ?)",
      device, timer_label(label), time.time() + seconds, seconds)


def timer_list(device: str) -> list[dict]:
    """A device's timers for the screen, oldest first (so they keep their places). A running one counts down to
    ends_at; a paused one has paused_left seconds left. duration is how long it was set for (None: set before
    that was kept)."""
    return [{"id": i, "label": label, "ends_at": ends, "duration": duration, "paused_left": paused}
            for i, label, ends, duration, paused in
            q("SELECT id, label, ends_at, duration, paused_left FROM timers WHERE device = ? ORDER BY id", device)]


def timer_left(t: dict, now: float) -> float:
    return t["ends_at"] - now if t["paused_left"] is None else t["paused_left"]


def timer_pause(device: str, timer_id: int, now: float | None = None) -> bool:
    """Pause a running timer (a Pause button). Returns whether one was paused."""
    now = time.time() if now is None else now
    return bool(q("UPDATE timers SET paused_left = max(0, ends_at - ?) WHERE device = ? AND id = ? "
                  "AND paused_left IS NULL RETURNING id", now, device, timer_id))


def timer_resume(device: str, timer_id: int, now: float | None = None) -> bool:
    """Start a paused timer again from where it stopped. Returns whether one was resumed."""
    now = time.time() if now is None else now
    return bool(q("UPDATE timers SET ends_at = ? + paused_left, paused_left = NULL WHERE device = ? AND id = ? "
                  "AND paused_left IS NOT NULL RETURNING id", now, device, timer_id))


def timer_name(t: dict) -> str:
    """What a timer is called: its label, else its length ('5 minute timer'), else just 'timer'."""
    if t["label"]:
        return t["label"]
    return f"{spoken_duration(t['duration'], singular=True)} timer" if t.get("duration") else "timer"


def timer_matches(t: dict, text: str) -> bool:
    """Whether someone means this timer: by its label ('pasta', 'pasta timer') or its length ('5 minute timer').
    Case-insensitive; empty text means every timer."""
    text = text.strip().casefold()
    names = [t["label"], f"{t['label']} timer"] if t["label"] else []
    if t.get("duration"):
        names.append(f"{spoken_duration(t['duration'], singular=True)} timer")
    return not text or any(text in name.casefold() for name in names) or (not t["label"] and text == "timer")


def timer_cancel(device: str, text: str) -> int:
    """Cancel timers called text (see timer_matches; all of the device's timers if empty). Returns how many."""
    return sum(timer_cancel_id(device, t["id"]) for t in timer_list(device) if timer_matches(t, text))


def timer_cancel_id(device: str, timer_id: int) -> bool:
    """Cancel one of a device's timers by id (a Cancel button). Returns whether it existed."""
    return bool(q("DELETE FROM timers WHERE device = ? AND id = ? RETURNING id", device, timer_id))


def timers_due() -> list[tuple[str, str, float | None]]:
    """Pop finished timers: [(device, label, duration)]."""
    return q("DELETE FROM timers WHERE ends_at <= ? AND paused_left IS NULL RETURNING device, label, duration",
             time.time())


def timer_text(t: dict, now: float) -> str:
    """A timer as Claude says it: 'pasta, 4 minutes left', with ', paused' when it is."""
    return (f"{timer_name(t)}, {spoken_duration(timer_left(t, now))} left"
            + (", paused" if t["paused_left"] is not None else ""))


def spoken_duration(seconds: float, singular: bool = False) -> str:
    """'1 hour 2 minutes 5 seconds'; singular for before a noun: '5 minute timer'."""
    s = max(0, round(seconds))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    parts = [f"{n} {unit}{'s' if n != 1 and not singular else ''}"
             for n, unit in ((h, "hour"), (m, "minute"), (s, "second")) if n]
    return " ".join(parts) or "0 seconds"


# --- Alarms ------------------------------------------------------------------------------------
# time is "HH:MM" (24-hour, local); days is a string of weekday digits, Monday = 0 ("01234" weekdays,
# "0123456" daily), or "" for a one-time alarm.

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def next_ring(hhmm: str, days: str, now: datetime.datetime) -> datetime.datetime:
    """The first moment strictly after now when this alarm should ring."""
    h, m = (int(x) for x in hhmm.split(":"))
    for offset in range(8):
        at = (now + datetime.timedelta(days=offset)).replace(hour=h, minute=m, second=0, microsecond=0)
        if at > now and (not days or str(at.weekday()) in days):
            return at
    raise ValueError(f"no ring time for {hhmm!r} on days {days!r}")


def day_digits(names: list[str]) -> str:
    """["monday", "Friday"] -> "04"; understands "weekdays", "weekends", "daily"/"every day"."""
    out = set()
    for name in (n.strip().lower() for n in names):
        if name in ("weekdays", "weekday"):
            out |= set("01234")
        elif name in ("weekends", "weekend"):
            out |= set("56")
        elif name in ("daily", "every day", "everyday"):
            out |= set("0123456")
        elif name[:3] in (w[:3] for w in WEEKDAYS):
            out.add(str([w[:3] for w in WEEKDAYS].index(name[:3])))
    return "".join(sorted(out))


def spoken_time(hhmm: str) -> str:
    """"07:30" -> "7:30 AM", "12:00" -> "12 PM"."""
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}{f':{m:02d}' if m else ''} {'AM' if h < 12 else 'PM'}"


def spoken_days(days: str) -> str:
    return {"": "once", "01234": "on weekdays", "56": "on weekends", "0123456": "every day"}.get(
        days, "on " + ", ".join(WEEKDAYS[int(d)].title() for d in days))


def alarm_add(device: str, hhmm: str, days: str, label: str, now: datetime.datetime | None = None) -> int:
    at = next_ring(hhmm, days, now or datetime.datetime.now())
    return q("INSERT INTO alarms (device, time, days, label, enabled, next_ring) VALUES (?, ?, ?, ?, 1, ?) RETURNING id",
             device, hhmm, days, label, at.timestamp())[0][0]


def alarm_list(device: str) -> list[dict]:
    return [{"id": i, "time": t, "days": d, "label": label, "enabled": bool(e), "next_ring": n}
            for i, t, d, label, e, n in q("SELECT id, time, days, label, enabled, next_ring FROM alarms "
                                          "WHERE device = ? ORDER BY time", device)]


def next_alarm(device: str) -> dict | None:
    """The device's soonest enabled alarm, for the home screen."""
    upcoming = [a for a in alarm_list(device) if a["enabled"]]
    return min(upcoming, key=lambda a: a["next_ring"]) if upcoming else None


def alarm_delete(device: str, alarm_id: int) -> bool:
    return bool(q("DELETE FROM alarms WHERE device = ? AND id = ? RETURNING id", device, alarm_id))


def alarm_cancel(device: str, match: str) -> int:
    """Delete alarms whose label contains match or whose time is match ("07:30"); all of them if empty."""
    return len(q("DELETE FROM alarms WHERE device = ? AND (instr(lower(label), lower(?)) OR time = ?) RETURNING id",
                 device, match, match))


def alarm_toggle(device: str, alarm_id: int, enabled: bool, now: datetime.datetime | None = None) -> bool:
    """Turn an alarm on or off; turning it on schedules it from now."""
    row = q("SELECT time, days FROM alarms WHERE device = ? AND id = ?", device, alarm_id)
    if not row:
        return False
    at = next_ring(row[0][0], row[0][1], now or datetime.datetime.now()).timestamp()
    q("UPDATE alarms SET enabled = ?, next_ring = ? WHERE id = ?", int(enabled), at, alarm_id)
    return True


def alarms_due(now: float) -> list[dict]:
    """Enabled alarms whose time has come: {id, device, time, days, label, next_ring}. They stay until alarm_done."""
    return [{"id": i, "device": dev, "time": t, "days": d, "label": label, "next_ring": n} for i, dev, t, d, label, n in
            q("SELECT id, device, time, days, label, next_ring FROM alarms WHERE enabled = 1 AND next_ring <= ?", now)]


def alarm_done(alarm_id: int, now: datetime.datetime | None = None) -> None:
    """After an alarm has rung: a one-time alarm is removed; a recurring one moves to its next day."""
    row = q("SELECT time, days FROM alarms WHERE id = ?", alarm_id)
    if not row:
        return
    hhmm, days = row[0]
    if not days:
        q("DELETE FROM alarms WHERE id = ?", alarm_id)
        return
    now = now or datetime.datetime.now()
    q("UPDATE alarms SET next_ring = ? WHERE id = ?", next_ring(hhmm, days, now + datetime.timedelta(minutes=1)).timestamp(),
      alarm_id)  # (+1 minute: never reschedule into the minute that just rang)


def alarm_snooze(alarm_id: int, minutes: int, now: float) -> None:
    q("UPDATE alarms SET next_ring = ? WHERE id = ?", now + minutes * 60, alarm_id)


# --- Weather (Open-Meteo: free, no key) ---------------------------------------------------------

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
       82: "heavy showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
       96: "thunderstorms with hail", 99: "thunderstorms with hail"}


async def locate(http: httpx.AsyncClient, place: str) -> tuple[str, float, float]:
    """Place name, or "lat,lon", to (name, lat, lon)."""
    try:
        lat, lon = (float(x) for x in place.split(","))
        return "Home", lat, lon  # shown as the weather sheet's title
    except ValueError:
        pass
    r = await http.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": place, "count": 1})
    hits = r.raise_for_status().json().get("results")
    if not hits:
        raise LookupError(f"couldn't find a place called {place!r}")
    h = hits[0]
    return ", ".join(x for x in (h["name"], h.get("admin1"), h.get("country")) if x), h["latitude"], h["longitude"]


def parse_forecast(name: str, d: dict) -> dict:
    """An Open-Meteo response as the weather the screen and Claude use (Fahrenheit, mph, rounded)."""
    c, day = d["current"], d["daily"]
    labels = ["Today", "Tomorrow", datetime.date.fromisoformat(day["time"][2]).strftime("%A")]
    return {
        "place": name,
        "now": {"temp": round(c["temperature_2m"]), "feels": round(c["apparent_temperature"]),
                "condition": WMO.get(c["weather_code"], "unknown"), "code": c["weather_code"],
                "wind": round(c["wind_speed_10m"])},
        "days": [{"label": label, "condition": WMO.get(day["weather_code"][i], "unknown"),
                  "code": day["weather_code"][i], "high": round(day["temperature_2m_max"][i]),
                  "low": round(day["temperature_2m_min"][i]), "rain": day["precipitation_probability_max"][i] or 0}
                 for i, label in enumerate(labels)],
    }


def weather_text(w: dict) -> str:
    """The weather as plain lines for Claude to speak from."""
    n = w["now"]
    lines = [f"{w['place']}, now: {n['temp']}F (feels {n['feels']}F), {n['condition']}, wind {n['wind']} mph"]
    lines += [f"{d['label'].lower()}: {d['condition']}, high {d['high']}F, low {d['low']}F, "
              f"{d['rain']}% chance of rain" for d in w["days"]]
    return "\n".join(lines)


async def weather_data(place: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as http:
        name, lat, lon = await locate(http, place)
        r = await http.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 3,
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
        return parse_forecast(name, r.raise_for_status().json())


# --- Tools Claude can call ----------------------------------------------------------------------

def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _schema(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props)}


STRINGS = {"type": "array", "items": {"type": "string"}}




# --- Cards: what a screen shows after a tool runs, or when someone opens a sheet ------------------

def timers_card(device: str) -> dict:
    return {"kind": "timers", "timers": timer_list(device)}


def alarms_card(device: str) -> dict:
    return {"kind": "alarms", "alarms": alarm_list(device)}


def weather_card(w: dict) -> dict:
    return {"kind": "weather", **w}


async def shopping_card() -> dict:
    try:
        return {"kind": "shopping", "groups": await shopping_groups()}
    except todoist.TodoistError as e:
        return text_card("Shopping list", [str(e)])


def text_card(title: str, lines: list[str]) -> dict:
    return {"kind": "text", "title": title, "lines": lines}


def agenda_card(days: int = agenda.DAYS, now: float | None = None) -> dict:
    return {"kind": "agenda", "events": agenda.upcoming(time.time() if now is None else now, days)}


def day_name(day: datetime.date, today: datetime.date) -> str:
    if day == today:
        return "Today"
    if day == today + datetime.timedelta(days=1):
        return "Tomorrow"
    return f"{day:%A}, {day:%B} {day.day}"


def agenda_text(events: list[dict], now: float) -> str:
    """Events grouped by day, for Claude to read out. An event already under way counts as today's."""
    if not events:
        return "Nothing on the calendar."
    today = datetime.date.fromtimestamp(now)
    parts = []
    for day, group in groupby(events, key=lambda e: datetime.date.fromtimestamp(max(e["start"], now))):
        items = [("all day" if e["all_day"] else spoken_time(f"{datetime.datetime.fromtimestamp(e['start']):%H:%M}"))
                 + f": {e['title']} ({e['calendar']})" for e in group]
        parts.append(f"{day_name(day, today)}: " + "; ".join(items))
    return ". ".join(parts) + "."


# --- Recipes: one open recipe per device, walked through a step at a time (in memory; a restart closes it) --

recipes: dict[str, dict] = {}  # device -> {title, ingredients, steps, step}; step -1 is the ingredients


def recipe_open(device: str, title: str, ingredients: list[str], steps: list[str]) -> None:
    recipes[device] = {"title": title, "ingredients": ingredients, "steps": steps, "step": -1}


def recipe_move(device: str, direction: str) -> bool:
    """next, previous, repeat, or first (step 1). Stays put at either end. Returns whether a recipe is open."""
    r = recipes.get(device)
    if r is None:
        return False
    last = len(r["steps"]) - 1
    r["step"] = {"next": min(r["step"] + 1, last), "previous": max(r["step"] - 1, -1),
                 "first": min(0, last), "repeat": r["step"]}[direction]
    return True


def recipe_card(device: str) -> dict:
    return {"kind": "recipe", **recipes[device]}


def recipe_text(r: dict) -> str:
    """Where the recipe is, for Claude to read out."""
    if r["step"] < 0:
        return "Ingredients: " + "; ".join(r["ingredients"]) + "."
    n, i = len(r["steps"]), r["step"]
    return f"Step {i + 1} of {n}: {r['steps'][i]}" + (" That's the last step." if i == n - 1 else "")


def greeting(now: float) -> str:
    h = datetime.datetime.fromtimestamp(now).hour
    return "Good morning" if h < 12 else "Good afternoon" if h < 17 else "Good evening"


def briefing_card(device: str, weather: dict | None, shopping: int | None, now: float | None = None) -> dict:
    """The day at a glance: the forecast, the rest of today's calendar, the next alarm, timers, the list.
    shopping is how many things are on the list (None: Todoist couldn't be reached)."""
    now = time.time() if now is None else now
    return {"kind": "briefing", "greeting": greeting(now), "weather": weather, "events": agenda.upcoming(now, 1),
            "next_alarm": next_alarm(device), "timers": [t for t in timer_list(device) if timer_left(t, now) > 0],
            "shopping": shopping}


def briefing_text(card: dict, now: float) -> str:
    """The briefing as plain sentences for Claude to speak from."""
    today = datetime.datetime.fromtimestamp(now)
    parts = [f"{card['greeting']}. It's {today:%A}, {today:%B} {today.day}."]
    if w := card["weather"]:
        n, d = w["now"], w["days"][0]
        rain = f", {d['rain']}% chance of rain" if d["rain"] >= 30 else ""
        parts.append(f"Now {n['temp']}F and {n['condition']}; today a high of {d['high']}F and a low of {d['low']}F{rain}.")
    else:
        parts.append("The forecast isn't available right now.")
    parts.append(agenda_text(card["events"], now) if card["events"] else "Nothing on the calendar today.")
    if a := card["next_alarm"]:
        when = day_name(datetime.date.fromtimestamp(a["next_ring"]), today.date()).lower()
        parts.append(f"Next alarm: {spoken_time(a['time'])} {when}" + (f" ({a['label']})." if a["label"] else "."))
    if card["timers"]:
        parts.append("Timers: " + "; ".join(timer_text(t, now) for t in card["timers"]) + ".")
    n = card["shopping"]
    parts.append("The shopping list isn't available right now." if n is None else
                 f"The shopping list has {n} item{'s' if n != 1 else ''}." if n else "The shopping list is empty.")
    return " ".join(parts)


async def _no_screen(card: dict):
    pass


async def _no_speaker(level: int, relative: bool) -> None:
    return None


async def _right_away(action):
    await action()


def tool_list(device: str, on_card=_no_screen, on_volume=_no_speaker, music: spotify.Spotify | None = None,
              categories: list[str] = (), after_reply=_right_away, cameras: list[dict] = (),
              on_camera=_no_screen) -> list:
    """Claude's tools for one satellite. Timers belong to the device they were set on.
    on_card(card) is awaited to put a result on that device's screens.
    on_volume(level, relative) sets the speaker's volume and returns the new level (None: no speaker).
    music is the Spotify client (None: Spotify isn't set up).
    categories are the shopping list's, as of the session's start (shopping results always list the current ones).
    after_reply(action) is awaited to run an async action once this device's reply has finished playing, so music
    doesn't start under Claude's voice (and the music ducking).
    cameras are Home Assistant's, as {entity, name, area} (none: no camera tools); on_camera(camera) is awaited
    to put one's live view on this device's screens, and on_camera(None) to close it."""

    async def via_spotify(action, say):
        if music is None:
            return _text("Spotify isn't set up.")
        try:
            result = await action()
        except spotify.SpotifyError as e:
            return _text(str(e))
        music.changed.set()  # the screens' now playing catches up within a second or so
        return _text(say(result))

    @tool("set_timer", "Start a countdown timer that rings on this speaker.",
          _schema(seconds={"type": "integer", "minimum": 1},
                  label={"type": "string", "description": "what it's for, e.g. 'pasta', only if the person said; "
                         "otherwise empty (the screen names it by its length)"}))
    async def set_timer(a):
        timer_add(device, a["label"], a["seconds"])
        await on_card(timers_card(device))
        return _text(f"Timer set for {spoken_duration(a['seconds'])}.")

    @tool("list_timers", "List this speaker's timers and the time left on each.", _schema())
    async def list_timers(a):
        now = time.time()
        await on_card(timers_card(device))
        return _text("; ".join(timer_text(t, now) for t in timer_list(device)) or "No timers running.")

    timer_called = {"type": "string", "description": "its name, as list_timers gives it (a timer with no label is "
                    "called by its length, e.g. '5 minute timer'); empty for all of them"}

    @tool("cancel_timer", "Cancel (stop) timers by name.", _schema(name=timer_called))
    async def cancel_timer(a):
        n = timer_cancel(device, a["name"])
        await on_card(timers_card(device))
        return _text(f"Cancelled {n} timer(s).")

    @tool("pause_timer", "Pause running timers by name; they keep their time left until resumed.",
          _schema(name=timer_called))
    async def pause_timer(a):
        n = sum(timer_pause(device, t["id"]) for t in timer_list(device) if timer_matches(t, a["name"]))
        await on_card(timers_card(device))
        return _text(f"Paused {n} timer(s).")

    @tool("resume_timer", "Start paused timers again by name, from where they stopped.", _schema(name=timer_called))
    async def resume_timer(a):
        n = sum(timer_resume(device, t["id"]) for t in timer_list(device) if timer_matches(t, a["name"]))
        await on_card(timers_card(device))
        return _text(f"Resumed {n} timer(s).")

    @tool("set_alarm", "Set an alarm that rings on this speaker at a time of day, once or repeating.",
          _schema(time={"type": "string", "pattern": "^([01]?[0-9]|2[0-3]):[0-5][0-9]$",
                        "description": "24-hour local time, e.g. 07:30 or 18:05"},
                  days={**STRINGS, "description": "repeat on these days (weekday names, or 'weekdays', 'weekends', "
                        "'daily'); empty for a one-time alarm"},
                  label={"type": "string", "description": "optional name, e.g. 'gym'; empty if none given"}))
    async def set_alarm(a):
        hhmm = "%02d:%02d" % tuple(int(x) for x in a["time"].split(":"))
        days = day_digits(a["days"])
        alarm_add(device, hhmm, days, a["label"])
        await on_card(alarms_card(device))
        return _text(f"Alarm set for {spoken_time(hhmm)} {spoken_days(days)}.")

    @tool("list_alarms", "List this speaker's alarms.", _schema())
    async def list_alarms(a):
        alarms = alarm_list(device)
        await on_card(alarms_card(device))
        return _text("; ".join(f"{spoken_time(x['time'])} {spoken_days(x['days'])}"
                               + (f" ({x['label']})" if x["label"] else "") + ("" if x["enabled"] else ", off")
                               for x in alarms) or "No alarms set.")

    @tool("cancel_alarm", "Delete alarms whose label contains the text or whose time matches (HH:MM, 24-hour); "
          "all alarms if empty.", _schema(match={"type": "string"}))
    async def cancel_alarm(a):
        n = alarm_cancel(device, a["match"])
        await on_card(alarms_card(device))
        return _text(f"Deleted {n} alarm(s).")

    @tool("remember", "Save a fact about the household to remember in future conversations.",
          _schema(fact={"type": "string"}))
    async def remember_tool(a):
        remember(a["fact"])
        return _text("Saved.")

    @tool("forget", "Forget every saved fact that contains the given text.", _schema(text={"type": "string"}))
    async def forget_tool(a):
        gone = forget(a["text"])
        return _text(f"Forgot: {'; '.join(gone)}" if gone else "No saved fact matched.")

    async def on_list(change=None):
        """Make a change to the shopping list (or none), show it, and say what's on it now and its categories."""
        try:
            said = await change() if change else ""
            groups, categories = await asyncio.gather(shopping_groups(), todoist.sections(todoist.shopping()))
        except todoist.TodoistError as e:
            return _text(f"Couldn't update the shopping list: {e}")
        await on_card({"kind": "shopping", "groups": groups})
        now = "; ".join((f"{g['name']}: " if g["name"] else "") + ", ".join(g["items"]) for g in groups)
        return _text(said + (f"Shopping list now: {now}." if now else "The shopping list is empty.")
                     + (f" Its categories: {', '.join(c['name'] for c in categories)}." if categories else ""))

    @tool("add_to_shopping_list", "Add items to the shopping list (the Shopping List project in Todoist), each in "
          "the list's category it belongs in. " + (f"Its categories: {', '.join(categories)}." if categories else
                                                   "Every shopping list result names its categories."),
          _schema(items={"type": "array", "items": _schema(
              item={"type": "string"},
              category={"type": "string", "description": "the category it belongs in; empty if none fits"})}))
    async def add_shopping(a):
        async def change():
            unplaced = await shopping_add([(x["item"], x["category"]) for x in a["items"]])
            return f"No matching category, so added with none: {', '.join(unplaced)}. " if unplaced else ""
        return await on_list(change)

    @tool("remove_from_shopping_list", "Check items off the shopping list.", _schema(items=STRINGS))
    async def remove_shopping(a):
        async def change():
            missing = await shopping_remove(a["items"])
            return f"Not on the list: {', '.join(missing)}. " if missing else ""
        return await on_list(change)

    @tool("get_shopping_list", "Read the shopping list.", _schema())
    async def get_shopping(a):
        return await on_list()

    @tool("clear_shopping_list", "Check off everything on the shopping list.", _schema())
    async def clear_shopping(a):
        async def change():
            await shopping_remove(await shopping_items())
            return ""
        return await on_list(change)

    @tool("get_weather", "Current conditions and a 3-day forecast. Faster and more accurate than web search for weather.",
          _schema(place={"type": "string", "description": "city name; empty for home"}))
    async def get_weather(a):
        place = a["place"] or os.environ.get("HOME_LOCATION", "")
        if not place:
            return _text("Home location isn't configured (HOME_LOCATION). Ask which city.")
        try:
            w = await weather_data(place)
        except (LookupError, httpx.HTTPError) as e:
            return _text(f"Weather lookup failed: {e}")
        await on_card(weather_card(w))
        return _text(weather_text(w))

    @tool("show_on_screen", "Show something on this speaker's screen while you talk about it: a list, steps, a "
          "definition, a comparison. Keep it short; the screen is small and read from across the room. Not for "
          "timers, alarms, weather, the shopping list, the calendar, the briefing, recipes, or music: their own "
          "tools already put them on the screen, in a better form.",
          _schema(title={"type": "string"},
                  lines={**STRINGS, "description": "a few short lines, shown in order"}))
    async def show_on_screen(a):
        await on_card(text_card(a["title"], a["lines"]))
        return _text("Shown on the screen.")

    @tool("get_agenda", "The household's calendar events (each with the calendar it's on), shown on the screen too.",
          _schema(days={"type": "integer", "minimum": 1, "maximum": agenda.DAYS + 1,
                        "description": "1 for the rest of today, 2 for today and tomorrow, and so on"}))
    async def get_agenda(a):
        if not agenda.feeds():
            return _text("No calendars are set up.")
        card = agenda_card(a["days"])
        await on_card(card)
        return _text(agenda_text(card["events"], time.time()))

    @tool("morning_briefing", "A rundown of the day: weather, today's calendar, the next alarm, timers, and the "
          "shopping list, shown on the screen too. Speak it as a short, friendly summary, not a list.", _schema())
    async def morning_briefing(a):
        place = os.environ.get("HOME_LOCATION", "")
        try:
            w = await weather_data(place) if place else None
        except (LookupError, httpx.HTTPError):
            w = None
        try:
            shopping = len(await shopping_items())
        except todoist.TodoistError:
            shopping = None
        card = briefing_card(device, w, shopping)
        await on_card(card)
        return _text(briefing_text(card, time.time()))

    @tool("show_recipe", "Put a recipe on the screen to cook along with: the ingredients first, then one step at a "
          "time. Keep each step short (one or two sentences). Afterwards, use recipe_step to move through it.",
          _schema(title={"type": "string"}, ingredients={**STRINGS, "description": "with amounts, e.g. '2 eggs'"},
                  steps={**STRINGS, "description": "in order, without numbers"}))
    async def show_recipe(a):
        recipe_open(device, a["title"], a["ingredients"], a["steps"])
        await on_card(recipe_card(device))
        return _text(f"Showing {a['title']} on the screen: {len(a['ingredients'])} ingredients, "
                     f"{len(a['steps'])} steps, starting with the ingredients.")

    @tool("recipe_step", "Move through the recipe on the screen and get the step to read out: next, previous, "
          "repeat (the current one), or first (step 1).",
          _schema(direction={"type": "string", "enum": ["next", "previous", "repeat", "first"]}))
    async def recipe_step(a):
        r = recipes.get(device)
        if r is None:
            return _text("No recipe is open. Use show_recipe first.")
        before = r["step"]
        recipe_move(device, a["direction"])
        stuck = r["step"] == before and a["direction"] in ("next", "previous")
        note = ("There are no more steps. " if a["direction"] == "next" else "That's the beginning. ") if stuck else ""
        await on_card(recipe_card(device))
        return _text(note + recipe_text(r))

    @tool("play_music", "Play music on this speaker through Spotify: a song, an artist, an album, or a playlist.",
          _schema(query={"type": "string", "description": "what to search for, e.g. 'Blue by Joni Mitchell'"},
                  kind={"type": "string", "enum": list(spotify.KINDS)}))
    async def play_music(a):
        async def find():
            found = await music.search(a["query"], a["kind"])
            await music.speaker()  # a missing speaker is said now, not found out after the reply
            await after_reply(lambda: start(found))
            return found

        async def start(found):
            try:
                await music.start(found, a["kind"])
            except spotify.SpotifyError as e:  # Claude has finished talking; say it on the screen
                await on_card(text_card("Music", [str(e)]))
            music.changed.set()

        return await via_spotify(find, lambda f: f"Playing {f['name']}" + (f" by {f['by']}." if f["by"] else "."))

    @tool("pause_music", "Pause the music.", _schema())
    async def pause_music(a):
        return await via_spotify(lambda: music.pause(), lambda _: "Paused.")

    @tool("resume_music", "Carry on playing the music that was paused.", _schema())
    async def resume_music(a):
        return await via_spotify(lambda: music.resume(), lambda _: "Playing.")

    @tool("skip_track", "Skip to the next track.", _schema())
    async def skip_track(a):
        return await via_spotify(lambda: music.next(), lambda _: "Skipped.")

    @tool("previous_track", "Go back to the previous track.", _schema())
    async def previous_track(a):
        return await via_spotify(lambda: music.previous(), lambda _: "Gone back.")

    @tool("whats_playing", "What's playing on Spotify right now; also shows it on the screen.", _schema())
    async def whats_playing(a):
        async def look():
            return spotify.track(await music.playback())

        def say(t):
            if t is None:
                return "Nothing is playing."
            return f"{t['title']}" + (f" by {t['artist']}" if t["artist"] else "") + ("" if t["is_playing"] else ", paused") + "."
        result = await via_spotify(look, say)
        await on_card({"kind": "music"})
        return result

    @tool("set_volume", "Set this speaker's volume, 0 to 100. For 'turn it up' or 'louder' use relative 10; "
          "for 'turn it down' or 'quieter', relative -10.",
          _schema(level={"type": "integer", "description": "the new level, or the change when relative"},
                  relative={"type": "boolean"}))
    async def set_volume(a):
        level = await on_volume(a["level"], a["relative"])
        return _text("The speaker isn't connected." if level is None else f"Volume is now {level}.")

    where = ", ".join(f"{c['name']} ({c['area']})" if c["area"] and c["area"] != c["name"] else c["name"]
                      for c in cameras)

    @tool("show_camera", f"Show a security camera's live view on this speaker's screen, for about two minutes "
          f"or until closed. "
          f"The cameras, with where they are: {where}.",
          _schema(camera={"type": "string", "enum": [c["name"] for c in cameras]}))
    async def show_camera(a):
        cam = next((c for c in cameras if c["name"] == a["camera"]), None)
        if cam is None:
            return _text(f"There's no camera called {a['camera']}. The cameras are: {where}.")
        await on_camera(cam)
        return _text(f"The {cam['name']} camera is opening on the screen; it takes a few seconds to connect.")

    @tool("close_camera", "Close the camera view on this speaker's screen.", _schema())
    async def close_camera(a):
        await on_camera(None)
        return _text("Closed.")

    return [set_timer, list_timers, cancel_timer, pause_timer, resume_timer, set_alarm, list_alarms, cancel_alarm, remember_tool, forget_tool,
            add_shopping, remove_shopping, get_shopping, clear_shopping, get_weather, show_on_screen, get_agenda,
            morning_briefing, show_recipe, recipe_step, play_music, pause_music, resume_music, skip_track,
            previous_track, whats_playing, set_volume] + ([show_camera, close_camera] if cameras else [])


def make_tools(device: str, on_card=_no_screen, on_volume=_no_speaker, music: spotify.Spotify | None = None,
               categories: list[str] = (), after_reply=_right_away, cameras: list[dict] = (), on_camera=_no_screen):
    """The MCP tool server Claude's session uses for one satellite."""
    return create_sdk_mcp_server("home", tools=tool_list(device, on_card, on_volume, music, categories, after_reply,
                                                         cameras, on_camera))
