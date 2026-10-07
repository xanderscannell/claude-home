"""Phase 1 step 1: push-to-talk loop. Mic -> Groq STT -> Claude (Agent SDK) -> Piper -> speaker.

Run from brain/:  uv run --env-file ../.env proto.py
Optional env: AUDIO_IN / AUDIO_OUT = sounddevice device index or name (default: system default).
"""

import asyncio
import io
import os
import time
import wave
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock
from piper import PiperVoice

RATE = 16000
VOICE = Path(__file__).parent / "voices" / "en_US-lessac-medium.onnx"

SYSTEM_PROMPT = """You are Claude Home, a voice assistant on a smart speaker. Everything you write is spoken aloud.
- Answer in one to three short sentences unless asked for more.
- No markdown, lists, emoji, or URLs. Say numbers, times, and units the way a person would speak them.
- If the request is ambiguous, ask one short clarifying question.
- Each user message starts with the current local time in brackets; use it when relevant, never read it back unprompted."""

OPTIONS = ClaudeAgentOptions(
    system_prompt=SYSTEM_PROMPT,
    model="haiku",
    setting_sources=[],  # ignore ~/.claude and project config (CLAUDE.md, hooks, plugins)
    tools=["WebSearch"],  # no shell or file tools on this machine
    allowed_tools=["WebSearch"],
    permission_mode="dontAsk",
    # On a subscription login, claude.ai connectors (Gmail, Drive, ...) load into every session.
    # Block them both ways; setting_sources=[] alone does not.
    env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    extra_args={"strict-mcp-config": None},
)


def _device(name):
    v = os.environ.get(name)
    return int(v) if v and v.isdigit() else v


def record() -> np.ndarray:
    """Record from Enter to Enter."""
    frames = []
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", device=_device("AUDIO_IN"),
                        callback=lambda data, *_: frames.append(data.copy())):
        input("  recording... press Enter to stop")
    return np.concatenate(frames) if frames else np.zeros(0, dtype="int16")


def transcribe(pcm: np.ndarray) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    r = httpx.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.environ['GROQ_KEY']}"},
        files={"file": ("speech.wav", buf.getvalue(), "audio/wav")},
        data={"model": "whisper-large-v3-turbo", "language": "en", "response_format": "json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["text"].strip()


async def ask(client: ClaudeSDKClient, text: str) -> str:
    await client.query(f"[{datetime.now():%A %B %d, %I:%M %p}] {text}")
    parts = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            parts += [b.text for b in msg.content if isinstance(b, TextBlock)]
    return " ".join(parts).strip()


def speak(voice: PiperVoice, text: str) -> float:
    """Synthesize fully, then play. Returns synthesis seconds. (Sentence streaming is step 2.)"""
    t = time.perf_counter()
    chunks = list(voice.synthesize(text))
    synth = time.perf_counter() - t
    if chunks:
        sd.play(np.concatenate([c.audio_int16_array for c in chunks]), chunks[0].sample_rate,
                device=_device("AUDIO_OUT"))
        sd.wait()
    return synth


async def main():
    voice = PiperVoice.load(VOICE)
    async with ClaudeSDKClient(options=OPTIONS) as client:
        print("Claude Home prototype. Enter to talk, Ctrl+C to quit.")
        while True:
            await asyncio.to_thread(input, "\n[Enter] to talk ")
            pcm = await asyncio.to_thread(record)
            if len(pcm) < RATE // 2:
                print("  (too short, skipped)")
                continue

            t0 = time.perf_counter()
            text = await asyncio.to_thread(transcribe, pcm)
            t1 = time.perf_counter()
            if len(text.split()) < 2:  # empty, or a Whisper hallucination like "you" / "Thank you."
                print(f"  (heard {text!r}, skipped)")
                continue
            print(f"  you:    {text}")

            reply = await ask(client, text)
            t2 = time.perf_counter()
            print(f"  claude: {reply}")

            synth = await asyncio.to_thread(speak, voice, reply)
            print(f"  timing: stt {t1 - t0:.2f}s | claude {t2 - t1:.2f}s | tts {synth:.2f}s"
                  f" | end of speech to audio {t2 - t0 + synth:.2f}s")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
