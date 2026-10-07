"""Claude's tools: timers, memory, shopping list, weather.

To add a tool: write an async function with @tool (name, description for Claude, JSON schema of its
arguments) inside make_tools(), and add it to the list at the bottom. Storage helpers are plain functions
so they can be tested without Claude.
"""

import datetime
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

DB_PATH = Path(os.environ.get("CLAUDE_HOME_DB", Path(__file__).parent / "home.db"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS timers (id INTEGER PRIMARY KEY, device TEXT, label TEXT, ends_at REAL);
CREATE TABLE IF NOT EXISTS memory (fact TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS shopping (item TEXT PRIMARY KEY COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS alarms (id INTEGER PRIMARY KEY, device TEXT, time TEXT, days TEXT, label TEXT,
                                   enabled INTEGER DEFAULT 1, next_ring REAL);
"""


def q(sql: str, *args) -> list[tuple]:
    with closing(sqlite3.connect(DB_PATH)) as con, con:  # inner `with con` commits
        con.executescript(SCHEMA)
        return con.execute(sql, args).fetchall()


# --- Storage -----------------------------------------------------------------------------------

def shopping_add(items: list[str]) -> None:
    for item in items:
        q("INSERT OR IGNORE INTO shopping VALUES (?)", item.strip())


def shopping_remove(items: list[str]) -> list[str]:
    """Remove items (case-insensitive). Returns the ones that weren't on the list."""
    missing = []
    for item in items:
        if not q("DELETE FROM shopping WHERE item = ? RETURNING item", item.strip()):
            missing.append(item)
    return missing


def shopping_items() -> list[str]:
    return [r[0] for r in q("SELECT item FROM shopping ORDER BY rowid")]


def remember(fact: str) -> None:
    q("INSERT OR IGNORE INTO memory VALUES (?)", fact.strip())


def forget(text: str) -> list[str]:
    """Forget every fact containing text (case-insensitive). Returns what was forgotten."""
    return [r[0] for r in q("DELETE FROM memory WHERE instr(lower(fact), lower(?)) RETURNING fact", text)]


def facts() -> list[str]:
    return [r[0] for r in q("SELECT fact FROM memory ORDER BY rowid")]


def timer_add(device: str, label: str, seconds: float) -> None:
    q("INSERT INTO timers (device, label, ends_at) VALUES (?, ?, ?)", device, label, time.time() + seconds)


def timers(device: str) -> list[tuple[str, float]]:
    """(label, seconds left) for a device's running timers, soonest first."""
    now = time.time()
    return [(label, ends - now) for label, ends in
            q("SELECT label, ends_at FROM timers WHERE device = ? ORDER BY ends_at", device)]


def timer_list(device: str) -> list[dict]:
    """A device's running timers for the screen, soonest first; the screen counts down from ends_at."""
    return [{"id": i, "label": label, "ends_at": ends} for i, label, ends in
            q("SELECT id, label, ends_at FROM timers WHERE device = ? ORDER BY ends_at", device)]


def timer_cancel(device: str, label: str) -> int:
    """Cancel timers whose label contains label (all of the device's timers if empty). Returns how many."""
    return len(q("DELETE FROM timers WHERE device = ? AND instr(lower(label), lower(?)) RETURNING id", device, label))


def timer_cancel_id(device: str, timer_id: int) -> bool:
    """Cancel one of a device's timers by id (a Cancel button). Returns whether it existed."""
    return bool(q("DELETE FROM timers WHERE device = ? AND id = ? RETURNING id", device, timer_id))


def timers_due() -> list[tuple[str, str]]:
    """Pop finished timers: [(device, label)]."""
    return q("DELETE FROM timers WHERE ends_at <= ? RETURNING device, label", time.time())


def spoken_duration(seconds: float) -> str:
    s = max(0, round(seconds))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    parts = [f"{n} {unit}{'s' if n != 1 else ''}" for n, unit in ((h, "hour"), (m, "minute"), (s, "second")) if n]
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
    return {"kind": "timers", "timers": timer_list(device), "alarms": alarm_list(device)}


def weather_card(w: dict) -> dict:
    return {"kind": "weather", **w}


def shopping_card() -> dict:
    return {"kind": "shopping", "items": shopping_items()}


def text_card(title: str, lines: list[str]) -> dict:
    return {"kind": "text", "title": title, "lines": lines}


async def _no_screen(card: dict):
    pass


async def _no_speaker(level: int, relative: bool) -> None:
    return None


def tool_list(device: str, on_card=_no_screen, on_volume=_no_speaker) -> list:
    """Claude's tools for one satellite. Timers belong to the device they were set on.
    on_card(card) is awaited to put a result on that device's screens.
    on_volume(level, relative) sets the speaker's volume and returns the new level (None: no speaker)."""

    @tool("set_timer", "Start a countdown timer that rings on this speaker.",
          _schema(seconds={"type": "integer", "minimum": 1},
                  label={"type": "string", "description": "short name, e.g. 'pasta'; empty if none given"}))
    async def set_timer(a):
        timer_add(device, a["label"], a["seconds"])
        await on_card(timers_card(device))
        return _text(f"Timer set for {spoken_duration(a['seconds'])}.")

    @tool("list_timers", "List this speaker's running timers and the time left on each.", _schema())
    async def list_timers(a):
        running = timers(device)
        await on_card(timers_card(device))
        return _text("; ".join(f"{label or 'unnamed'}: {spoken_duration(left)} left" for label, left in running)
                     or "No timers running.")

    @tool("cancel_timer", "Cancel timers whose label contains the given text, or all timers if it is empty.",
          _schema(label={"type": "string"}))
    async def cancel_timer(a):
        return _text(f"Cancelled {timer_cancel(device, a['label'])} timer(s).")

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
        await on_card(timers_card(device))
        return _text(f"Alarm set for {spoken_time(hhmm)} {spoken_days(days)}.")

    @tool("list_alarms", "List this speaker's alarms.", _schema())
    async def list_alarms(a):
        alarms = alarm_list(device)
        await on_card(timers_card(device))
        return _text("; ".join(f"{spoken_time(x['time'])} {spoken_days(x['days'])}"
                               + (f" ({x['label']})" if x["label"] else "") + ("" if x["enabled"] else ", off")
                               for x in alarms) or "No alarms set.")

    @tool("cancel_alarm", "Delete alarms whose label contains the text or whose time matches (HH:MM, 24-hour); "
          "all alarms if empty.", _schema(match={"type": "string"}))
    async def cancel_alarm(a):
        n = alarm_cancel(device, a["match"])
        await on_card(timers_card(device))
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

    @tool("add_to_shopping_list", "Add items to the shopping list.", _schema(items=STRINGS))
    async def add_shopping(a):
        shopping_add(a["items"])
        await on_card(shopping_card())
        return _text(f"Shopping list now: {', '.join(shopping_items())}")

    @tool("remove_from_shopping_list", "Remove items from the shopping list.", _schema(items=STRINGS))
    async def remove_shopping(a):
        missing = shopping_remove(a["items"])
        await on_card(shopping_card())
        left = ", ".join(shopping_items()) or "empty"
        return _text((f"Not on the list: {', '.join(missing)}. " if missing else "") + f"Shopping list now: {left}")

    @tool("get_shopping_list", "Read the shopping list.", _schema())
    async def get_shopping(a):
        await on_card(shopping_card())
        return _text(", ".join(shopping_items()) or "The shopping list is empty.")

    @tool("clear_shopping_list", "Remove everything from the shopping list.", _schema())
    async def clear_shopping(a):
        shopping_remove(shopping_items())
        await on_card(shopping_card())
        return _text("Shopping list cleared.")

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
          "definition, a comparison. Keep it short; the screen is small and read from across the room.",
          _schema(title={"type": "string"},
                  lines={**STRINGS, "description": "a few short lines, shown in order"}))
    async def show_on_screen(a):
        await on_card(text_card(a["title"], a["lines"]))
        return _text("Shown on the screen.")

    @tool("set_volume", "Set this speaker's volume, 0 to 100. For 'turn it up' or 'louder' use relative 10; "
          "for 'turn it down' or 'quieter', relative -10.",
          _schema(level={"type": "integer", "description": "the new level, or the change when relative"},
                  relative={"type": "boolean"}))
    async def set_volume(a):
        level = await on_volume(a["level"], a["relative"])
        return _text("The speaker isn't connected." if level is None else f"Volume is now {level}.")

    return [set_timer, list_timers, cancel_timer, set_alarm, list_alarms, cancel_alarm, remember_tool, forget_tool,
            add_shopping, remove_shopping, get_shopping, clear_shopping, get_weather, show_on_screen, set_volume]


def make_tools(device: str, on_card=_no_screen, on_volume=_no_speaker):
    """The MCP tool server Claude's session uses for one satellite."""
    return create_sdk_mcp_server("home", tools=tool_list(device, on_card, on_volume))
