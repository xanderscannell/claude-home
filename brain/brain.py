"""Claude Home brain: WebSocket server for satellites. Speech -> Groq STT -> Claude (Agent SDK) -> Piper -> speech.

Claude's reply is spoken sentence by sentence while it is still being generated.
Every turn's timings are appended to turns.jsonl.

Run from brain/:  uv run --env-file ../.env brain.py
Optional env: BRAIN_HOST (default localhost; set 0.0.0.0 to accept satellites from the LAN), BRAIN_PORT (8765),
              HOME_LOCATION (city name or "lat,lon", for weather), CLAUDE_HOME_DB (default brain/home.db).

Protocol (see docs/architecture.md): satellite sends hello, wake, binary 16 kHz int16 mono mic audio,
then speech_end (or cancel), or {type: text, text} to skip speech-to-text. Brain replies
audio_start {sample_rate}, binary int16 speech, audio_end {follow_up}. Unprompted speech (a finished timer)
comes as the same messages with announce: true.
"""

import asyncio
import io
import json
import math
import os
import re
import time
import wave
from array import array
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import websockets
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, StreamEvent
from piper import PiperVoice

import tools

RATE = 16000
HERE = Path(__file__).parent
VOICE = HERE / "voices" / "en_GB-northern_english_male-medium.onnx"
LOG = HERE / "turns.jsonl"

SYSTEM_PROMPT = """You are Claude Home, a voice assistant on a smart speaker. Everything you write is spoken aloud.
- Answer in one to three short sentences unless asked for more.
- Start with a short sentence that answers directly, then add detail. The first sentence is spoken while you write the rest.
- No markdown, lists, emoji, or URLs. Say numbers, times, and units the way a person would speak them.
- If the request is ambiguous, ask one short clarifying question.
- Search the web only when you need current information, at most once per question, then answer.
- After a web search, never list sources or links; the listener cannot see them.
- Each user message starts with the current local time in brackets; use it when relevant, never read it back unprompted.
- Use your tools for timers, the shopping list, weather, and remembering things. Confirm what you did in a few words.
- When you end with a question, the speaker listens for the answer without the wake word, so only ask when you need to."""

OPTIONS = ClaudeAgentOptions(
    system_prompt=SYSTEM_PROMPT,
    model="haiku",
    setting_sources=[],  # ignore ~/.claude and project config (CLAUDE.md, hooks, plugins)
    tools=["WebSearch"],  # no shell or file tools on this machine
    allowed_tools=["WebSearch", "mcp__home__*"],
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
    """Yield the reply one sentence at a time as Claude streams it."""
    await client.query(f"[{datetime.now():%A %B %d, %I:%M %p}] {text}")
    buf, muted = "", False
    async for msg in client.receive_response():
        if not isinstance(msg, StreamEvent):
            continue
        ev = msg.event
        if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
            sentences, buf = pop_sentences(buf + ev["delta"]["text"])
        elif ev.get("type") == "content_block_start" and ev["content_block"].get("type") in ("tool_use", "server_tool_use"):
            print(f"  tool: {ev['content_block']['name'].removeprefix('mcp__home__')}")
            continue
        elif ev.get("type") == "content_block_stop":
            sentences, buf = [buf], ""  # end of a text block (e.g. before a web search) flushes the tail
        else:
            continue
        for s in sentences:
            # The WebSearch tool insists on a trailing "Sources:" list of links; never speak it.
            muted = muted or bool(SOURCES.match(s))
            if not muted and (s := MD_LINK.sub(r"\1", s).strip()):
                yield s
    if buf.strip() and not muted:
        yield MD_LINK.sub(r"\1", buf).strip()


# Whisper's usual output for silence or noise, and ways of calling off a request.
IGNORE = {"", "you", "thank you", "thanks for watching", "bye", "never mind", "nevermind", "cancel", "stop",
          "nothing", "forget it"}


def options(device: str) -> ClaudeAgentOptions:
    """Per-satellite session: its own timer tools, and saved facts baked into the prompt."""
    known = tools.facts()
    memory = "\n\nThings you remember about this household:\n" + "\n".join(f"- {f}" for f in known) if known else ""
    return replace(OPTIONS, system_prompt=SYSTEM_PROMPT + memory, mcp_servers={"home": tools.make_tools(device)})


def beeps(rate: int) -> bytes:
    """Three short beeps for a finished timer."""
    n, fade = int(rate * 0.15), rate * 0.01
    tone = [int(8000 * math.sin(2 * math.pi * 988 * i / rate) * min(1, i / fade, (n - i) / fade)) for i in range(n)]
    return array("h", (tone + [0] * int(rate * 0.1)) * 3).tobytes()


async def announce(ws, lock: asyncio.Lock, voice: PiperVoice, text: str):
    """Speak without being asked, e.g. a finished timer. The lock keeps it from interleaving with a reply."""
    rate = voice.config.sample_rate
    audio = await asyncio.to_thread(lambda: [c.audio_int16_bytes for c in voice.synthesize(for_speech(text))])
    async with lock:
        await ws.send(json.dumps({"type": "audio_start", "sample_rate": rate, "announce": True}))
        for chunk in [beeps(rate), *audio]:
            await ws.send(chunk)
        await ws.send(json.dumps({"type": "audio_end", "announce": True}))


async def turn(ws, lock: asyncio.Lock, client: ClaudeSDKClient, groq: httpx.Client, voice: PiperVoice, device: str,
               pcm: bytes = b"", text: str | None = None):
    t0 = time.perf_counter()  # speech_end received (the satellite's VAD silence tail came just before)
    if text is None:
        text = await asyncio.to_thread(transcribe, groq, pcm) if len(pcm) >= RATE else ""  # >= 0.5 s of audio
    t_stt = time.perf_counter()
    async with lock:
        if text.lower().strip(" .!?,") in IGNORE:
            print(f"[{device}] (heard {text!r}, skipped)")
            await ws.send(json.dumps({"type": "audio_end", "follow_up": False}))
            return
        print(f"[{device}] you:    {text}")

        await ws.send(json.dumps({"type": "audio_start", "sample_rate": voice.config.sample_rate}))
        reply, t_first, t_audio = [], None, None
        async for s in ask(client, text):
            t_first = t_first or time.perf_counter()
            reply.append(s)
            audio = await asyncio.to_thread(lambda: [c.audio_int16_bytes for c in voice.synthesize(for_speech(s))])
            for chunk in audio:
                await ws.send(chunk)
            t_audio = t_audio or time.perf_counter()
        follow_up = bool(reply) and reply[-1].endswith("?")
        await ws.send(json.dumps({"type": "audio_end", "follow_up": follow_up}))
    print(f"[{device}] claude: {' '.join(reply)}")

    log = {"ts": datetime.now().isoformat(timespec="seconds"), "device": device, "you": text,
           "claude": " ".join(reply), "audio_s": round(len(pcm) / 2 / RATE, 2),
           "stt": t_stt - t0,
           "claude_first_sentence": (t_first or t_stt) - t_stt,
           "tts_first_audio": (t_audio or t_first or t_stt) - (t_first or t_stt),
           "end_of_speech_to_audio": (t_audio or t_stt) - t0}
    log = {k: round(v, 2) if isinstance(v, float) else v for k, v in log.items()}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(log) + "\n")
    print(f"[{device}] timing: stt {log['stt']}s | claude first sentence {log['claude_first_sentence']}s"
          f" | tts {log['tts_first_audio']}s | end of speech to audio {log['end_of_speech_to_audio']}s")


async def main():
    voice = PiperVoice.load(VOICE)
    groq = httpx.Client(base_url="https://api.groq.com/openai/v1", timeout=30,
                        headers={"Authorization": f"Bearer {os.environ['GROQ_KEY']}"},
                        limits=httpx.Limits(keepalive_expiry=60))  # httpx default drops idle connections after 5 s
    groq.get("/models").raise_for_status()  # fail fast on a bad key
    satellites: dict[str, tuple] = {}  # device -> (ws, send lock)
    background = set()  # references to fire-and-forget tasks so they aren't garbage collected

    def spawn(coro):
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    async def satellite(ws):
        device = parse_qs(urlparse(ws.request.path).query).get("device", ["unknown"])[0]
        lock = asyncio.Lock()
        satellites[device] = (ws, lock)
        print(f"[{device}] connected")
        try:
            async with ClaudeSDKClient(options=options(device)) as client:  # one conversation per connection
                await serve(ws, lock, client, device)
        except websockets.ConnectionClosed:
            pass  # satellite went away without a clean close (killed, network drop)
        finally:
            if satellites.get(device, (None,))[0] is ws:
                del satellites[device]
        print(f"[{device}] disconnected")

    async def serve(ws, lock, client, device):
        frames = []
        async for msg in ws:
            if isinstance(msg, bytes):
                frames.append(msg)
                continue
            m = json.loads(msg)
            kind = m.get("type")
            if kind == "wake":
                frames = []
                # Reopen the Groq connection while the user talks, so STT never pays for a TLS handshake.
                spawn(asyncio.to_thread(groq.get, "/models"))
            elif kind == "cancel":
                frames = []
            elif kind == "speech_end":
                await turn(ws, lock, client, groq, voice, device, pcm=b"".join(frames))
                frames = []
            elif kind == "text":
                await turn(ws, lock, client, groq, voice, device, text=m["text"])

    async def ring_timers():
        """Timers live in SQLite, so they survive restarts; overdue ones ring once a satellite connects."""
        while True:
            await asyncio.sleep(0.5)
            if not satellites:
                continue
            for device, label in tools.timers_due():
                ws, lock = satellites.get(device) or next(iter(satellites.values()))
                print(f"[{device}] timer done: {label or 'unnamed'}")
                spawn(announce(ws, lock, voice, f"Your {label} timer is done." if label else "Your timer is done."))

    host, port = os.environ.get("BRAIN_HOST", "localhost"), int(os.environ.get("BRAIN_PORT", 8765))
    async with websockets.serve(satellite, host, port, max_size=2**20):
        print(f"Claude Home brain listening on ws://{host}:{port}")
        await ring_timers()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
