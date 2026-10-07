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


def timers_due() -> list[tuple[str, str]]:
    """Pop finished timers: [(device, label)]."""
    return q("DELETE FROM timers WHERE ends_at <= ? RETURNING device, label", time.time())


def spoken_duration(seconds: float) -> str:
    s = max(0, round(seconds))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    parts = [f"{n} {unit}{'s' if n != 1 else ''}" for n, unit in ((h, "hour"), (m, "minute"), (s, "second")) if n]
    return " ".join(parts) or "0 seconds"


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
        return "home", lat, lon
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


def make_tools(device: str):
    """The tool server for one satellite. Timers belong to the device they were set on."""

    @tool("set_timer", "Start a countdown timer that rings on this speaker.",
          _schema(seconds={"type": "integer", "minimum": 1},
                  label={"type": "string", "description": "short name, e.g. 'pasta'; empty if none given"}))
    async def set_timer(a):
        timer_add(device, a["label"], a["seconds"])
        return _text(f"Timer set for {spoken_duration(a['seconds'])}.")

    @tool("list_timers", "List this speaker's running timers and the time left on each.", _schema())
    async def list_timers(a):
        running = timers(device)
        return _text("; ".join(f"{label or 'unnamed'}: {spoken_duration(left)} left" for label, left in running)
                     or "No timers running.")

    @tool("cancel_timer", "Cancel timers whose label contains the given text, or all timers if it is empty.",
          _schema(label={"type": "string"}))
    async def cancel_timer(a):
        return _text(f"Cancelled {timer_cancel(device, a['label'])} timer(s).")

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
        return _text(f"Shopping list now: {', '.join(shopping_items())}")

    @tool("remove_from_shopping_list", "Remove items from the shopping list.", _schema(items=STRINGS))
    async def remove_shopping(a):
        missing = shopping_remove(a["items"])
        left = ", ".join(shopping_items()) or "empty"
        return _text((f"Not on the list: {', '.join(missing)}. " if missing else "") + f"Shopping list now: {left}")

    @tool("get_shopping_list", "Read the shopping list.", _schema())
    async def get_shopping(a):
        return _text(", ".join(shopping_items()) or "The shopping list is empty.")

    @tool("clear_shopping_list", "Remove everything from the shopping list.", _schema())
    async def clear_shopping(a):
        shopping_remove(shopping_items())
        return _text("Shopping list cleared.")

    @tool("get_weather", "Current conditions and a 3-day forecast. Faster and more accurate than web search for weather.",
          _schema(place={"type": "string", "description": "city name; empty for home"}))
    async def get_weather(a):
        place = a["place"] or os.environ.get("HOME_LOCATION", "")
        if not place:
            return _text("Home location isn't configured (HOME_LOCATION). Ask which city.")
        try:
            return _text(weather_text(await weather_data(place)))
        except (LookupError, httpx.HTTPError) as e:
            return _text(f"Weather lookup failed: {e}")

    return create_sdk_mcp_server("home", tools=[
        set_timer, list_timers, cancel_timer, remember_tool, forget_tool,
        add_shopping, remove_shopping, get_shopping, clear_shopping, get_weather,
    ])
