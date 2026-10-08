"""Raspotify lab: find which link between Spotify and the Pi's speaker is broken, and catch it breaking.

Run from brain/:  uv run --env-file ../.env python spotify_lab.py <mode>
  check              one read-only snapshot, checked from the bottom of the stack up
  cold-start [N]     restart raspotify N times (default 5), timing each layer coming up; no phone involved
  soak [SECONDS]     a snapshot every SECONDS (default 30) until Ctrl+C, logged to spotify_lab-<date>.jsonl;
                     prints each change with the journal lines and a ping to Spotify around it
  play [ALBUM]       play an album on the speaker at full volume, then pause, resume, and skip, checking each step
  conf <VARIANT>     write a variant to /etc/raspotify/conf and restart raspotify (lists them without one)
  selftest           check the parsing, no network

Pi access: PI_HOST (default claude-pi@192.168.0.178) with ~/.ssh/claude_home_pi. Needs the sudoers file
/etc/sudoers.d/raspotify-lab (ss, fuser, restart, tee to the conf) and claude-pi in systemd-journal.
"""

import asyncio
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import spotify

PI = os.environ.get("PI_HOST", "claude-pi@192.168.0.178")
KEY = Path.home() / ".ssh" / "claude_home_pi"

SNAP = r"""
echo "active $(systemctl is-active raspotify)"
echo "pid $(systemctl show -p MainPID --value raspotify)"
echo "since $(systemctl show -p ActiveEnterTimestamp --value raspotify)"
sudo -n /usr/bin/ss -tnp | grep '"librespot"' | awk '{print "sock " $5}'
sudo -n /usr/bin/fuser -v /dev/snd/pcmC2D0p 2>&1 | grep -q librespot && echo "sound yes"
for c in Master Music; do echo "$c $(amixer -c 2 sget $c 2>/dev/null | grep -o '[0-9]*%' | head -1)"; done
echo "session $(journalctl -u raspotify --since "$(systemctl show -p ActiveEnterTimestamp --value raspotify)" -o cat   | grep -oE 'Authenticated as|Connection to server closed' | tail -1)"
echo "errors $(journalctl -u raspotify --since "$(systemctl show -p ActiveEnterTimestamp --value raspotify)" -o cat   | grep -cE '(WARN|ERROR) +librespot_core::(dealer|session)')"
"""

# Each layer needs the ones above it. Being listed is the proof the dealer (Spotify Connect's command
# channel) is up: its socket looks like any other 443 one (audio downloads, spclient). Its failures show
# as "errors": librespot_core dealer/session warnings since raspotify started; Spotify closes the dealer
# now and then and librespot usually reconnects, so they aren't fatal on their own.
LAYERS = [("service", "raspotify running", lambda s: s["active"] and s["pid"] > 0),
          ("login", "connected to a Spotify access point", lambda s: s["login"]),
          ("listed", "listed by the Web API (so the Connect channel is up)", lambda s: s.get("listed", False))]

# /etc/raspotify/conf, written whole. BASE is what ran on 2026-10-07 minus LIBRESPOT_QUIET (see
# .claude/overnight/SPOTIFY-STATE.md). Flags are set by being present, even empty.
BASE = ['LIBRESPOT_NAME="Claude Home"', "LIBRESPOT_DEVICE=music", "LIBRESPOT_DEVICE_TYPE=speaker",
        "LIBRESPOT_INITIAL_VOLUME=100", "LIBRESPOT_DISABLE_AUDIO_CACHE=", "TMPDIR=/tmp"]
VARIANTS = {"quiet": BASE + ["LIBRESPOT_QUIET="],  # exactly what ran on 2026-10-07
            "baseline": BASE,  # info log: logins, disconnects, reconnects
            "verbose": BASE + ["LIBRESPOT_VERBOSE="],  # debug log, noisy
            "no-discovery": BASE + ["LIBRESPOT_DISABLE_DISCOVERY="]}  # cached login only, no zeroconf


def pi(cmd: str, stdin: str | None = None, timeout: float = 30) -> str:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-i", str(KEY), PI, cmd],
                       input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    if r.returncode == 255:
        raise ConnectionError(f"ssh to {PI} failed: {r.stderr.strip()}")
    return r.stdout


def parse(text: str) -> dict:
    s = {"active": False, "pid": 0, "since": "", "peers": [], "sound": False, "Master": None, "Music": None,
         "errors": 0, "session": ""}
    for line in text.splitlines():
        key, _, val = line.strip().partition(" ")
        if key == "active":
            s["active"] = val == "active"
        elif key == "pid":
            s["pid"] = int(val or 0)
        elif key == "since":
            s["since"] = val
        elif key == "sock":
            s["peers"].append(val)
        elif key == "sound":
            s["sound"] = True
        elif key == "session":
            s["session"] = val
        elif key == "errors":
            s["errors"] = int(val or 0)
        elif key in ("Master", "Music"):
            s[key] = val or None
    # Logged in: librespot's last word on its access point session (on 4070, or 443 after "Try another
    # access point", so the sockets can't tell). Needs the info log, i.e. not the "quiet" variant.
    s["ap"] = any(p.endswith(":4070") for p in s["peers"])
    s["login"] = s["session"] == "Authenticated as"
    return s


def first_broken(s: dict) -> str | None:
    return next((name for name, _, ok in LAYERS if not ok(s)), None)


async def api_state(sp: spotify.Spotify) -> dict:
    try:
        devs = await sp.devices()
        pb = await sp.playback()
    except spotify.SpotifyError as e:
        return {"listed": False, "api_error": str(e)}
    me = next((d for d in devs if d.get("name") == sp.device_name), None)
    t = spotify.track(pb) or {}
    return {"listed": me is not None, "device_id": me and me["id"], "devices": [d.get("name") for d in devs],
            "player_device": pb and (pb.get("device") or {}).get("name"), "playing": bool(t.get("is_playing")),
            "title": t.get("title"), "uri": t.get("uri")}


async def snapshot(sp: spotify.Spotify) -> dict:
    s = parse(await asyncio.to_thread(pi, SNAP))
    return {"t": datetime.datetime.now().isoformat(timespec="seconds"), **s, **await api_state(sp)}


def show(s: dict):
    broken = first_broken(s)
    for name, label, ok in LAYERS:
        print(f"  {'ok ' if ok(s) else 'BAD'}  {label}{'   <- first broken link' if name == broken else ''}")
    print(f"  librespot pid {s['pid']} since {s['since'] or '-'}; sockets: {', '.join(s['peers']) or 'none'}")
    print(f"  dealer/session errors since it started: {s['errors']}")
    print(f"  holding the sound device: {'yes' if s['sound'] else 'no'}; Master {s['Master']}, Music {s['Music']}")
    print(f"  Web API devices: {s.get('devices')}; player on {s.get('player_device')}, "
          f"{'playing' if s.get('playing') else 'not playing'} {s.get('title') or ''}")
    if s.get("api_error"):
        print(f"  Web API error: {s['api_error']}")


async def wait_for(sp, cond, timeout: float) -> tuple[dict, float | None]:
    """Snapshot until cond holds; the last snapshot and the seconds it took (None if it never did)."""
    t0 = time.monotonic()
    while True:
        s = await snapshot(sp)
        if cond(s):
            return s, round(time.monotonic() - t0, 1)
        if time.monotonic() - t0 > timeout:
            return s, None
        await asyncio.sleep(1)


async def cold_start(sp, rounds: int):
    for i in range(rounds):
        await asyncio.to_thread(pi, "sudo -n /usr/bin/systemctl restart raspotify")
        t0, up = time.monotonic(), {}
        while time.monotonic() - t0 < 120 and len(up) < len(LAYERS):
            s = await snapshot(sp)
            for name, _, ok in LAYERS:
                if ok(s):
                    up.setdefault(name, round(time.monotonic() - t0))
            await asyncio.sleep(3)
        print(f"round {i + 1}: " + "   ".join(f"{n} {str(up[n]) + 's' if n in up else 'never'}" for n, _, _ in LAYERS),
              flush=True)


async def soak(sp, every: float):
    log = Path(__file__).parent / f"spotify_lab-{datetime.date.today()}.jsonl"
    print(f"logging to {log.name}; Ctrl+C to stop", flush=True)
    prev, ap_ip = None, "apresolve.spotify.com"
    while True:
        try:
            s = await snapshot(sp)
        except Exception as e:  # the Pi or the network being away is a result too
            s = {"t": datetime.datetime.now().isoformat(timespec="seconds"), "error": repr(e)}
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(s) + "\n")
        ap_ip = next((p.rsplit(":", 1)[0] for p in s.get("peers", []) if p.endswith(":4070")), ap_ip)
        key = s.get("error") or (s["active"], s["pid"], s["login"], s["errors"], s["listed"], s["playing"])
        if key != prev:
            print(f"\n{s['t']}  first broken: {s.get('error') or first_broken(s) or 'nothing'}", flush=True)
            if "error" not in s:
                show(s)
            if prev is not None:
                print(pi('journalctl -u raspotify --since "-3min" --no-pager -o short-iso | tail -20'))
                print(pi(f"ping -c 3 -q -W 2 {ap_ip} | tail -2"), flush=True)
            prev = key
        await asyncio.sleep(every)


async def play(sp, album: str):
    s = await snapshot(sp)
    if not s["listed"]:
        show(s)
        sys.exit("Claude Home isn't listed by the Web API, so there's nothing to play on.")
    dev = s["device_id"]

    async def step(label, action, cond, timeout=15):
        if action is not None:
            await action
        got, took = await wait_for(sp, cond, timeout)
        print(f"  {'ok ' if took is not None else 'BAD'}  {label}: {took if took is not None else 'not within ' + str(timeout)}s",
              flush=True)
        return got

    await step("transfer to Claude Home", sp.call("PUT", "/me/player", json={"device_ids": [dev], "play": False}),
               lambda s: s.get("player_device") == sp.device_name)
    await sp.call("PUT", "/me/player/volume", params={"volume_percent": 100, "device_id": dev})
    got = await step(f"play {album!r} (Spotify says playing)", sp.play(album, "album"), lambda s: s.get("playing"))
    await step("librespot holds the sound device", None, lambda s: s["sound"])
    await step("pause", sp.pause(), lambda s: not s.get("playing"), 10)
    await step("resume", sp.resume(), lambda s: s.get("playing"), 10)
    await step("next track", sp.next(), lambda s: s.get("uri") not in (None, got.get("uri")), 10)
    print("  left playing, so you can listen; pause it from the app or the screen")


async def write_conf(variant: str):
    lines = [f"# Written by brain/spotify_lab.py: variant {variant!r}. Stock: conf.orig; before 2026-10-07's",
             "# changes: conf.bak-2026-10-07.", *VARIANTS[variant]]
    await asyncio.to_thread(pi, "sudo -n /usr/bin/tee /etc/raspotify/conf >/dev/null", "\n".join(lines) + "\n")
    await asyncio.to_thread(pi, "sudo -n /usr/bin/systemctl restart raspotify")
    print(f"wrote {variant!r} and restarted raspotify")


def selftest():
    s = parse("active active\npid 13923\nsince Wed 2026-10-07 11:31:33 EDT\nsock 34.158.255.62:4070\n"
              "sock [2600:1901:1:c36::]:443\nMaster 100%\nMusic \nsession Authenticated as\n")
    assert s["active"] and s["pid"] == 13923 and s["ap"] and s["login"] and not s["sound"] and s["errors"] == 0
    assert s["Master"] == "100%" and s["Music"] is None
    assert first_broken({**s, "listed": False}) == "listed" and first_broken({**s, "listed": True}) is None
    dead = parse("active inactive\npid 0\nsince \n")
    assert first_broken(dead) == "service" and not dead["peers"]
    # 2026-10-08 08:41: the session closed and didn't come back; one 443 socket left, still listed
    closed = parse("active active\npid 5\nsock [2600:1901:1:3b2::]:443\nsession Connection to server closed\nerrors 6\n")
    assert first_broken({**closed, "listed": True}) == "login" and closed["errors"] == 6
    fallback = parse("active active\npid 5\nsock 34.158.255.62:443\nsession Authenticated as\n")  # AP on 443
    assert fallback["login"] and not fallback["ap"]
    print("selftest ok")


async def main(mode: str, arg: str | None):
    if mode == "selftest":
        return selftest()
    if mode == "conf":
        if arg not in VARIANTS:
            sys.exit("variants: " + ", ".join(VARIANTS))
        return await write_conf(arg)
    sp = spotify.Spotify(os.environ["SPOTIFY_CLIENT_ID"])
    try:
        if mode == "check":
            show(await snapshot(sp))
        elif mode == "cold-start":
            await cold_start(sp, int(arg or 5))
        elif mode == "soak":
            await soak(sp, float(arg or 30))
        elif mode == "play":
            await play(sp, arg or "Abbey Road")
        else:
            sys.exit(__doc__)
    finally:
        await sp.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # track titles in any script; Windows defaults to cp1252
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2] if len(sys.argv) > 2 else None))
    except KeyboardInterrupt:
        pass
