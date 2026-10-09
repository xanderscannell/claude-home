"""Claude Home brain: web server for satellites and displays. Speech -> Groq STT -> Claude (Agent SDK) -> Piper -> speech.

Claude's reply is spoken sentence by sentence while it is still being generated.
Every turn's timings are appended to turns.jsonl (CLAUDE_HOME_LOG overrides the path), and every Home Assistant
action Claude takes to ha_actions.jsonl beside it.

Run from brain/:  uv run --env-file ../.env brain.py
Optional env: BRAIN_HOST (default localhost; set 0.0.0.0 to accept satellites from the LAN), BRAIN_PORT (8765),
              HOME_LOCATION (city name or "lat,lon", for weather), CLAUDE_HOME_DB (default brain/home.db),
              NIGHT_START / NIGHT_END (display night mode, default 22:00 / 07:00),
              CALENDAR_<LABEL> (one secret iCal address per calendar, e.g. CALENDAR_WORK), PHOTOS_DIR,
              SPOTIFY_CLIENT_ID / SPOTIFY_DEVICE (see spotify.py),
              HA_URL / HA_TOKEN (Home Assistant's MCP Server and a long-lived access token).

Displays connect to /display?device=<name> and follow that device's conversation (see docs/architecture.md).

Protocol (see docs/architecture.md): satellite sends hello, wake, binary 16 kHz int16 mono mic audio,
then speech_end (or cancel), or {type: text, text} to skip speech-to-text. Brain replies
audio_start {sample_rate}, binary int16 speech, audio_end {follow_up}. Unprompted speech (a finished timer)
comes as the same messages with announce: true.
"""

import asyncio
import contextlib
import io
import json
import math
import os
import re
import secrets
import time
import wave
from array import array
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
import websockets
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage, StreamEvent
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from piper import PiperVoice

import agenda
import photos
import spotify
import todoist
import tools

RATE = 16000
HERE = Path(__file__).parent
VOICE = HERE / "voices" / "en_GB-northern_english_male-medium.onnx"
LOG = Path(os.environ.get("CLAUDE_HOME_LOG", HERE / "turns.jsonl"))
HA_LOG = LOG.with_name("ha_actions.jsonl")

SYSTEM_PROMPT = """You are Claude Home, a voice assistant on a smart speaker. Everything you write is spoken aloud.
- Answer in one to three short sentences unless asked for more.
- Start with a short sentence that answers directly, then add detail. The first sentence is spoken while you write the rest.
- No markdown, lists, emoji, or URLs. Say numbers, times, and units the way a person would speak them.
- If the request is ambiguous, ask one short clarifying question.
- Search the web only when you need current information, at most once per question, then answer.
- After a web search, never list sources or links; the listener cannot see them.
- Each user message starts with the current local time in brackets; use it when relevant, never read it back unprompted.
- Use your tools for timers, alarms, the shopping list, weather, the calendar, a morning briefing, recipes, music, the speaker's volume, and remembering things. Confirm what you did in a few words.
- Home Assistant controls the house's lights, switches, climate, TVs, and other speakers. Music requests use your own music tools unless a TV or other speaker is named.
- When you end with a question, the speaker listens for the answer without the wake word, so only ask when you need to."""

OPTIONS = ClaudeAgentOptions(
    system_prompt=SYSTEM_PROMPT,
    model="claude-haiku-5-5",  # pinned: the "haiku" alias in this SDK's Claude Code still means 4.5
    setting_sources=[],  # ignore ~/.claude and project config (CLAUDE.md, hooks, plugins)
    tools=["WebSearch"],  # no shell or file tools on this machine
    allowed_tools=["WebSearch", "mcp__home__*", "mcp__ha__*"],
    # Home Assistant tools the brain already does itself: timers, the (Todoist) shopping list, the time, announcements.
    disallowed_tools=[f"mcp__ha__{t}" for t in ("intent__HassCancelAllTimers", "todo__get_items", "todo__HassListAddItem",
                                                 "todo__HassListCompleteItem", "todo__HassListRemoveItem",
                                                 "llm__GetDateTime", "assist_satellite__HassBroadcast")],
    permission_mode="dontAsk",
    include_partial_messages=True,  # text deltas, for speaking sentence by sentence
    # On a subscription login, claude.ai connectors (Gmail, Drive, ...) load into every session.
    # Block them both ways; setting_sources=[] alone does not.
    env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    extra_args={"strict-mcp-config": None},
)

MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
SOURCES = re.compile(r"^\W*sources?\s*:", re.I)

# ponytail: punctuation + whitespace (or newline) ends a sentence, except after a known abbreviation.
# Unlisted abbreviations still split early: a short pause, not broken speech. Add them to ABBREV as heard.
# A real sentence ending in "etc." or "D.C." merges with the next one: slightly later audio, nothing lost.
SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s+|\n+")
ABBREV = re.compile(r"(?:\b(?:Dr|Mr|Mrs|Ms|Jr|Sr|St|Mt|vs|etc|approx|e\.g|i\.e)|\b[A-HJ-Z])\.\s+$")


# Piper reads these literally ("Jr." -> "jay are") and treats their period as a sentence end (a pause).
SPOKEN = {"Dr.": "Doctor", "Mr.": "Mister", "Mrs.": "Missus", "Ms.": "Miz", "Jr.": "Junior", "Sr.": "Senior",
          "Mt.": "Mount", "vs.": "versus", "etc.": "et cetera", "e.g.": "for example", "i.e.": "that is"}
SPOKEN_RE = re.compile(r"(?<!\w)(" + "|".join(map(re.escape, SPOKEN)) + r")(?!\w)")


def for_speech(s: str) -> str:
    return SPOKEN_RE.sub(lambda m: SPOKEN[m.group(1)], s)


def pop_sentences(buf: str) -> tuple[list[str], str]:
    """Split complete sentences off the front of buf. Returns (sentences, remainder)."""
    out, start = [], 0
    for m in SENTENCE_END.finditer(buf):
        if ABBREV.search(buf, 0, m.end()):
            continue
        if s := buf[start:m.end()].strip():
            out.append(s)
        start = m.end()
    return out, buf[start:]


def transcribe(groq: httpx.Client, pcm: bytes) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    r = groq.post("/audio/transcriptions",
                  files={"file": ("speech.wav", buf.getvalue(), "audio/wav")},
                  data={"model": "whisper-large-v3-turbo", "language": "en", "response_format": "json"})
    r.raise_for_status()
    return r.json()["text"].strip()


async def ask(client: ClaudeSDKClient, text: str):
    """Yield the reply one sentence at a time as Claude streams it. If the turn fails (a usage limit, an overload),
    say so: the failure arrives only in the result, and without this the speaker went silent (2026-10-08 15:40)."""
    await client.query(f"[{datetime.now():%A %B %d, %I:%M %p}] {text}")
    buf, muted, spoke = "", False, False
    async for msg in client.receive_response():
        if isinstance(msg, ResultMessage) and msg.is_error:
            print(f"  claude failed: {msg.subtype} {msg.api_error_status or ''} {msg.result or msg.errors or ''}")
            if not spoke:
                yield ("Claude is rate limited right now. Try again in a little while." if msg.api_error_status == 429
                       else "Sorry, I couldn't get an answer from Claude just then.")
            continue
        if not isinstance(msg, StreamEvent):
            continue
        ev = msg.event
        if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
            sentences, buf = pop_sentences(buf + ev["delta"]["text"])
        elif ev.get("type") == "content_block_start" and ev["content_block"].get("type") in ("tool_use", "server_tool_use"):
            print(f"  tool: {ev['content_block']['name'].removeprefix('mcp__home__').removeprefix('mcp__ha__')}")
            continue
        elif ev.get("type") == "content_block_stop":
            sentences, buf = [buf], ""  # end of a text block (e.g. before a web search) flushes the tail
        else:
            continue
        for s in sentences:
            # The WebSearch tool insists on a trailing "Sources:" list of links; never speak it.
            muted = muted or bool(SOURCES.match(s))
            if not muted and (s := MD_LINK.sub(r"\1", s).strip()):
                spoke = True
                yield s
    if buf.strip() and not muted:
        yield MD_LINK.sub(r"\1", buf).strip()


# Whisper's usual output for silence or noise, and ways of calling off a request.
IGNORE = {"", "you", "thank you", "thanks for watching", "bye", "never mind", "nevermind", "cancel", "stop",
          "nothing", "forget it"}

# Tests set CLAUDE_HOME_FAKE_LLM=1: canned replies instead of Claude, so they are fast, free, and repeatable.
FAKE_LLM = os.environ.get("CLAUDE_HOME_FAKE_LLM") == "1"


async def fake_ask(client, text: str):
    """Stand-in for ask(): two sentences, ending with a question if the request mentions one.
    "long" gives five slower sentences, so tests can stop a reply partway through."""
    if "long" in text.lower():
        for i in range(1, 6):
            await asyncio.sleep(0.3)
            yield f"This is sentence {i} of a long reply."
        return
    yield "This is a canned reply."
    yield "Did you want anything else?" if "question" in text.lower() else "That is all."


def options(device: str, categories: list[str] = ()) -> ClaudeAgentOptions:
    """Per-satellite session: its own timer tools, the shopping list's categories, saved facts baked into
    the prompt, and Home Assistant when HA_URL is set."""
    known = tools.facts()
    memory = "\n\nThings you remember about this household:\n" + "\n".join(f"- {f}" for f in known) if known else ""
    servers = {"home": tools.make_tools(device, on_card=show_card(device), on_volume=speaker(device),
                                        music=app.state.spotify, categories=categories, after_reply=after_reply(device),
                                        cameras=app.state.cameras, on_camera=show_camera(device))}
    if os.environ.get("HA_URL"):
        servers["ha"] = {"type": "http", "url": os.environ["HA_URL"].rstrip("/") + "/api/mcp",
                         "headers": {"Authorization": f"Bearer {os.environ.get('HA_TOKEN', '')}"}}
    logged = [HookMatcher(matcher="mcp__ha__.*", hooks=[log_ha(device)])]
    return replace(OPTIONS, system_prompt=SYSTEM_PROMPT + memory, mcp_servers=servers,
                   hooks={"PostToolUse": logged, "PostToolUseFailure": logged})


def log_ha(name: str):
    """Hook for one device's session: append each Home Assistant action Claude takes (not its state lookups) to
    HA_LOG with what was heard, so a surprise in the house traces back to a mishearing or to Claude's choice."""
    async def hook(data, tool_use_id, context):
        tool = data["tool_name"].removeprefix("mcp__ha__")
        if not tool.endswith("GetLiveContext"):
            entry = {"ts": datetime.now().isoformat(timespec="seconds"), "device": name, "heard": device(name).heard,
                     "tool": tool, "args": data["tool_input"], "result": data.get("tool_response", data.get("error"))}
            with HA_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        return {}
    return hook


def after_reply(name: str):
    """after_reply for one device's tools: hold an action until the device goes idle, i.e. its reply has played."""
    async def hold(action):
        device(name).after_reply.append(action)
    return hold


def show_camera(name: str):
    """on_camera for one device's tools: a camera's live view goes up on that device's screens (None: closes)."""
    async def on_camera(cam: dict | None):
        await device(name).show({"type": "camera", "entity": cam["entity"], "name": cam["name"]} if cam
                                else {"type": "camera", "entity": None})
    return on_camera


def show_card(name: str):
    """on_card for one device's tools: a tool's result goes up on that device's screens."""
    async def on_card(card: dict):
        await device(name).show({"type": "card", "card": card})
    return on_card


def speaker(name: str):
    """on_volume for one device's tools: set its speaker's level (or change it by some amount) and show it."""
    async def on_volume(level: int, relative: bool) -> int | None:
        dev = device(name)
        if dev.sat is None or (relative and dev.volume is None):
            return None
        await set_volume(dev, dev.volume + level if relative else level)
        await dev.show({"type": "card", "card": {"kind": "volume", "level": dev.volume}})
        return dev.volume
    return on_volume


async def set_volume(dev: "Device", level: float):
    """Send a new level (clamped to 0-100) to the satellite and the screens; the satellite then reports
    what it actually set."""
    if dev.sat is None:
        return
    dev.volume = max(0, min(100, int(level)))
    await send(dev.sat, json.dumps({"type": "volume", "level": dev.volume}))
    await dev.show({"type": "volume", "level": dev.volume})


def session(device: str, categories: list[str]):
    """One Claude conversation per satellite connection (nothing to start in fake mode)."""
    return contextlib.nullcontext() if FAKE_LLM else ClaudeSDKClient(options=options(device, categories))


def beeps(rate: int) -> bytes:
    """Three short beeps for a finished timer."""
    n, fade = int(rate * 0.15), rate * 0.01
    tone = [int(8000 * math.sin(2 * math.pi * 988 * i / rate) * min(1, i / fade, (n - i) / fade)) for i in range(n)]
    return array("h", (tone + [0] * int(rate * 0.1)) * 3).tobytes()


async def send(ws: WebSocket, data: str | bytes):
    await (ws.send_bytes(data) if isinstance(data, bytes) else ws.send_text(data))


class Device:
    """Everything the brain knows about one device id: its satellite, its screens, and its conversation."""

    def __init__(self, name: str):
        self.name = name
        self.sat: WebSocket | None = None  # the satellite connection, if one is connected
        self.client = None  # its Claude session (None in fake mode or with no satellite)
        self.lock = asyncio.Lock()  # one reply or announcement at a time
        self.busy = False  # a turn is running
        self.stopped = False  # Stop was tapped during this turn
        self.volume: int | None = None  # the speaker's level, as last reported by the satellite
        self.ringing: dict | None = None  # the alarm or timer ringing right now, as shown on screens
        self.ring_task: asyncio.Task | None = None  # what rings it; still running for a moment after a stop
        self.timers_waiting: list[tuple[float, str, float | None]] = []  # finished timers waiting to ring
        self.snoozed = False
        self.displays: set[WebSocket] = set()
        self.state = "idle"  # idle | listening | thinking | speaking
        self.after_reply: list = []  # async actions held until the reply has played (music starting, say)
        self.heard = ""  # what was said in the latest turn, for the Home Assistant action log

    async def show(self, msg: dict):
        """Send to every display of this device; drop displays that have gone away or stopped reading.
        The timeout matters: a hung screen would otherwise stall whoever is showing (a reply, the timer loop)."""
        text = json.dumps(msg)
        for d in list(self.displays):
            try:
                await asyncio.wait_for(d.send_text(text), 2)
            except (*GONE, TimeoutError):
                self.displays.discard(d)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(d.close(), 1)  # it reconnects and gets a fresh snapshot

    async def set_state(self, state: str):
        if state != self.state:
            self.state = state
            await self.show({"type": "state", "state": state})
        if state == "idle":
            held, self.after_reply = self.after_reply, []
            for action in held:
                spawn(action())


devices: dict[str, Device] = {}
background = set()  # references to fire-and-forget tasks so they aren't garbage collected
GONE = (WebSocketDisconnect, RuntimeError)  # what sending to or reading from a vanished client raises


def device(name: str) -> Device:
    return devices.setdefault(name, Device(name))


def spawn(coro):
    task = asyncio.create_task(coro)
    background.add(task)
    task.add_done_callback(background.discard)
    return task


async def announce(dev: Device, text: str | None):
    """Beeps, then text spoken (none: just the beeps), without being asked: a finished timer, an alarm.
    The lock keeps it from interleaving with a reply."""
    voice = app.state.voice
    rate = voice.config.sample_rate
    audio = await asyncio.to_thread(lambda: [c.audio_int16_bytes for c in voice.synthesize(for_speech(text))]) if text else []
    async with dev.lock:
        if dev.sat is None:
            return
        await send(dev.sat, json.dumps({"type": "audio_start", "sample_rate": rate, "announce": True}))
        for chunk in [beeps(rate), *audio]:
            await send(dev.sat, chunk)
        await send(dev.sat, json.dumps({"type": "audio_end", "announce": True}))


async def turn(dev: Device, pcm: bytes = b"", text: str | None = None):
    """One request: speech (pcm) or typed text in, Claude's spoken reply out, with the screens following along."""
    dev.busy, dev.stopped = True, False
    try:
        await _turn(dev, pcm, text)
    finally:
        dev.busy = dev.stopped = False


async def quietly(coro):
    with contextlib.suppress(Exception):
        await coro


RING_LIMIT_S = 600  # an alarm or timer rings at most this long
BEEP_EVERY_S = 4  # between bursts of beeps while it rings
SNOOZE_MIN = 9
clock = time.time  # alarm timing reads this, so tests can steer it
# Said while something rings, no wake word needed (the satellite listens for speech meanwhile)
STOP_WORDS = re.compile(r"\b(stop|cancel|dismiss|enough|quiet|shut up|off)\b", re.I)
SNOOZE_WORD = re.compile(r"\bsnooze\b", re.I)


async def set_ringing(dev: Device, ringing: dict | None):
    """What rings on this device now: its screens show it, and its satellite listens for "stop" meanwhile."""
    dev.ringing = ringing
    await dev.show({"type": "ringing", "ringing": ringing})
    if dev.sat is not None:
        await quietly(send(dev.sat, json.dumps({"type": "ringing", "on": ringing is not None})))


async def ring(dev: Device, ringing: dict, text: str):
    """Ring until stopped (a tap, or "stop" said), snoozed, answered (wake word or tap-to-talk), or RING_LIMIT_S
    runs out."""
    dev.snoozed = False
    await set_ringing(dev, ringing)
    start = clock()
    try:
        await announce(dev, text)
        while dev.ringing is ringing and clock() - start < RING_LIMIT_S:
            await asyncio.sleep(BEEP_EVERY_S)
            if dev.ringing is ringing:
                await announce(dev, None)
    finally:
        if dev.ringing is ringing:  # ran out on its own
            await set_ringing(dev, None)


async def ring_alarm(dev: Device, alarm: dict):
    print(f"[{dev.name}] alarm ringing: {alarm['time']} {alarm['label']}")
    try:
        await ring(dev, {"kind": "alarm", "id": alarm["id"], "label": alarm["label"], "time": alarm["time"]},
                   f"It's {tools.spoken_time(alarm['time'])}." + (f" {alarm['label']}." if alarm["label"] else ""))
    finally:
        if not dev.snoozed:
            tools.alarm_done(alarm["id"])


async def ring_timer(dev: Device, label: str, duration: float | None):
    name = tools.timer_name({"label": label, "duration": duration})
    print(f"[{dev.name}] timer done: {name}")
    text = f"Your {label} timer is done." if label else f"Your {name} is done."
    await ring(dev, {"kind": "timer", "label": label, "duration": duration}, text)


async def stop_ringing(dev: Device, snooze: bool = False) -> bool:
    """End what's ringing now (snoozing it, if it's an alarm and snooze is asked). Returns whether anything rang."""
    if dev.ringing is None:
        return False
    if snooze and dev.ringing["kind"] == "alarm":
        tools.alarm_snooze(dev.ringing["id"], SNOOZE_MIN, clock())
        dev.snoozed = True
    await set_ringing(dev, None)
    if dev.sat is not None:
        await send(dev.sat, json.dumps({"type": "stop"}))  # drop the beeps still queued
    return True


async def heard_while_ringing(dev: Device, pcm: bytes):
    """Speech the satellite heard while something rang, without the wake word: "stop" (or the like) stops it,
    "snooze" snoozes an alarm, and anything else is ignored."""
    if dev.ringing is None:
        return
    try:
        text = await asyncio.to_thread(transcribe, app.state.groq, pcm)
    except httpx.HTTPError as e:
        print(f"[{dev.name}] couldn't transcribe speech while ringing: {e!r}")
        return
    print(f"[{dev.name}] heard while ringing: {text!r}")
    if SNOOZE_WORD.search(text):
        await stop_ringing(dev, snooze=True)
    elif STOP_WORDS.search(text):
        await stop_ringing(dev)


async def stop(dev: Device):
    """Stop on the screen: silence a ringing alarm, cut the reply short now, or drop whatever the
    satellite still has queued."""
    if await stop_ringing(dev):
        return
    if dev.busy:
        dev.stopped = True  # the running turn stops speaking at its next sentence
        if dev.client is not None:
            # Claude stops writing. Spawned: awaiting it here held up this screen's next message for ~8 s.
            spawn(quietly(dev.client.interrupt()))
    elif dev.sat is not None:
        await send(dev.sat, json.dumps({"type": "stop"}))


async def _turn(dev: Device, pcm: bytes, text: str | None):
    voice, groq = app.state.voice, app.state.groq
    await dev.set_state("thinking")
    t0 = time.perf_counter()  # speech_end received (the satellite's VAD silence tail came just before)
    if text is None:
        text = await asyncio.to_thread(transcribe, groq, pcm) if len(pcm) >= RATE else ""  # >= 0.5 s of audio
    t_stt = time.perf_counter()
    async with dev.lock:
        sat = dev.sat
        if text.lower().strip(" .!?,") in IGNORE or sat is None or dev.stopped:
            print(f"[{dev.name}] (heard {text!r}, skipped)" if sat else f"[{dev.name}] no satellite connected")
            if sat:
                await send(sat, json.dumps({"type": "audio_end", "follow_up": False}))
            await dev.set_state("idle")
            return
        print(f"[{dev.name}] you:    {text}")
        dev.heard = text
        await dev.show({"type": "heard", "text": text})

        await send(sat, json.dumps({"type": "audio_start", "sample_rate": voice.config.sample_rate}))
        reply, t_first, t_audio = [], None, None
        async for s in (fake_ask if FAKE_LLM else ask)(dev.client, text):
            if dev.stopped:
                continue  # keep draining Claude's stream so nothing leaks into the next turn, but say no more
            t_first = t_first or time.perf_counter()
            reply.append(s)
            audio = await asyncio.to_thread(lambda: [c.audio_int16_bytes for c in voice.synthesize(for_speech(s))])
            await dev.set_state("speaking")
            await dev.show({"type": "reply", "text": s})
            for chunk in audio:
                await send(sat, chunk)
            t_audio = t_audio or time.perf_counter()
        if dev.stopped:
            await send(sat, json.dumps({"type": "stop"}))  # drop what the satellite has queued
        follow_up = bool(reply) and reply[-1].endswith("?") and not dev.stopped
        await send(sat, json.dumps({"type": "audio_end", "follow_up": follow_up}))
        if not reply or dev.stopped:
            await dev.set_state("idle")  # nothing (more) to play, so don't wait for "played"
    print(f"[{dev.name}] claude: {' '.join(reply)}" + (" (stopped)" if dev.stopped else ""))

    log = {"ts": datetime.now().isoformat(timespec="seconds"), "device": dev.name, "you": text,
           "claude": " ".join(reply), "audio_s": round(len(pcm) / 2 / RATE, 2),
           "stt": t_stt - t0,
           "claude_first_sentence": (t_first or t_stt) - t_stt,
           "tts_first_audio": (t_audio or t_first or t_stt) - (t_first or t_stt),
           "end_of_speech_to_audio": (t_audio or t_stt) - t0}
    log = {k: round(v, 2) if isinstance(v, float) else v for k, v in log.items()}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(log) + "\n")
    print(f"[{dev.name}] timing: stt {log['stt']}s | claude first sentence {log['claude_first_sentence']}s"
          f" | tts {log['tts_first_audio']}s | end of speech to audio {log['end_of_speech_to_audio']}s")


# --- Server --------------------------------------------------------------------------------------

weather_now: dict | None = None  # home weather for the screens, refreshed in the background


def ambient(dev: Device) -> dict:
    """What the home screen shows besides the clock."""
    return {"type": "ambient", "weather": weather_now, "timers": tools.timer_list(dev.name),
            "next_alarm": tools.next_alarm(dev.name), "agenda": agenda.today()}


async def refresh_agenda():
    """The calendars every 15 minutes, in the background (big feeds take minutes to fetch), or after 1 minute
    when one failed (a fresh start has no last copy to fall back on). Needs CALENDAR_*."""
    while agenda.feeds():
        failed = await agenda.refresh()
        for dev in list(devices.values()):
            await dev.show(ambient(dev))
        await asyncio.sleep(60 if failed else agenda.REFRESH_S)


async def refresh_weather():
    """Home weather every 15 minutes (2 after a failure). Needs HOME_LOCATION; screens get it as it arrives."""
    global weather_now
    place = os.environ.get("HOME_LOCATION")
    while place:
        try:
            weather_now = await tools.weather_data(place)
        except (httpx.HTTPError, LookupError, KeyError, ValueError) as e:
            print(f"weather refresh failed ({type(e).__name__}); retrying in 2 minutes")
            await asyncio.sleep(120)
            continue
        for dev in list(devices.values()):
            await dev.show(ambient(dev))
        await asyncio.sleep(900)


music_now: dict | None = None  # what the screens show as now playing (see spotify.track)
music_at = 0.0  # when music_now was sent; its progress_ms is as of then
music_error = ""  # the last Spotify problem logged, so the poller doesn't repeat it every 20 s


def music_snapshot() -> dict | None:
    """music_now with its position brought up to date, for a screen that connects mid-song."""
    if not music_now or not music_now["is_playing"]:
        return music_now
    ahead = int((time.time() - music_at) * 1000)
    return {**music_now, "progress_ms": min(music_now["duration_ms"], music_now["progress_ms"] + ahead)}


async def check_music(sp: spotify.Spotify) -> float:
    """Look at Spotify once and tell every screen if what's playing changed. Returns seconds until the next look:
    5 while playing, 20 otherwise, longer while Spotify has asked us to back off."""
    global music_now, music_at, music_error
    try:
        new = spotify.track(await sp.playback())
    except spotify.SpotifyError as e:
        if str(e) != music_error:
            music_error = str(e)
            print(f"spotify: {e}")
        return max(20.0, sp.blocked_until - sp.clock())
    music_error = ""
    if spotify.moved(music_now, new, time.time() - music_at):
        music_now, music_at = new, time.time()
        for dev in list(devices.values()):
            await dev.show({"type": "now_playing", "track": new})
    return 5.0 if new and new["is_playing"] else 20.0


async def poll_music():
    sp = app.state.spotify
    while True:
        delay = await check_music(sp)
        try:
            await asyncio.wait_for(sp.changed.wait(), delay)
            sp.changed.clear()
            await asyncio.sleep(1)  # a control just happened; Spotify takes a moment to reflect it
        except TimeoutError:
            pass


async def ring_timers():
    """Timers and alarms live in SQLite, so they survive restarts. Overdue timers ring once a satellite
    connects; an alarm more than RING_LIMIT_S late (the brain was down) is skipped rather than ringing late.
    A device rings one thing at a time, and not over a conversation: the rest wait their turn, oldest first.
    Also keeps screens' timers and next alarm current, whatever changed them (Claude, a finish, a tap)."""
    started = clock()
    shown: dict[str, tuple] = {}
    while True:
        await asyncio.sleep(0.5)
        for dev in list(devices.values()):
            if not dev.displays:
                continue
            now = (tools.timer_list(dev.name), tools.next_alarm(dev.name))
            if now != shown.get(dev.name, ([], None)):
                shown[dev.name] = now
                await dev.show(ambient(dev))
        alarms: dict[str, list[dict]] = {}  # alarms stay due until rung, so they wait in SQLite
        for alarm in tools.alarms_due(clock()):
            if alarm["next_ring"] < started - RING_LIMIT_S:
                print(f"[{alarm['device']}] skipped an alarm that was due while the brain was down: {alarm['time']}")
                tools.alarm_done(alarm["id"])
                continue
            device(alarm["device"])  # so the loop below finds it
            alarms.setdefault(alarm["device"], []).append(alarm)
        if connected := [d for d in devices.values() if d.sat is not None]:
            for name, label, duration in tools.timers_due():
                dev = devices.get(name) if name in devices and devices[name].sat else connected[0]
                dev.timers_waiting.append((clock(), label, duration))
        for dev in list(devices.values()):
            # The ring task, not dev.ringing: a stopped alarm stays due until its task has finished with it
            if (dev.ring_task and not dev.ring_task.done()) or dev.busy or dev.state == "listening":
                continue
            alarm = min(alarms.get(dev.name, []), key=lambda a: a["next_ring"], default=None)
            if dev.timers_waiting and (alarm is None or dev.timers_waiting[0][0] < alarm["next_ring"]):
                _, label, duration = dev.timers_waiting.pop(0)
                dev.ring_task = spawn(ring_timer(dev, label, duration))
            elif alarm:
                dev.ring_task = spawn(ring_alarm(dev, alarm))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.loop = asyncio.get_running_loop()  # tests hand tool calls to this loop from their own thread
    app.state.voice = PiperVoice.load(VOICE)
    app.state.groq = httpx.Client(base_url="https://api.groq.com/openai/v1", timeout=30,
                                  headers={"Authorization": f"Bearer {os.environ.get('GROQ_KEY', '')}"},
                                  limits=httpx.Limits(keepalive_expiry=60))  # httpx default drops idle connections after 5 s
    if not FAKE_LLM:
        app.state.groq.get("/models").raise_for_status()  # fail fast on a bad key
    app.state.spotify = spotify.Spotify(os.environ.get("SPOTIFY_CLIENT_ID", ""))
    app.state.cameras = await ha_cameras()  # ponytail: read once; a camera added in Home Assistant needs a restart
    loops = [asyncio.create_task(ring_timers()), asyncio.create_task(refresh_weather()),
             asyncio.create_task(refresh_agenda())]
    if os.environ.get("SPOTIFY_CLIENT_ID"):
        loops.append(asyncio.create_task(poll_music()))
    yield
    for task in loops:
        task.cancel()
    app.state.groq.close()
    await app.state.spotify.close()


app = FastAPI(lifespan=lifespan)


@app.websocket("/satellite")
async def satellite(ws: WebSocket):
    await ws.accept()
    dev = device(ws.query_params.get("device", "unknown"))
    dev.sat = ws
    print(f"[{dev.name}] connected")
    if dev.ringing is not None:  # back mid-ring: listen for "stop" again
        await quietly(send(ws, json.dumps({"type": "ringing", "on": True})))
    try:
        async with session(dev.name, await tools.shopping_categories()) as client:
            dev.client = client
            await serve_satellite(ws, dev)
    except GONE:
        pass  # satellite went away without a clean close (killed, network drop)
    finally:
        if dev.sat is ws:
            dev.sat, dev.client = None, None
            await dev.set_state("idle")
    print(f"[{dev.name}] disconnected")


async def serve_satellite(ws: WebSocket, dev: Device):
    frames = []
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            return
        if msg.get("bytes") is not None:
            frames.append(msg["bytes"])
            continue
        m = json.loads(msg["text"])
        kind = m.get("type")
        if kind == "wake":
            frames = []
            await stop_ringing(dev)  # "Hey Jarvis" over a ringing alarm answers it
            await dev.set_state("listening")
            # Reopen the Groq connection while the user talks, so STT never pays for a TLS handshake.
            if not FAKE_LLM:
                spawn(asyncio.to_thread(app.state.groq.get, "/models"))
        elif kind == "cancel":
            frames = []
            await dev.set_state("idle")
        elif kind == "speech_end":
            await turn(dev, pcm=b"".join(frames))
            frames = []
        elif kind == "ring_speech":  # the audio just sent is speech heard while ringing
            spawn(heard_while_ringing(dev, b"".join(frames)))
            frames = []
        elif kind == "text":
            await turn(dev, text=m["text"])
        elif kind == "played":
            await dev.set_state("idle")
        elif kind == "volume":
            dev.volume = m["level"]
            await dev.show({"type": "volume", "level": dev.volume})


async def sheet_card(dev: Device, sheet: str) -> dict | None:
    """The card for a sheet opened from the screen (the menu, or tapping something on the home screen)."""
    if sheet == "weather":
        return (tools.weather_card(weather_now) if weather_now else
                tools.text_card("Weather", ["The forecast hasn't loaded yet. Try again in a minute."]))
    if sheet == "timers":
        return tools.timers_card(dev.name)
    if sheet == "alarms":
        return tools.alarms_card(dev.name)
    if sheet == "shopping":
        return await tools.shopping_card()
    if sheet == "agenda":
        return tools.agenda_card()
    return None


async def from_display(dev: Device, m: dict, ws: WebSocket):
    """A touch on the screen."""
    kind = m.get("type")
    if kind == "open" and (card := await sheet_card(dev, m.get("sheet"))):
        await ws.send_text(json.dumps({"type": "card", "card": card}))  # just the screen that asked
    elif kind == "timer_cancel" and isinstance(m.get("id"), int):
        tools.timer_cancel_id(dev.name, m["id"])  # the timer watcher updates every screen
    elif kind in ("timer_pause", "timer_resume") and isinstance(m.get("id"), int):
        (tools.timer_pause if kind == "timer_pause" else tools.timer_resume)(dev.name, m["id"])  # the watcher too
    elif kind in ("alarm_toggle", "alarm_delete") and isinstance(m.get("id"), int):
        if kind == "alarm_toggle":
            tools.alarm_toggle(dev.name, m["id"], bool(m.get("enabled")))
        else:
            tools.alarm_delete(dev.name, m["id"])
        await ws.send_text(json.dumps({"type": "card", "card": tools.alarms_card(dev.name)}))  # refresh this sheet
    elif kind == "shopping_remove" and isinstance(m.get("item"), str):
        try:
            await tools.shopping_remove([m["item"]])
        except todoist.TodoistError:
            pass  # the card below says what's wrong
        # Only this screen: a card opens the sheet, and other screens shouldn't pop open. They get a fresh list
        # whenever they open it.
        await ws.send_text(json.dumps({"type": "card", "card": await tools.shopping_card()}))
    elif kind == "recipe" and m.get("action") in ("next", "previous", "close"):
        if m["action"] == "close":
            tools.recipes.pop(dev.name, None)  # closing the sheet ends recipe mode
        elif tools.recipe_move(dev.name, m["action"]):
            await dev.show({"type": "card", "card": tools.recipe_card(dev.name)})  # every screen follows along
    elif kind == "media" and m.get("action") in ("play", "pause", "next", "previous"):
        sp = app.state.spotify
        try:
            await {"play": sp.resume, "pause": sp.pause, "next": sp.next, "previous": sp.previous}[m["action"]]()
        except spotify.SpotifyError as e:  # say why on the screen that was tapped
            await ws.send_text(json.dumps({"type": "card", "card": tools.text_card("Music", [str(e)])}))
        sp.changed.set()
    elif kind == "text" and m.get("text"):
        spawn(turn(dev, text=m["text"]))  # spawned, so this screen can still send (e.g. stop) meanwhile
    elif kind == "talk" and dev.sat is not None:
        await stop_ringing(dev)
        await send(dev.sat, json.dumps({"type": "listen"}))
    elif kind == "stop":
        await stop(dev)
    elif kind == "snooze":
        await stop_ringing(dev, snooze=True)
    elif kind == "volume" and isinstance(m.get("level"), (int, float)):
        await set_volume(dev, m["level"])


# Screens reload themselves when this changes, so a kiosk picks up new display code after a brain restart.
DISPLAY_VERSION = str(int(max(f.stat().st_mtime for f in (HERE / "display").rglob("*") if f.is_file())))


@app.websocket("/display")
async def display(ws: WebSocket):
    await ws.accept()
    dev = device(ws.query_params.get("device", "unknown"))
    dev.displays.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", "device": dev.name, "version": DISPLAY_VERSION, "night": {
            "start": os.environ.get("NIGHT_START", "22:00"), "end": os.environ.get("NIGHT_END", "07:00")}}))
        await ws.send_text(json.dumps(ambient(dev)))
        await ws.send_text(json.dumps({"type": "state", "state": dev.state}))
        if dev.volume is not None:
            await ws.send_text(json.dumps({"type": "volume", "level": dev.volume}))
        if dev.ringing is not None:
            await ws.send_text(json.dumps({"type": "ringing", "ringing": dev.ringing}))
        if music_now is not None:
            await ws.send_text(json.dumps({"type": "now_playing", "track": music_snapshot()}))
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            await from_display(dev, json.loads(msg.get("text") or "{}"), ws)
    except GONE:
        pass
    finally:
        dev.displays.discard(ws)


# --- Cameras -------------------------------------------------------------------------------------
# A live view is WebRTC between the screen and Home Assistant (for Ring cameras, Ring's servers). The brain only
# relays the setup messages, so Home Assistant's token never reaches the screen.

async def ha_cameras() -> list[dict]:
    """Home Assistant's cameras as [{entity, name, area}] ("Front Door Live view" is named "Front Door"), or [] if
    there's no Home Assistant or it can't be reached."""
    if not os.environ.get("HA_URL"):
        return []
    template = ('[{% for s in states.camera %}{"entity": {{ s.entity_id | tojson }}, '
                '"name": {{ s.name | replace(" Live view", "") | tojson }}, '
                '"area": {{ (area_name(s.entity_id) or "") | tojson }}}{{ "," if not loop.last }}{% endfor %}]')
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(f"{os.environ['HA_URL'].rstrip('/')}/api/template", json={"template": template},
                                headers={"Authorization": f"Bearer {os.environ.get('HA_TOKEN', '')}"})
        return json.loads(r.raise_for_status().text)
    except (httpx.HTTPError, ValueError) as e:
        print(f"cameras: couldn't list Home Assistant's cameras ({e!r}); no show_camera tool")
        return []


def ha_socket():
    """Home Assistant's WebSocket API, not yet authenticated: use as `async with ha_socket() as ha`, then ha_auth."""
    return websockets.connect(os.environ["HA_URL"].replace("http", "ws", 1).rstrip("/") + "/api/websocket")


async def ha_auth(ha):
    await ha.recv()  # auth_required
    await ha.send(json.dumps({"type": "auth", "access_token": os.environ.get("HA_TOKEN", "")}))
    if json.loads(await ha.recv())["type"] != "auth_ok":
        raise PermissionError("Home Assistant refused HA_TOKEN")


@app.websocket("/camera")
async def camera(ws: WebSocket):
    """One live view for one screen. The screen sends {offer} and then {candidate}s; it gets {type: config} (ICE
    servers), then Home Assistant's session, answer, candidate, and error events as they come. Closing this socket
    ends the stream."""
    await ws.accept()
    entity = ws.query_params.get("entity", "")
    if not entity.startswith("camera.") or not os.environ.get("HA_URL"):
        return await ws.close()
    async with ha_socket() as ha:
        await ha_auth(ha)
        await ha.send(json.dumps({"id": 1, "type": "camera/webrtc/get_client_config", "entity_id": entity}))
        config = json.loads(await ha.recv()).get("result") or {}
        await ws.send_text(json.dumps({"type": "config", "config": config.get("configuration", {})}))
        ids, session, pending = iter(range(2, 1 << 30)), None, []  # the screen's candidates wait for the session

        async def send_candidate(c):
            await ha.send(json.dumps({"id": next(ids), "type": "camera/webrtc/candidate", "entity_id": entity,
                                      "session_id": session, "candidate": c}))

        async def from_ha():
            nonlocal session
            async for raw in ha:
                m = json.loads(raw)
                if m.get("type") == "event":
                    if m["event"]["type"] == "session":
                        session = m["event"]["session_id"]
                        for c in pending:
                            await send_candidate(c)
                    await ws.send_text(json.dumps(m["event"]))
                elif m.get("type") == "result" and not m["success"]:
                    await ws.send_text(json.dumps({"type": "error", "message": m["error"]["message"]}))

        relay = asyncio.create_task(from_ha())
        try:
            while True:
                m = json.loads(await ws.receive_text())
                if "offer" in m:
                    await ha.send(json.dumps({"id": next(ids), "type": "camera/webrtc/offer", "entity_id": entity,
                                              "offer": m["offer"]}))
                elif "candidate" in m:
                    await send_candidate(m["candidate"]) if session else pending.append(m["candidate"])
        except (*GONE, websockets.ConnectionClosed):
            pass
        finally:
            relay.cancel()


@app.post("/show")
async def show(device_name: str = Query(alias="device"), camera: str = Query()):
    """Put a camera's live view on a device's screens, for Home Assistant automations (the doorbell ringing):
    POST /show?device=claude-pi&camera=camera.front_door_live_view"""
    cam = next((c for c in app.state.cameras if c["entity"] == camera), None)
    if cam is None:
        return JSONResponse({"error": f"no camera {camera}", "cameras": [c["entity"] for c in app.state.cameras]},
                            status_code=404)
    await show_camera(device_name)(cam)
    return {"shown": cam["name"]}


@app.get("/")
async def root():
    return RedirectResponse("/display/")


spotify_logins: dict[str, str] = {}  # state -> PKCE verifier, for logins in progress


@app.get("/spotify/login")
async def spotify_login():
    """Open on this machine (Spotify only redirects to 127.0.0.1) to connect the household's account."""
    if not os.environ.get("SPOTIFY_CLIENT_ID"):
        return Response("Set SPOTIFY_CLIENT_ID in .env first.", status_code=503)
    state, code_verifier = secrets.token_urlsafe(16), spotify.verifier()
    spotify_logins[state] = code_verifier
    return RedirectResponse(spotify.login_url(os.environ["SPOTIFY_CLIENT_ID"], code_verifier, state))


@app.get("/spotify/callback")
async def spotify_callback(state: str = "", code: str = "", error: str = ""):
    code_verifier = spotify_logins.pop(state, None)
    if error or not code or code_verifier is None:
        return Response(f"Spotify login didn't finish ({error or 'unknown or expired login'}). "
                        "Open /spotify/login to try again.", status_code=400)
    try:
        await app.state.spotify.exchange(code, code_verifier)
    except spotify.SpotifyError as e:
        return Response(str(e), status_code=502)
    return Response("Spotify is connected. You can close this tab.")


@app.get("/photos")
async def photo_list():
    return JSONResponse(photos.names())


@app.get("/photos/{name}")
async def photo(name: str):
    src = photos.find(name)
    if src is None:
        return Response(status_code=404)
    try:
        return FileResponse(await asyncio.to_thread(photos.cached, src), media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=86400"})
    except OSError:  # unreadable or not really an image
        return Response(status_code=404)


class Revalidated(StaticFiles):
    """Files the browser must check before reusing (a quick 304 on the LAN). Without it Chromium guesses how long
    they stay fresh, and the kiosk once reloaded for new display code with a new app.js but an old style.css."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Mounted last: routes above (including the WebSocket ones) match first.
app.mount("/display", Revalidated(directory=HERE / "display", html=True), name="display")


if __name__ == "__main__":
    host, port = os.environ.get("BRAIN_HOST", "localhost"), int(os.environ.get("BRAIN_PORT", 8765))
    print(f"Claude Home brain listening on ws://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning", ws_max_size=2**20)
