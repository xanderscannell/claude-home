"""Claude Home satellite: wake word -> stream speech to the brain -> play the reply.

Run from satellite/:  uv run satellite.py
Optional env: BRAIN_URL (default ws://localhost:8765), DEVICE (default: hostname),
              AUDIO_IN / AUDIO_OUT (sounddevice device index or name), WAKE_THRESHOLD (default 0.5).
"""

import asyncio
import json
import os
import socket
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

# Tuning knobs: every mic and room is different.
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", 0.5))
SPEECH_PROB = 0.5  # VAD score that counts as speech
MIN_SPEECH_S = 0.25  # less speech than this after the wake word counts as nothing said
END_SILENCE_S = 0.7  # this much silence after speech ends the utterance
NO_SPEECH_TIMEOUT_S = 4.0  # wake word fired but nobody spoke
MAX_UTTERANCE_S = 15.0
MIC_TIMEOUT_S = 2.0  # no audio for this long means the mic is gone (unplugged)


def _device(name):
    v = os.environ.get(name)
    return int(v) if v and v.isdigit() else v


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

    def drain(self):
        """Block until everything queued has played."""
        while self.buf:
            if not self.stream.active:
                raise SystemExit("audio output stopped (headset unplugged?); exiting so the service restarts")
            time.sleep(0.02)
        time.sleep(self.stream.latency)


async def next_frame(mic: asyncio.Queue) -> np.ndarray:
    """A swapped or unplugged mic fails silently in PortAudio; exit instead of waiting forever."""
    try:
        return await asyncio.wait_for(mic.get(), MIC_TIMEOUT_S)
    except TimeoutError:
        raise SystemExit("no audio from the mic (unplugged?); exiting so the service restarts")


async def receive(ws, player: Player, replies: asyncio.Queue):
    """Everything the brain sends: replies to our questions, and announcements (timers) at any time."""
    rate = 22050
    async for msg in ws:
        if isinstance(msg, bytes):
            player.play(rate, np.frombuffer(msg, dtype=np.int16))
            continue
        m = json.loads(msg)
        if m["type"] == "audio_start":
            rate = m["sample_rate"]
        elif m["type"] == "audio_end" and not m.get("announce"):
            replies.put_nowait(m)
    raise ConnectionError("brain closed the connection")


async def listen(ws, mic: asyncio.Queue, wake: Model, vad: VAD, player: Player, replies: asyncio.Queue):
    follow_up = False
    while True:
        # 1. Wait for the wake word, unless Claude just asked a question.
        if not follow_up:
            while wake.predict(await next_frame(mic))[WAKE_WORD] < WAKE_THRESHOLD:
                pass
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
            await ws.send(json.dumps({"type": "speech_end"}))
            # 3. Wait for the reply to finish playing. The mic is ignored meanwhile (no barge-in yet).
            reply = await replies.get()
            await asyncio.to_thread(player.drain)
            follow_up = reply.get("follow_up", False)
        else:
            await ws.send(json.dumps({"type": "cancel"}))
            print("(nothing heard)")
            follow_up = False

        # Drop audio captured while busy (including our own voice) and clear the wake word's memory.
        while not mic.empty():
            mic.get_nowait()
        wake.reset()


async def session(ws, mic: asyncio.Queue, wake: Model, vad: VAD, player: Player):
    replies: asyncio.Queue = asyncio.Queue()
    async with asyncio.TaskGroup() as tasks:  # if either ends (e.g. the brain disconnects), both stop
        tasks.create_task(receive(ws, player, replies))
        tasks.create_task(listen(ws, mic, wake, vad, player, replies))


async def main():
    wake = Model(wakeword_models=[WAKE_WORD], inference_framework="onnx")
    vad = VAD()
    player = Player()
    loop = asyncio.get_running_loop()
    mic: asyncio.Queue = asyncio.Queue()

    def on_audio(data, *_):
        loop.call_soon_threadsafe(mic.put_nowait, data[:, 0].copy())

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
