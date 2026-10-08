"""A fake brain for designing the display: the real display/ files, canned data, no Claude, no Pi.

Run: uv run python fake_brain.py   (then open http://localhost:8001/ at 800x480)
Scenes: GET /scene lists them, GET /scene/<name> pushes one to every open screen.
Taps the page sends are printed; opening a sheet answers with its canned card, the way the brain would.
To prototype a feature the brain can't do yet, add a scene that sends the new message, then build it in app.js.
"""
import datetime as dt
import json
import pathlib
import time
import urllib.parse

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

HERE = pathlib.Path(__file__).parent
app = FastAPI()
screens: set[WebSocket] = set()
recipe = {"step": -1}  # where the open recipe is, the way the brain tracks it


def at(days=0, h=0, m=0) -> float:
    """A time of day, days from today."""
    return dt.datetime.combine(dt.date.today() + dt.timedelta(days=days), dt.time(h, m)).timestamp()


def soon(minutes=0.0) -> float:
    return time.time() + minutes * 60


def event(title, calendar, start, end, all_day=False):
    return {"title": title, "calendar": calendar, "start": start, "end": end, "all_day": all_day}


def alarm(id, time_, days="", label="", enabled=True, next_ring=None):
    return {"id": id, "time": time_, "days": days, "label": label, "enabled": enabled,
            "next_ring": next_ring or at(1, *map(int, time_.split(":")))}


WEATHER = {"place": "New York", "now": {"temp": 47, "feels": 43, "condition": "clear", "code": 0, "wind": 3},
           "days": [{"label": "Today", "condition": "clear", "code": 0, "high": 65, "low": 44, "rain": 10},
                    {"label": "Tomorrow", "condition": "overcast", "code": 3, "high": 76, "low": 56, "rain": 40},
                    {"label": "Friday", "condition": "rain", "code": 61, "high": 58, "low": 49, "rain": 80}]}


def timers():
    return [{"id": 1, "label": "pasta", "ends_at": soon(4.2)},
            {"id": 2, "label": "", "ends_at": soon(10), "duration": 600},
            {"id": 3, "label": "bread", "ends_at": soon(), "duration": 3600, "paused_left": 2400}]


def alarms():
    return [alarm(1, "06:30", "01234", "gym"), alarm(2, "21:15", "56", "reading", enabled=False)]


def events():
    return [event("Recycling day", "Personal", at(), at(1), all_day=True),
            event("Standup", "Work", soon(-10), soon(20)),  # under way, so the strip says Now
            event("Dentist", "Personal", at(0, 15), at(0, 16)),
            event("Swim lessons", "Kids Sports", at(0, 17, 30), at(0, 18, 15)),
            event("Call with Sam", "Work", at(1, 10), at(1, 11)),
            event("Grandma's birthday", "Family", at(2), at(3), all_day=True)]


SHOPPING = [{"name": "Produce", "items": ["Bananas", "Spinach", "Lemons"]},
            {"name": "Dairy", "items": ["Milk", "Eggs", "Greek yogurt"]},
            {"name": "", "items": ["Sourdough bread"]}]

PANCAKES = {"kind": "recipe", "title": "Pancakes",
            "ingredients": ["1 cup flour", "2 tbsp sugar", "2 tsp baking powder", "Pinch of salt", "1 cup milk",
                            "1 egg", "2 tbsp melted butter", "Butter for the pan"],
            "steps": ["Whisk the flour, sugar, baking powder and salt in a large bowl.",
                      "Whisk the milk, egg and melted butter, then stir into the dry ingredients until just combined; "
                      "a few lumps are fine, so stop as soon as no dry flour remains and let the batter rest.",
                      "Pour a quarter cup of batter per pancake onto the buttered pan.",
                      "Flip when bubbles form, and cook until golden."]}

TRACK = {"title": "A Case of You", "artist": "Joni Mitchell", "album": "Blue",
         "art": "data:image/svg+xml," + urllib.parse.quote(  # stand-in album art
             '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><defs><linearGradient id="g">'
             '<stop stop-color="#285aa0"/><stop offset="1" stop-color="#e6c878"/></linearGradient></defs>'
             '<rect width="300" height="300" fill="url(#g)"/></svg>'),
         "uri": "spotify:track:1", "is_playing": True, "progress_ms": 61000, "duration_ms": 263000}


def ambient(**over):
    return {"type": "ambient", "weather": WEATHER, "timers": timers(), "next_alarm": alarms()[0],
            "agenda": [e for e in events() if e["start"] < at(1)], **over}


def card(kind, **fields):
    return {"type": "card", "card": {"kind": kind, **fields}}


def talk(heard, *reply):
    return [{"type": "state", "state": "listening"}, {"type": "state", "state": "thinking"},
            {"type": "heard", "text": heard}, {"type": "state", "state": "speaking"},
            *[{"type": "reply", "text": r} for r in reply]]


SHEETS = {
    "weather": lambda: card("weather", **WEATHER),
    "timers": lambda: card("timers", timers=timers()),
    "alarms": lambda: card("alarms", alarms=alarms()),
    "agenda": lambda: card("agenda", events=events()),
    "shopping": lambda: card("shopping", groups=SHOPPING),
}

SCENES = {
    "home": lambda: [{"type": "state", "state": "idle"}, {"type": "ringing", "ringing": None}, ambient()],
    "empty": lambda: [ambient(weather=None, timers=[], next_alarm=None, agenda=[])],
    "many-timers": lambda: [ambient(timers=[{"id": i, "label": f"timer {i}", "ends_at": soon(i * 3)}
                                            for i in range(1, 9)])],
    "listening": lambda: [{"type": "state", "state": "listening"}],
    "thinking": lambda: [{"type": "state", "state": "thinking"}, {"type": "heard", "text": "What's the weather like?"}],
    "reply": lambda: talk("What's the weather like this afternoon?",
                          "Sunny and mild, topping out around sixty-five.", "A light breeze picks up after four.",
                          "Tomorrow looks cloudier."),
    "long-reply": lambda: talk("How do I make pancakes?", PANCAKES["steps"][1] * 2),
    "idle": lambda: [{"type": "state", "state": "idle"}],
    **{name: (lambda f=f: [f()]) for name, f in SHEETS.items()},
    "text": lambda: [card("text", title="Pancakes", lines=PANCAKES["ingredients"])],
    "briefing": lambda: [card("briefing", greeting="Good morning", weather=WEATHER, events=events()[:4],
                              next_alarm=alarms()[0], timers=timers()[:1], shopping=7)],
    "recipe": lambda: recipe.update(step=-1) or [card(**PANCAKES, step=-1)],
    "music": lambda: [{"type": "now_playing", "track": TRACK}, card("music")],
    "no-music": lambda: [{"type": "now_playing", "track": None}],
    "volume": lambda: [{"type": "volume", "level": 40}, card("volume")],
    "alarm-ringing": lambda: [{"type": "ringing", "ringing": {"kind": "alarm", "id": 1, "label": "gym", "time": "06:30"}}],
    "timer-ringing": lambda: [{"type": "ringing", "ringing": {"kind": "timer", "id": 1, "label": "pasta"}}],
    "stop-ringing": lambda: [{"type": "ringing", "ringing": None}],
    "night": lambda: [hello("00:00", "23:59")],
    "day": lambda: [hello("23:59", "23:59")],
}


def hello(start="22:00", end="07:00"):
    return {"type": "hello", "device": "fake", "version": "fake", "night": {"start": start, "end": end}}


async def push(*msgs, to=None):
    for ws in [to] if to else list(screens):
        for m in msgs:
            await ws.send_text(json.dumps(m))


@app.websocket("/display")
async def display(ws: WebSocket):
    await ws.accept()
    screens.add(ws)
    try:
        await push(hello("23:59", "23:59"), *SCENES["home"](), {"type": "volume", "level": 40},
                   {"type": "now_playing", "track": TRACK}, to=ws)
        while True:
            m = json.loads(await ws.receive_text())
            print("screen ->", m)
            if m.get("type") == "open" and m.get("sheet") in SHEETS:
                await push(SHEETS[m["sheet"]](), to=ws)
            elif m.get("type") == "recipe" and m.get("action") in ("next", "previous"):
                step = recipe["step"] + (1 if m["action"] == "next" else -1)
                recipe["step"] = max(-1, min(len(PANCAKES["steps"]) - 1, step))
                await push(card(**PANCAKES, step=recipe["step"]), to=ws)
            elif m.get("type") == "recipe":
                recipe["step"] = -1
    except WebSocketDisconnect:
        pass
    finally:
        screens.discard(ws)


@app.get("/scene")
async def scene_list():
    return JSONResponse(sorted(SCENES))


@app.get("/scene/{name}")
async def scene(name: str):
    if name not in SCENES:
        return JSONResponse({"error": f"no scene {name!r}", "scenes": sorted(SCENES)}, status_code=404)
    await push(*SCENES[name]())
    return JSONResponse({"pushed": name, "screens": len(screens)})


@app.get("/photos")
async def photo_list():
    return JSONResponse([])


@app.get("/")
async def root():
    return RedirectResponse("/display/?device=fake")


app.mount("/display", StaticFiles(directory=HERE / "display", html=True), name="display")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
