"""Server tests: the FastAPI app on 127.0.0.1 with the fake LLM and a temp database.

Run: uv run python test_server.py
"""
import asyncio
import json
import logging
import os
import socket
import tempfile
import threading
import time

TMP = tempfile.mkdtemp()
os.environ.update(CLAUDE_HOME_FAKE_LLM="1", CLAUDE_HOME_DB=os.path.join(TMP, "test.db"),
                  CLAUDE_HOME_LOG=os.path.join(TMP, "turns.jsonl"))  # before importing the brain

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

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
    for ws in (a, b, other):
        await ws.close()


def main():
    server, port = start()
    base = f"http://127.0.0.1:{port}"

    # Static display page, and / redirects to it
    r = httpx.get(f"{base}/display/")
    assert r.status_code == 200 and "Claude Home" in r.text, r.status_code
    r = httpx.get(f"{base}/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/display/", (r.status_code, r.headers)

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
    assert not errors.records, [r.getMessage() for r in errors.records]

    server.should_exit = True
    print("ok")


if __name__ == "__main__":
    main()
