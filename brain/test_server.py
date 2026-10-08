"""Server tests: the FastAPI app on 127.0.0.1 with the fake LLM and a temp database.

Run: uv run python test_server.py
"""
import asyncio
import io
import json
import logging
import os
import socket
import tempfile
import threading
import time
import urllib.parse

TMP = tempfile.mkdtemp()
PICS = os.path.join(TMP, "photos")
os.environ.update(CLAUDE_HOME_FAKE_LLM="1", CLAUDE_HOME_DB=os.path.join(TMP, "test.db"),
                  CLAUDE_HOME_LOG=os.path.join(TMP, "turns.jsonl"), PHOTOS_DIR=PICS,
                  SPOTIFY_TOKEN=os.path.join(TMP, "no_spotify_token.json"))  # before importing the brain
os.environ.pop("HOME_LOCATION", None)  # no forecasts: the briefing checks expect none

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402
from PIL import Image  # noqa: E402

import brain  # noqa: E402
import todoist  # noqa: E402

todoist.fake()


class Errors(logging.Handler):
    """Collects anything the server logs at ERROR, so a swallowed traceback still fails the test."""
    records: list = []

    def emit(self, record):
        self.records.append(record)


errors = Errors(level=logging.ERROR)
for name in ("uvicorn.error", "uvicorn.asgi", "fastapi"):
    logging.getLogger(name).addHandler(errors)


def start() -> tuple[uvicorn.Server, int]:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(brain.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 30
    while not server.started:
        assert time.time() < deadline, "server didn't start"
        time.sleep(0.05)
    return server, port


async def satellite_turn(port: int, text: str) -> list:
    """Run one text turn as a satellite; return everything received up to and including audio_end."""
    got = []
    async with websockets.connect(f"ws://127.0.0.1:{port}/satellite?device=sat1") as ws:
        await ws.send(json.dumps({"type": "hello", "device": "sat1"}))
        await ws.send(json.dumps({"type": "text", "text": text}))
        while True:
            msg = await asyncio.wait_for(ws.recv(), 30)
            got.append(msg if isinstance(msg, bytes) else json.loads(msg))
            if isinstance(got[-1], dict) and got[-1]["type"] == "audio_end":
                return got


async def abrupt_disconnect(port: int):
    """Start a turn, then drop the TCP connection without a WebSocket close."""
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/satellite?device=rude")
    await ws.send(json.dumps({"type": "text", "text": "hello there"}))
    await asyncio.wait_for(ws.recv(), 30)  # audio_start: the turn is underway
    ws.transport.abort()


async def recv_json(ws, timeout=30):
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout)
        if isinstance(msg, str):
            return json.loads(msg)


async def open_display(port: int, name: str):
    """Connect a display and check its greeting: hello, an ambient snapshot, then the current state."""
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/display?device={name}")
    hello, ambient, state = [await recv_json(ws) for _ in range(3)]
    assert hello["type"] == "hello" and hello["device"] == name and hello["night"] == {"start": "22:00", "end": "07:00"}, hello
    assert hello["version"] == brain.DISPLAY_VERSION and hello["version"].isdigit(), hello
    assert ambient["type"] == "ambient" and {"weather", "timers", "next_alarm", "agenda"} <= ambient.keys(), ambient
    assert state == {"type": "state", "state": "idle"}, state
    return ws


async def until_idle(ws) -> list:
    """Display messages up to and including the return to idle."""
    got = []
    while not got or got[-1] != {"type": "state", "state": "idle"}:
        got.append(await recv_json(ws))
    return got


async def play_out(sat):
    """Act as the satellite: take the reply's audio, then report that it finished playing."""
    while (await recv_json(sat))["type"] != "audio_end":
        pass
    await sat.send(json.dumps({"type": "played"}))


FIXTURE = os.path.join(os.path.dirname(__file__), "test_data", "openmeteo.json")


async def on_server(coro):
    """Run a coroutine on the server's event loop (tool handlers send over its sockets)."""
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, brain.app.state.loop))


async def until_type(ws, kind: str) -> dict:
    """Skip messages until one of this type (ambient updates and state changes interleave)."""
    while (m := await recv_json(ws))["type"] != kind:
        pass
    return m


async def until_card(ws, kind: str) -> dict:
    """Skip messages until a card of this kind; return the card."""
    while (m := await until_type(ws, "card"))["card"]["kind"] != kind:
        pass
    return m["card"]


async def display_flow(port: int):
    a, b, other = [await open_display(port, n) for n in ("kitchen", "kitchen", "office")]
    async with websockets.connect(f"ws://127.0.0.1:{port}/satellite?device=kitchen") as sat:
        await sat.send(json.dumps({"type": "hello", "device": "kitchen"}))

        # A spoken-style request from the satellite: every kitchen display follows the whole turn, in order
        await sat.send(json.dumps({"type": "text", "text": "hello there"}))
        await play_out(sat)
        expected = [{"type": "state", "state": "thinking"}, {"type": "heard", "text": "hello there"},
                    {"type": "state", "state": "speaking"}, {"type": "reply", "text": "This is a canned reply."},
                    {"type": "reply", "text": "That is all."}, {"type": "state", "state": "idle"}]
        assert await until_idle(a) == expected, "display a"
        assert await until_idle(b) == expected, "display b"

        # The office display heard none of it
        try:
            stray = await recv_json(other, timeout=0.5)
            raise AssertionError(f"office display got {stray}")
        except TimeoutError:
            pass

        # A display can start a turn; the reply plays on that device's satellite
        await a.send(json.dumps({"type": "text", "text": "from the screen"}))
        await play_out(sat)
        got = await until_idle(a)
        assert {"type": "heard", "text": "from the screen"} in got and got[-1]["state"] == "idle", got

        # The wake word shows listening; a cancelled request goes back to idle
        await sat.send(json.dumps({"type": "wake"}))
        assert await recv_json(a) == {"type": "state", "state": "listening"}
        await sat.send(json.dumps({"type": "cancel"}))
        assert await recv_json(a) == {"type": "state", "state": "idle"}

        # Tap-to-talk reaches the satellite as listen
        await a.send(json.dumps({"type": "talk"}))
        assert await recv_json(sat) == {"type": "listen"}

        # Volume: the satellite's report reaches the screens; the screen's slider reaches the satellite
        await sat.send(json.dumps({"type": "volume", "level": 82}))
        assert await recv_json(a) == {"type": "volume", "level": 82}
        await a.send(json.dumps({"type": "volume", "level": 140}))
        assert await recv_json(sat) == {"type": "volume", "level": 100}, "volume should be clamped"
        assert await until_type(b, "volume") == {"type": "volume", "level": 82}
        assert await until_type(b, "volume") == {"type": "volume", "level": 100}, "every screen follows the slider"
        await sat.send(json.dumps({"type": "volume", "level": 70}))  # what the satellite actually set
        assert await until_type(a, "volume") == {"type": "volume", "level": 100}
        assert await until_type(a, "volume") == {"type": "volume", "level": 70}

        # Claude's volume tool: "turn it up" is relative to the reported level; the screens show the new level
        volume_tools = {t.name: t for t in brain.tools.tool_list("kitchen", brain.show_card("kitchen"),
                                                                  brain.speaker("kitchen"))}
        r = await on_server(volume_tools["set_volume"].handler({"level": 10, "relative": True}))
        assert r["content"][0]["text"] == "Volume is now 80.", r
        assert await recv_json(sat) == {"type": "volume", "level": 80}
        assert await until_card(b, "volume") == {"kind": "volume", "level": 80}
        await on_server(volume_tools["set_volume"].handler({"level": -30, "relative": False}))
        assert await recv_json(sat) == {"type": "volume", "level": 0}
        r = await brain.tools.tool_list("nowhere", on_volume=brain.speaker("nowhere"))[-1].handler(
            {"level": 50, "relative": False})
        assert r["content"][0]["text"] == "The speaker isn't connected.", r

        # Stop while idle drops whatever the satellite still has queued
        await a.send(json.dumps({"type": "stop"}))
        assert await recv_json(sat) == {"type": "stop"}

        # Stop mid-reply: the turn ends early with stop then audio_end, and the screen goes idle
        await a.send(json.dumps({"type": "text", "text": "tell me a long story"}))
        got = []
        while got[-1:] != [{"type": "reply", "text": "This is sentence 1 of a long reply."}]:
            got.append(await recv_json(a))
        await a.send(json.dumps({"type": "stop"}))
        to_sat = []
        while not to_sat or to_sat[-1].get("type") != "audio_end":
            to_sat.append(await recv_json(sat))
        assert to_sat[-2:] == [{"type": "stop"}, {"type": "audio_end", "follow_up": False}], to_sat
        rest = await until_idle(a)
        replies = [m for m in got + rest if m["type"] == "reply"]
        assert len(replies) < 5, f"reply wasn't cut short: {replies}"
        # The next request works normally
        await a.send(json.dumps({"type": "text", "text": "hello again"}))
        await play_out(sat)
        assert {"type": "reply", "text": "That is all."} in await until_idle(a)
        # A timer set (as Claude's tool would) shows up on the kitchen screens within a second, and goes away
        brain.tools.timer_add("kitchen", "pasta", 600)
        m = await recv_json(a, timeout=3)
        assert m["type"] == "ambient" and [t["label"] for t in m["timers"]] == ["pasta"], m
        assert abs(m["timers"][0]["ends_at"] - (time.time() + 600)) < 5, m["timers"]
        brain.tools.timer_cancel("kitchen", "pasta")
        m = await recv_json(a, timeout=3)
        assert m["type"] == "ambient" and m["timers"] == [], m

        # Claude's tools put their results on the device's screens: a timer, a weather lookup, a list
        kitchen_tools = {t.name: t for t in brain.tools.tool_list("kitchen", brain.show_card("kitchen"))}
        await on_server(kitchen_tools["set_timer"].handler({"seconds": 300, "label": "eggs"}))
        card = await until_card(a, "timers")  # (volume cards from above may still be queued)
        assert [t["label"] for t in card["timers"]] == ["eggs"], card
        await until_card(b, "timers")  # every kitchen screen gets the card
        # (the timer watcher polls every 0.5 s; wait until it has seen this timer, or a quick cancel goes unnoticed)
        while [t["label"] for t in (await until_type(a, "ambient"))["timers"]] != ["eggs"]:
            pass
        await on_server(kitchen_tools["show_on_screen"].handler({"title": "Pancakes", "lines": ["Flour", "Eggs"]}))
        assert (await until_type(a, "card"))["card"] == {"kind": "text", "title": "Pancakes", "lines": ["Flour", "Eggs"]}

        # Opening sheets from the screen: only the screen that asked gets the card
        brain.weather_now = brain.tools.parse_forecast("Home", json.load(open(FIXTURE)))
        await a.send(json.dumps({"type": "open", "sheet": "weather"}))
        m = await until_type(a, "card")
        assert m["card"]["kind"] == "weather" and m["card"]["now"]["temp"] == 47, m
        await a.send(json.dumps({"type": "open", "sheet": "timers"}))
        assert [t["label"] for t in (await until_type(a, "card"))["card"]["timers"]] == ["eggs"]
        await a.send(json.dumps({"type": "open", "sheet": "nonsense"}))  # ignored, nothing breaks

        # Pause and resume from the screen by id; every screen hears through the timer watcher
        eggs = brain.tools.timer_list("kitchen")[0]["id"]
        await a.send(json.dumps({"type": "timer_pause", "id": eggs}))
        while (await until_type(b, "ambient"))["timers"][0]["paused_left"] is None:
            pass
        await a.send(json.dumps({"type": "timer_resume", "id": eggs}))
        while (await until_type(b, "ambient"))["timers"][0]["paused_left"] is not None:
            pass
        await a.send(json.dumps({"type": "open", "sheet": "alarms"}))
        assert (await until_card(a, "alarms")) == {"kind": "alarms", "alarms": []}

        # Cancel from the screen by id; screens hear about it through the timer watcher
        await a.send(json.dumps({"type": "timer_cancel", "id": eggs}))
        m = await until_type(a, "ambient")
        while m["timers"]:
            m = await until_type(a, "ambient")
        assert brain.tools.timer_list("kitchen") == []

        # Shopping: Claude's tools show the list; checking an item off on one screen updates just that screen
        await on_server(kitchen_tools["add_to_shopping_list"].handler({"items": [
            {"item": "Milk", "category": "dairy"}, {"item": "eggs", "category": "dairy"}, {"item": "Bread", "category": ""}]}))
        assert await until_card(a, "shopping") == {"kind": "shopping", "groups": [
            {"name": "", "items": ["Bread"]}, {"name": "Meat & Dairy", "items": ["Milk", "eggs"]}]}
        await until_card(b, "shopping")
        await a.send(json.dumps({"type": "shopping_remove", "item": "EGGS"}))
        after = [{"name": "", "items": ["Bread"]}, {"name": "Meat & Dairy", "items": ["Milk"]}]
        assert (await until_card(a, "shopping"))["groups"] == after
        assert await on_server(brain.tools.shopping_items()) == ["Bread", "Milk"]
        await b.send(json.dumps({"type": "open", "sheet": "shopping"}))
        assert (await until_card(b, "shopping"))["groups"] == after, "b sees the change when it opens"
        await on_server(brain.tools.shopping_remove(["Milk", "Bread"]))

        # The agenda sheet opens with whatever the calendars have (no feeds here, so nothing)
        brain.agenda.events[:] = [{"title": "Dentist", "calendar": "Family", "start": time.time() + 3600,
                                   "end": time.time() + 7200, "all_day": False}]
        await a.send(json.dumps({"type": "open", "sheet": "agenda"}))
        assert [e["title"] for e in (await until_card(a, "agenda"))["events"]] == ["Dentist"]
        assert [e["title"] for e in brain.ambient(brain.device("kitchen"))["agenda"]] in (["Dentist"], [])  # [] near midnight
        # The morning briefing goes up on every kitchen screen (no HOME_LOCATION here, so no forecast)
        r = await on_server(kitchen_tools["morning_briefing"].handler({}))
        card = await until_card(a, "briefing")
        assert card["weather"] is None and [e["title"] for e in card["events"]] in (["Dentist"], [])
        assert card["greeting"].startswith("Good ") and r["content"][0]["text"].startswith(card["greeting"]), r
        await until_card(b, "briefing")
        brain.agenda.events.clear()

        # Recipe: Next on one screen moves every kitchen screen along; closing the sheet ends recipe mode
        await on_server(kitchen_tools["show_recipe"].handler(
            {"title": "Pancakes", "ingredients": ["1 cup flour"], "steps": ["Whisk.", "Cook."]}))
        assert (await until_card(b, "recipe"))["step"] == -1
        await a.send(json.dumps({"type": "recipe", "action": "next"}))
        assert (await until_card(b, "recipe"))["step"] == 0, "the other screen follows"
        await a.send(json.dumps({"type": "recipe", "action": "close"}))
        deadline = time.time() + 3
        while "kitchen" in brain.tools.recipes:
            assert time.time() < deadline, "close should end recipe mode"
            await asyncio.sleep(0.05)
        r = await on_server(kitchen_tools["recipe_step"].handler({"direction": "next"}))
        assert r["content"][0]["text"] == "No recipe is open. Use show_recipe first.", r
    for ws in (a, b, other):
        await ws.close()


async def alarm_flow(port: int):
    """Alarms ring on the device's screens and satellite until stopped, snoozed, answered, or the limit."""
    brain.BEEP_EVERY_S, brain.RING_LIMIT_S = 0.2, 30
    screen = await open_display(port, "bedroom")
    async with websockets.connect(f"ws://127.0.0.1:{port}/satellite?device=bedroom") as sat:
        await sat.send(json.dumps({"type": "hello", "device": "bedroom"}))

        def due_now(days="", label="wake up"):
            alarm = brain.tools.alarm_add("bedroom", "06:30", days, label)
            brain.tools.q("UPDATE alarms SET next_ring = ? WHERE id = ?", time.time() - 0.2, alarm)
            return alarm

        async def rings() -> dict:
            m = await until_type(screen, "ringing")
            assert m["ringing"] and m["ringing"]["kind"] == "alarm", m
            return m["ringing"]

        async def stops():
            while (await until_type(screen, "ringing"))["ringing"] is not None:
                pass

        # Rings: the screen shows it, the satellite gets announcements; Stop ends it and a one-time alarm is gone
        once = due_now()
        r = await rings()
        assert r["id"] == once and r["label"] == "wake up" and r["time"] == "06:30", r
        assert await recv_json(sat) == {"type": "ringing", "on": True}  # the satellite listens for "stop" meanwhile
        m = await recv_json(sat)
        assert m["type"] == "audio_start" and m.get("announce"), m
        await screen.send(json.dumps({"type": "stop"}))
        await stops()
        while (await recv_json(sat)).get("type") != "stop":  # the satellite drops the queued beeps
            pass
        deadline = time.time() + 5
        while any(a["id"] == once for a in brain.tools.alarm_list("bedroom")):
            assert time.time() < deadline, "one-time alarm should be removed after it rings"
            await asyncio.sleep(0.1)

        # Snooze: a weekday alarm stops ringing and comes back in 9 minutes
        weekday = due_now(days="01234", label="")
        await rings()
        await screen.send(json.dumps({"type": "snooze"}))
        await stops()
        await asyncio.sleep(0.5)
        [a] = [a for a in brain.tools.alarm_list("bedroom") if a["id"] == weekday]
        assert abs(a["next_ring"] - (time.time() + 540)) < 5, a
        brain.tools.alarm_delete("bedroom", weekday)

        # The wake word answers a ringing alarm
        due_now()
        await rings()
        await sat.send(json.dumps({"type": "wake"}))
        await stops()
        await sat.send(json.dumps({"type": "cancel"}))

        # Speech heard while ringing, no wake word (the satellite is told when to listen): "snooze" snoozes an alarm,
        # "stop" stops a timer, anything else is ignored. Transcripts are canned; the audio doesn't matter.
        heard = iter(["Snooze it.", "What time is it?", "Stop."])
        brain.transcribe = lambda groq, pcm: next(heard)

        async def say_while_ringing():
            await sat.send(bytes(32000))
            await sat.send(json.dumps({"type": "ring_speech"}))

        async def satellite_told(on: bool):
            while (await recv_json(sat)) != {"type": "ringing", "on": on}:
                pass

        weekday = due_now(days="01234", label="")
        await rings()
        await satellite_told(True)
        await say_while_ringing()
        await stops()
        await satellite_told(False)
        await asyncio.sleep(0.3)
        [a] = [a for a in brain.tools.alarm_list("bedroom") if a["id"] == weekday]
        assert abs(a["next_ring"] - (time.time() + 540)) < 5, a
        brain.tools.alarm_delete("bedroom", weekday)

        # A finished timer rings like an alarm, until stopped
        brain.tools.timer_add("bedroom", "pasta", 0)
        m = await until_type(screen, "ringing")
        assert m["ringing"] == {"kind": "timer", "label": "pasta", "duration": 0}, m
        await satellite_told(True)
        await say_while_ringing()  # "What time is it?"
        await asyncio.sleep(0.5)
        assert brain.device("bedroom").ringing is not None, "only words like stop end it"
        await say_while_ringing()  # "Stop."
        await stops()
        await satellite_told(False)
        assert next(heard, None) is None

        # Left alone, it gives up after the limit, and the one-time alarm is done
        brain.RING_LIMIT_S = 1
        gives_up = due_now()
        await rings()
        await asyncio.wait_for(stops(), 5)
        await asyncio.sleep(0.3)
        assert not any(a["id"] == gives_up for a in brain.tools.alarm_list("bedroom"))

        # From the timers sheet: turn an alarm off and on, delete it; the sheet refreshes, home learns the next alarm
        later = brain.tools.alarm_add("bedroom", "21:15", "56", "weekend reading")
        await screen.send(json.dumps({"type": "alarm_toggle", "id": later, "enabled": False}))
        card = await until_card(screen, "alarms")
        assert [(a["id"], a["enabled"]) for a in card["alarms"]] == [(later, False)], card
        await screen.send(json.dumps({"type": "alarm_toggle", "id": later, "enabled": True}))
        assert (await until_card(screen, "alarms"))["alarms"][0]["enabled"] is True
        m = await until_type(screen, "ambient")
        while not m["next_alarm"]:
            m = await until_type(screen, "ambient")
        assert m["next_alarm"]["id"] == later and m["next_alarm"]["time"] == "21:15", m["next_alarm"]
        await screen.send(json.dumps({"type": "alarm_delete", "id": later}))
        assert (await until_card(screen, "alarms"))["alarms"] == []

        # An alarm missed by more than the limit (the brain was down) is skipped, not rung late
        late = brain.tools.alarm_add("bedroom", "06:30", "", "late")
        brain.tools.q("UPDATE alarms SET next_ring = ? WHERE id = ?", time.time() - 3600, late)
        await asyncio.sleep(1.5)
        assert not any(a["id"] == late for a in brain.tools.alarm_list("bedroom"))
        try:
            stray = await until_type(screen, "ringing")
            raise AssertionError(f"a missed alarm rang late: {stray}")
        except TimeoutError:
            pass
    await screen.close()


async def music_flow(port: int):
    """Media buttons reach Spotify, and the now-playing poller tells screens about changes only.
    Never the real account: the brain's Spotify client is swapped for one whose transport is a mock."""
    hits, playing = [], {"now": None}

    def api(request):
        hits.append((request.method, request.url.path))
        if request.url.path == "/v1/me/player/next":
            return httpx.Response(404, json={"error": {"status": 404, "message": "Player command failed: No active device found"}})
        if request.method == "GET" and request.url.path == "/v1/me/player":
            return httpx.Response(200, json=playing["now"]) if playing["now"] else httpx.Response(204)
        return httpx.Response(204)

    tok = os.path.join(TMP, "spotify_token.json")
    open(tok, "w").write(json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": 10**12}))
    real = brain.app.state.spotify
    sp = brain.spotify.Spotify("cid", tok, transport=httpx.MockTransport(api))
    brain.app.state.spotify = sp
    try:
        screen = await open_display(port, "den")

        # A media button: the API call, then the poller is poked
        await screen.send(json.dumps({"type": "media", "action": "pause"}))
        deadline = time.time() + 3
        while ("PUT", "/v1/me/player/pause") not in hits:
            assert time.time() < deadline, hits
            await asyncio.sleep(0.05)
        assert sp.changed.is_set()
        # A failed command says why, on the screen that was tapped
        await screen.send(json.dumps({"type": "media", "action": "next"}))
        card = await until_card(screen, "text")
        assert card["title"] == "Music" and "No active device found" in card["lines"][0], card

        # The poller: a new track is pushed; the same track a moment later is not; a pause is
        playing["now"] = {"is_playing": True, "progress_ms": 1000, "item": {
            "type": "track", "name": "Blue", "uri": "spotify:track:1", "duration_ms": 180000,
            "artists": [{"name": "Joni Mitchell"}], "album": {"name": "Blue", "images": []}}}
        assert await on_server(brain.check_music(sp)) == 5.0
        m = await until_type(screen, "now_playing")
        assert m["track"]["title"] == "Blue" and m["track"]["is_playing"], m
        assert await on_server(brain.check_music(sp)) == 5.0
        try:
            stray = await recv_json(screen, timeout=0.5)
            raise AssertionError(f"unchanged playback was pushed again: {stray}")
        except TimeoutError:
            pass
        late = await open_display(port, "den")  # a screen connecting mid-song gets it straight away
        assert (await until_type(late, "now_playing"))["track"]["title"] == "Blue"
        playing["now"] = dict(playing["now"], is_playing=False)
        assert await on_server(brain.check_music(sp)) == 20.0
        assert (await until_type(screen, "now_playing"))["track"]["is_playing"] is False
        playing["now"] = None
        await on_server(brain.check_music(sp))
        assert (await until_type(screen, "now_playing"))["track"] is None
        await screen.close()
        await late.close()
    finally:
        brain.app.state.spotify = real
        brain.music_now = None
        await on_server(sp.close())


async def after_reply_check():
    """A tool's held action (a song starting) runs when the device goes idle after its reply, not before."""
    dev, ran = brain.device("held"), []

    async def act():
        ran.append("played")

    await dev.set_state("thinking")
    await brain.after_reply("held")(act)
    await dev.set_state("speaking")
    await asyncio.sleep(0.05)
    assert ran == [], "still talking"
    await dev.set_state("idle")
    await asyncio.sleep(0.05)
    assert ran == ["played"] and dev.after_reply == []
    await dev.set_state("idle")
    await asyncio.sleep(0.05)
    assert ran == ["played"], "only once"


def main():
    asyncio.run(after_reply_check())
    server, port = start()
    base = f"http://127.0.0.1:{port}"

    # Static display page, and / redirects to it
    r = httpx.get(f"{base}/display/")
    assert r.status_code == 200 and "Claude Home" in r.text, r.status_code
    for path in ("", "style.css", "app.js"):  # checked on every load, so new code never mixes with an old stylesheet
        assert httpx.get(f"{base}/display/{path}").headers["cache-control"] == "no-cache", path
    r = httpx.get(f"{base}/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/display/", (r.status_code, r.headers)

    # Photos: the list, a screen-sized JPEG, and nothing outside the folder
    os.makedirs(PICS)
    Image.new("RGB", (1200, 900), "teal").save(os.path.join(PICS, "beach day.jpg"))
    open(os.path.join(TMP, "secret.jpg"), "wb").write(b"not yours")
    assert httpx.get(f"{base}/photos").json() == ["beach day.jpg"]
    r = httpx.get(f"{base}/photos/beach%20day.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", r.status_code
    assert Image.open(io.BytesIO(r.content)).size == (800, 480)
    for bad in ("..%2Fsecret.jpg", "missing.jpg", "%2E%2E%2Fsecret.jpg"):
        assert httpx.get(f"{base}/photos/{bad}").status_code == 404, bad

    # Spotify login: needs a client id; sends the browser to Spotify with PKCE; a stray callback is refused
    os.environ.pop("SPOTIFY_CLIENT_ID", None)
    assert httpx.get(f"{base}/spotify/login").status_code == 503
    os.environ["SPOTIFY_CLIENT_ID"] = "test-client"
    r = httpx.get(f"{base}/spotify/login", follow_redirects=False)
    to = urllib.parse.urlparse(r.headers["location"])
    sent = urllib.parse.parse_qs(to.query)
    assert r.status_code == 307 and to.netloc == "accounts.spotify.com" and sent["client_id"] == ["test-client"], r.headers
    assert sent["state"][0] in brain.spotify_logins and sent["code_challenge_method"] == ["S256"]
    assert httpx.get(f"{base}/spotify/callback", params={"state": "made-up", "code": "x"}).status_code == 400
    assert httpx.get(f"{base}/spotify/callback", params={"state": sent["state"][0], "error": "access_denied"}).status_code == 400
    assert sent["state"][0] not in brain.spotify_logins, "a used login is forgotten"
    del os.environ["SPOTIFY_CLIENT_ID"]

    # A text turn: audio_start, speech audio, audio_end with follow_up
    got = asyncio.run(satellite_turn(port, "hello there"))
    assert got[0]["type"] == "audio_start" and got[0]["sample_rate"] > 0, got[0]
    assert any(isinstance(m, bytes) and len(m) > 1000 for m in got), "no speech audio"
    assert got[-1] == {"type": "audio_end", "follow_up": False}, got[-1]

    # A reply ending in a question asks the satellite to listen for the answer
    got = asyncio.run(satellite_turn(port, "ask me a question"))
    assert got[-1] == {"type": "audio_end", "follow_up": True}, got[-1]

    # "Never mind" is skipped without a reply
    got = asyncio.run(satellite_turn(port, "never mind"))
    assert got == [{"type": "audio_end", "follow_up": False}], got

    # A satellite that vanishes mid-reply is cleaned up quietly, and the next one still works
    asyncio.run(abrupt_disconnect(port))
    deadline = time.time() + 15
    while brain.devices["rude"].sat is not None:
        assert time.time() < deadline, "vanished satellite never cleaned up"
        time.sleep(0.1)
    got = asyncio.run(satellite_turn(port, "hello again"))
    assert got[-1]["type"] == "audio_end"
    # Displays: the device hub, ordering, isolation between devices, and turns started from a screen
    asyncio.run(display_flow(port))
    asyncio.run(music_flow(port))
    asyncio.run(alarm_flow(port))
    assert not errors.records, [r.getMessage() for r in errors.records]

    server.should_exit = True
    print("ok")


if __name__ == "__main__":
    main()
