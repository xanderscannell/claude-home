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

TMP = tempfile.mkdtemp()
PICS = os.path.join(TMP, "photos")
os.environ.update(CLAUDE_HOME_FAKE_LLM="1", CLAUDE_HOME_DB=os.path.join(TMP, "test.db"),
                  CLAUDE_HOME_LOG=os.path.join(TMP, "turns.jsonl"), PHOTOS_DIR=PICS)  # before importing the brain

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402
from PIL import Image  # noqa: E402

import brain  # noqa: E402


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
        m = await until_type(a, "card")
        assert m["card"]["kind"] == "timers" and [t["label"] for t in m["card"]["timers"]] == ["eggs"], m
        assert (await until_type(b, "card"))["card"]["kind"] == "timers", "every kitchen screen gets the card"
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

        # Cancel from the screen by id; screens hear about it through the timer watcher
        eggs = brain.tools.timer_list("kitchen")[0]["id"]
        await a.send(json.dumps({"type": "timer_cancel", "id": eggs}))
        m = await until_type(a, "ambient")
        while m["timers"]:
            m = await until_type(a, "ambient")
        assert brain.tools.timer_list("kitchen") == []

        # Shopping: Claude's tools show the list; checking an item off on one screen updates just that screen
        await on_server(kitchen_tools["add_to_shopping_list"].handler({"items": ["Milk", "eggs", "Bread"]}))
        assert await until_card(a, "shopping") == {"kind": "shopping", "items": ["Milk", "eggs", "Bread"]}
        await until_card(b, "shopping")
        await a.send(json.dumps({"type": "shopping_remove", "item": "EGGS"}))
        assert (await until_card(a, "shopping"))["items"] == ["Milk", "Bread"]
        assert brain.tools.shopping_items() == ["Milk", "Bread"]
        await b.send(json.dumps({"type": "open", "sheet": "shopping"}))
        assert (await until_card(b, "shopping"))["items"] == ["Milk", "Bread"], "b sees the change when it opens"
        brain.tools.shopping_remove(["Milk", "Bread"])
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
        card = await until_card(screen, "timers")
        assert [(a["id"], a["enabled"]) for a in card["alarms"]] == [(later, False)], card
        await screen.send(json.dumps({"type": "alarm_toggle", "id": later, "enabled": True}))
        assert (await until_card(screen, "timers"))["alarms"][0]["enabled"] is True
        m = await until_type(screen, "ambient")
        while not m["next_alarm"]:
            m = await until_type(screen, "ambient")
        assert m["next_alarm"]["id"] == later and m["next_alarm"]["time"] == "21:15", m["next_alarm"]
        await screen.send(json.dumps({"type": "alarm_delete", "id": later}))
        assert (await until_card(screen, "timers"))["alarms"] == []

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


def main():
    server, port = start()
    base = f"http://127.0.0.1:{port}"

    # Static display page, and / redirects to it
    r = httpx.get(f"{base}/display/")
    assert r.status_code == 200 and "Claude Home" in r.text, r.status_code
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
    asyncio.run(alarm_flow(port))
    assert not errors.records, [r.getMessage() for r in errors.records]

    server.should_exit = True
    print("ok")


if __name__ == "__main__":
    main()
