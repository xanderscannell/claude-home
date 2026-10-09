"""Claude Home satellite: wake word -> stream speech to the brain -> play the reply.

Run from satellite/:  uv run satellite.py
Optional env: BRAIN_URL (default ws://localhost:8765), DEVICE (default: hostname),
              AUDIO_IN / AUDIO_OUT (sounddevice device index or name), WAKE_THRESHOLD (default 0.5),
              MIXER_CARD (ALSA card with the Master and Music controls from asound.conf, default 2).
"""

import asyncio
import json
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
import websockets
from openwakeword.model import Model
from openwakeword.vad import VAD

RATE = 16000
FRAME = 1280  # 80 ms, the chunk size openWakeWord expects
FRAME_S = FRAME / RATE
WAKE_WORD = "hey_jarvis"
DEVICE = os.environ.get("DEVICE", socket.gethostname())
MIXER_CARD = os.environ.get("MIXER_CARD", "2")

# Tuning knobs: every mic and room is different.
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", 0.5))
SPEECH_PROB = 0.5  # VAD score that counts as speech
MIN_SPEECH_S = 0.25  # less speech than this after the wake word counts as nothing said
END_SILENCE_S = 0.7  # this much silence after speech ends the utterance
NO_SPEECH_TIMEOUT_S = 4.0  # wake word fired but nobody spoke
MAX_UTTERANCE_S = 15.0
PRE_ROLL_S = 0.4  # audio kept from before speech starts, so a short "stop" heard while ringing isn't clipped
RING_SPEECH_MAX_S = 4.0  # longest speech sent to the brain while ringing (it only listens for "stop" or "snooze")
RING_MIN_SPEECH_S = 0.16  # a lone "stop" is short; the brain's transcript decides what it was
MIC_TIMEOUT_S = 2.0  # no audio for this long means the mic is gone (unplugged)
MIC_BACKLOG = int(5 / FRAME_S)  # mic frames kept for a reader that fell behind; older ones are dropped
DUCK_PERCENT = 20  # music volume while listening and talking
VOLUME = "Master"  # the speaker's volume: asound.conf's software control, the same whatever is plugged in


def _device(name):
    v = os.environ.get(name)
    return int(v) if v and v.isdigit() else v


# --- Mixer (amixer on the Pi; absent on a dev PC, where every call quietly returns None) ----------------

LEVEL = re.compile(r"\[(\d{1,3})%\]")


def amixer_args(control: str, percent: int | None = None) -> list[str]:
    """The amixer command that reads a control (percent None) or sets it, clamped to 0-100."""
    if percent is None:
        return ["amixer", "-c", MIXER_CARD, "sget", control]
    return ["amixer", "-c", MIXER_CARD, "-q", "sset", control, f"{max(0, min(100, int(percent)))}%"]


def parse_level(output: str) -> int | None:
    """First percentage in amixer's output (the left channel for stereo controls)."""
    m = LEVEL.search(output)
    return int(m[1]) if m else None


def mixer(control: str, percent: int | None = None) -> int | None:
    """Set a control if percent is given; return its level, or None if there is no amixer or no such control."""
    try:
        if percent is not None and subprocess.run(amixer_args(control, percent), capture_output=True,
                                                  timeout=2).returncode != 0:
            return None
        r = subprocess.run(amixer_args(control), capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_level(r.stdout) if r.returncode == 0 else None


def chime(rate=22050) -> tuple[int, np.ndarray]:
    """Two short rising tones: 'I'm listening'."""
    t = np.arange(int(rate * 0.09)) / rate
    fade = np.minimum(1, np.minimum(t, t[::-1]) / 0.01)  # 10 ms fade in/out, no clicks
    tone = lambda hz: (np.sin(2 * np.pi * hz * t) * fade * 6000).astype(np.int16)
    return rate, np.concatenate([tone(880), tone(1320)])


class Player:
    """One always-running output stream that plays queued audio, and silence when there is none.

    The stream never underflows, so the first sentence after a quiet spell plays as cleanly as the rest.
    """

    def __init__(self, rate=22050):
        self.buf: deque[np.ndarray] = deque()
        self.lock = threading.Lock()
        self.stream = None
        self._open(rate)

    def _open(self, rate):
        if self.stream:
            self.stream.close()
        self.rate = rate
        self.stream = sd.OutputStream(samplerate=rate, channels=1, dtype="int16",
                                      device=_device("AUDIO_OUT"), callback=self._fill)
        self.stream.start()

    def _fill(self, out, frames, _time, _status):  # runs on the audio thread
        out.fill(0)
        done = 0
        with self.lock:
            while done < frames and self.buf:
                chunk = self.buf[0]
                n = min(len(chunk), frames - done)
                out[done:done + n, 0] = chunk[:n]
                if n == len(chunk):
                    self.buf.popleft()
                else:
                    self.buf[0] = chunk[n:]
                done += n

    def play(self, rate: int, audio: np.ndarray):
        if rate != self.rate:  # a voice with a different sample rate: finish what's queued, then reopen
            self.drain()
            self._open(rate)
        with self.lock:
            self.buf.append(audio)

    def clear(self):
        """Drop everything queued (Stop)."""
        with self.lock:
            self.buf.clear()

    def drain(self):
        """Block until everything queued has played."""
        while self.buf:
            if not self.stream.active:
                raise SystemExit("audio output stopped (headset unplugged?); exiting so the service restarts")
            time.sleep(0.02)
        time.sleep(self.stream.latency)


def push_latest(q: asyncio.Queue, item):
    """Queue item, dropping the oldest when full. While the brain is down nothing reads the mic, and an
    unbounded queue grew 32 KB/s (660 MB over a 6-hour outage), then took hours of CPU to chew through."""
    if q.full():
        q.get_nowait()
    q.put_nowait(item)


async def next_frame(mic: asyncio.Queue) -> np.ndarray:
    """A swapped or unplugged mic fails silently in PortAudio; exit instead of waiting forever."""
    try:
        return await asyncio.wait_for(mic.get(), MIC_TIMEOUT_S)
    except TimeoutError:
        raise SystemExit("no audio from the mic (unplugged?); exiting so the service restarts")


class Heard:
    """Speech picked out of the mic frames as they come, without the wake word: used while something rings, so
    "stop" works on its own. feed() returns the int16 audio once a stretch of speech ends, else None."""

    def __init__(self, vad: VAD):
        self.vad = vad
        self.frames: list[np.ndarray] = []
        self.speech = self.silence = 0.0
        vad.reset_states()

    def feed(self, frame: np.ndarray) -> bytes | None:
        self.frames.append(frame)
        if self.vad.predict(frame, frame_size=640) >= SPEECH_PROB:
            self.speech, self.silence = self.speech + FRAME_S, 0.0
        elif self.speech:
            self.silence += FRAME_S
        else:
            del self.frames[:-int(PRE_ROLL_S / FRAME_S)]  # nothing said yet: keep only the pre-roll
        if self.speech and (self.silence >= END_SILENCE_S or len(self.frames) * FRAME_S >= RING_SPEECH_MAX_S):
            said = self.speech >= RING_MIN_SPEECH_S and b"".join(f.tobytes() for f in self.frames)
            self.frames, self.speech, self.silence = [], 0.0, 0.0
            return said or None
        return None


async def report_volume(ws):
    level = await asyncio.to_thread(mixer, VOLUME)
    if level is not None:
        await ws.send(json.dumps({"type": "volume", "level": level}))


async def finish_unasked(ws, player: Player, end: dict, listen_now: asyncio.Event):
    """A reply to a request made on the screen, not by voice: play it out, report it, listen if asked."""
    await asyncio.to_thread(player.drain)
    await ws.send(json.dumps({"type": "played"}))
    if end.get("follow_up"):
        listen_now.set()
    else:
        await asyncio.to_thread(mixer, "Music", 100)


async def receive(ws, player: Player, replies: asyncio.Queue, listen_now: asyncio.Event, awaiting: asyncio.Event,
                  ringing: asyncio.Event):
    """Everything the brain sends: replies, announcements (timers) at any time, and control messages."""
    rate = 22050
    pending = set()  # unasked-reply tasks, kept referenced
    async for msg in ws:
        if isinstance(msg, bytes):
            player.play(rate, np.frombuffer(msg, dtype=np.int16))
            continue
        m = json.loads(msg)
        kind = m["type"]
        if kind == "audio_start":
            rate = m["sample_rate"]
            if not m.get("announce") and not awaiting.is_set():
                await asyncio.to_thread(mixer, "Music", DUCK_PERCENT)  # a screen request: duck for the reply too
        elif kind == "audio_end" and not m.get("announce"):
            if awaiting.is_set():
                replies.put_nowait(m)  # the reply to what was just said; listen() takes it from here
            else:
                task = asyncio.create_task(finish_unasked(ws, player, m, listen_now))
                pending.add(task)
                task.add_done_callback(pending.discard)
        elif kind == "listen":  # tap-to-talk on the screen
            listen_now.set()
        elif kind == "stop":
            player.clear()
        elif kind == "ringing":  # an alarm or timer started or stopped ringing here
            ringing.set() if m["on"] else ringing.clear()
        elif kind == "volume":
            await asyncio.to_thread(mixer, VOLUME, m["level"])
            await report_volume(ws)
    raise ConnectionError("brain closed the connection")


async def listen(ws, mic: asyncio.Queue, wake: Model, vad: VAD, player: Player, replies: asyncio.Queue,
                 listen_now: asyncio.Event, awaiting: asyncio.Event, ringing: asyncio.Event):
    follow_up = False
    while True:
        # 1. Wait for the wake word (or a tap on the screen), unless Claude just asked a question. While something
        #    rings, any speech goes to the brain too, which stops the ringing if it hears "stop".
        if not follow_up:
            heard = Heard(vad)
            while not listen_now.is_set():
                frame = await next_frame(mic)
                if wake.predict(frame)[WAKE_WORD] >= WAKE_THRESHOLD:
                    break
                if not ringing.is_set():
                    if heard.frames:
                        heard = Heard(vad)  # nothing stale left over for the next ring
                elif said := heard.feed(frame):
                    await ws.send(said)
                    await ws.send(json.dumps({"type": "ring_speech"}))
            listen_now.clear()
            await asyncio.to_thread(mixer, "Music", DUCK_PERCENT)  # duck the music while we talk
        player.play(*chime(player.rate))
        await ws.send(json.dumps({"type": "wake"}))
        print("listening for the answer..." if follow_up else "wake word, listening...")

        # 2. Stream audio until the speaker goes quiet.
        vad.reset_states()
        speech = silence = total = 0.0
        while True:
            frame = await next_frame(mic)
            await ws.send(frame.tobytes())
            total += FRAME_S
            if vad.predict(frame, frame_size=640) >= SPEECH_PROB:
                speech, silence = speech + FRAME_S, 0.0
            else:
                silence += FRAME_S
            heard = speech >= MIN_SPEECH_S
            if (heard and silence >= END_SILENCE_S) or total >= MAX_UTTERANCE_S:
                break
            if not heard and total >= NO_SPEECH_TIMEOUT_S:
                break

        if heard:
            awaiting.set()
            await ws.send(json.dumps({"type": "speech_end"}))
            # 3. Wait for the reply to finish playing. The mic is ignored meanwhile (no barge-in yet).
            reply = await replies.get()
            awaiting.clear()
            await asyncio.to_thread(player.drain)
            await ws.send(json.dumps({"type": "played"}))
            follow_up = reply.get("follow_up", False)
        else:
            await ws.send(json.dumps({"type": "cancel"}))
            print("(nothing heard)")
            follow_up = False

        if not follow_up:
            await asyncio.to_thread(mixer, "Music", 100)
        # Drop audio captured while busy (including our own voice) and clear the wake word's memory.
        while not mic.empty():
            mic.get_nowait()
        wake.reset()


async def session(ws, mic: asyncio.Queue, wake: Model, vad: VAD, player: Player):
    replies: asyncio.Queue = asyncio.Queue()
    listen_now = asyncio.Event()  # the screen asked us to listen (tap-to-talk, or a question from Claude)
    awaiting = asyncio.Event()  # set while listen() waits for the reply to what it just sent
    ringing = asyncio.Event()  # an alarm or timer is ringing here: "stop" works without the wake word
    await report_volume(ws)
    async with asyncio.TaskGroup() as tasks:  # if either ends (e.g. the brain disconnects), both stop
        tasks.create_task(receive(ws, player, replies, listen_now, awaiting, ringing))
        tasks.create_task(listen(ws, mic, wake, vad, player, replies, listen_now, awaiting, ringing))


async def main():
    wake = Model(wakeword_models=[WAKE_WORD], inference_framework="onnx")
    vad = VAD()
    player = Player()
    loop = asyncio.get_running_loop()
    mic: asyncio.Queue = asyncio.Queue(MIC_BACKLOG)

    def on_audio(data, *_):
        loop.call_soon_threadsafe(push_latest, mic, data[:, 0].copy())

    url = f"{os.environ.get('BRAIN_URL', 'ws://localhost:8765')}/satellite?device={DEVICE}"
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                        device=_device("AUDIO_IN"), callback=on_audio):
        async for ws in websockets.connect(url, max_size=2**22):  # reconnects with backoff
            try:
                await ws.send(json.dumps({"type": "hello", "device": DEVICE, "capabilities": {"speaker": True}}))
                print(f"connected to {url}. Say 'hey Jarvis'.")
                await session(ws, mic, wake, vad, player)
            except* (websockets.ConnectionClosed, ConnectionError):
                print("brain disconnected, reconnecting...")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
