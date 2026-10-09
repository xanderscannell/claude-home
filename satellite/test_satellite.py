"""Run: uv run python test_satellite.py"""
import asyncio

import numpy as np

from satellite import FRAME, MIC_BACKLOG, VOLUME, Heard, amixer_args, parse_level, push_latest

# amixer commands: read, set, clamped
assert VOLUME == "Master", "the speaker volume is asound.conf's software control, not the device's own"
assert amixer_args(VOLUME) == ["amixer", "-c", "2", "sget", "Master"]
assert amixer_args("Music", 20) == ["amixer", "-c", "2", "-q", "sset", "Music", "20%"]
assert amixer_args(VOLUME, 140)[-1] == "100%" and amixer_args(VOLUME, -5)[-1] == "0%"
assert amixer_args(VOLUME, 37.6)[-1] == "37%"

# Levels from real amixer output on the Pi (a USB headset's PCM control, and a softvol control)
PCM = """Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 55
  Mono:
  Front Left: Playback 45 [82%] [-5.00dB] [on]
  Front Right: Playback 45 [82%] [-5.00dB] [on]"""
MUSIC = """Simple mixer control 'Music',0
  Capabilities: volume
  Playback channels: Front Left - Front Right
  Capture channels: Front Left - Front Right
  Limits: 0 - 255
  Front Left: 51 [20%]
  Front Right: 51 [20%]"""
assert parse_level(PCM) == 82
assert parse_level(MUSIC) == 20
assert parse_level("amixer: Unable to find simple control 'Music',0") is None
assert parse_level("") is None

# The mic queue keeps only the newest frames when nobody reads it (the brain is down)
q = asyncio.Queue(MIC_BACKLOG)
for i in range(MIC_BACKLOG * 3):
    push_latest(q, i)
assert q.qsize() == MIC_BACKLOG and q.get_nowait() == MIC_BACKLOG * 2, "oldest dropped, newest kept"
assert 50 <= MIC_BACKLOG <= 100, "a few seconds of 80 ms frames"


# While ringing: speech is picked out without the wake word, with a little audio from before it started
class FakeVAD:
    """Calls a frame speech when its first sample is 1."""
    def predict(self, frame, frame_size):
        return float(frame[0] == 1)

    def reset_states(self):
        pass


def frames(*kinds):
    return [np.full(FRAME, k, dtype=np.int16) for k in kinds]


heard = Heard(FakeVAD())
got = [heard.feed(f) for f in frames(*[0] * 20, 1, 1, 1, *[0] * 9)]
assert got[:-1] == [None] * 31 and got[-1] is not None, "speech counts once 0.7 s of quiet follows it"
assert len(got[-1]) == (5 + 3 + 9) * FRAME * 2, "5 frames of pre-roll, the speech, and the quiet after"
assert all(heard.feed(f) is None for f in frames(1, *[0] * 9)), "one 80 ms blip isn't speech"
long = [heard.feed(f) for f in frames(*[1] * 60)]
assert sum(x is not None for x in long) == 1, "endless talk is cut off and sent after 4 s"
print("ok")
