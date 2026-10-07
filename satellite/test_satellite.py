"""Run: uv run python test_satellite.py"""
from satellite import amixer_args, parse_level

# amixer commands: read, set, clamped
assert amixer_args("PCM") == ["amixer", "-c", "2", "sget", "PCM"]
assert amixer_args("Music", 20) == ["amixer", "-c", "2", "-q", "sset", "Music", "20%"]
assert amixer_args("PCM", 140)[-1] == "100%" and amixer_args("PCM", -5)[-1] == "0%"
assert amixer_args("PCM", 37.6)[-1] == "37%"

# Levels from real amixer output on the Pi (SteelSeries Arctis Pro, and the softvol Music control)
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
print("ok")
