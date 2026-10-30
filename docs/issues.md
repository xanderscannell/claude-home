# Known issues

Problems on the Pi that are worked around or waiting to happen again, with everything measured so far, so
the next person to hit one starts from the evidence rather than from scratch. Each says what was seen,
what was ruled out, what's in place now, what to do when it happens, and when to close it. Times are the
Pi's clock on 2026-10-29 (it has no real-time clock, so times logged before the network comes up after a
boot can be a few minutes off).

## The mic array stops streaming after its audio is reopened

**Status:** worked around by `satellite/array-hold.service`. The cause is unknown.

### Setup

- Raspberry Pi 3 B+, Raspberry Pi OS Trixie, the Pi's own USB host driver (`dwc_otg`).
- ReSpeaker XVF3800 on USB at high speed, on the Pi's internal hub (the same hub as the Ethernet chip).
- Seeed's USB firmware `v2.1.1_48k2ch` (the array reports version 2.11): playback and capture are both
  2 channels, `S16_LE`, 48000 Hz only, both endpoints synchronous with a 500 us packet interval.
- `asound.conf`: playback mixed with `dmix` at 48000, the mic shared with `dsnoop`, the satellite reading
  the right channel converted to 16 kHz.

The stock firmware (2.0.6, 16 kHz only) ran for weeks, through a satellite restart on every deploy,
without this happening.

### What it looks like

- The satellite exits with `no audio from the mic (unplugged?); exiting so the service restarts` about
  47 s after each start, and systemd restarts it, over and over. Screens see it reconnect constantly
  (it's restarting; the brain connection itself never drops). The wake word does nothing.
- Both of the array's streams show `state: RUNNING` in `/proc/asound/Array/pcm0c/sub0/status` (mic) and
  `pcm0p` (speaker), but `hw_ptr` doesn't move: 0 frames a second, where a healthy stream moves about
  48,000.
- Recording 2 s through `default`, `mic`, or straight from `dsnoop:CARD=Array` at 48 kHz doesn't finish
  in 20 s. It stalls with the satellite stopped and nothing playing too, so it isn't playback.
- The kernel logs nothing: no USB, sound, or xrun errors.
- ALSA sets the chain up correctly (hardware 48000 Hz, 2 channels, period 1024, buffer 16384), so it
  isn't `asound.conf`.
- Unexplained: the satellite lasts about 47 s before its 2 s mic timeout fires, though `hw_ptr` reads 0
  the whole time. Probably PortAudio handing over something other than real audio at first.

### Timeline

| Time | What happened |
|---|---|
| 21:39 | Flashed `v2.1.1_48k2ch`; the array came back as 2.11. |
| about 21:40 | A direct 3 s recording from the hardware at 48 kHz worked: the first open after the flash. |
| 21:42 | The satellite and raspotify started, reopening the array. It stalled, and every restart after failed at about 47 s. |
| 21:48 | Full power cycle. The satellite opened the array first and ran for about 30 minutes, through tests that joined its open streams (`dsnoop`, `aplay` into `dmix`) without reopening the hardware. |
| 22:18 | The satellite restarted (a new `asound.conf` was installed). Stalled again within about 47 s. |
| 22:21 | `usbreset 2886:001a` reported ok. No change. |
| 22:27 | Services restarted with `array-hold.service` installed. Still stalled: the array hadn't been power-cycled. |
| 22:33 to 22:36 | With the array's cable pulled out of the Pi, the kernel still listed it (same device number, sound card present) and had logged no disconnect, but a control message to it (`xvf_host.py VERSION`) failed with an I/O error. The Pi's USB stack had stopped processing hub events. |
| 22:35 | Full power cycle with `array-hold.service` enabled. It opened the array before anything else. |
| 22:42 | Stopped the satellite on purpose; systemd restarted it. The mic kept streaming at about 48,400 frames a second for 90 s, and the satellite stayed up. |

So: the first open after power-up works, and a full close and reopen breaks it until the next power cycle.

### Ruled out

- **Power.** The worst of the undervoltage was fixed before this started (a combined 5 V 2.5 A supply and
  cable), and `vcgencmd get_throttled` read `0x0` through it.
- **The ALSA config.** `dsnoop` on its own stalls the same way, with correct parameters.
- **Playback.** Capture alone stalls.
- **CPU.** The satellite's main thread sits at about 71% of a core with or without music.

### What might cause it

Not settled; no test so far tells these apart.

- **The firmware:** the `48k2ch` build doesn't restart its streams properly when the host closes the audio
  interfaces and opens them again.
- **The Pi 3's USB controller:** `dwc_otg` is known to be weak at continuous high-speed isochronous
  transfers, and 48 kHz both ways is three times the data of 16 kHz. The unplug the kernel never noticed
  points this way: a wedged controller would also stop every audio transfer at once.
- **Both together.**

### The workaround

`array-hold.service` starts at boot, before the satellite and raspotify, and holds the array open for
good: `aplay` writes silence into the same `dmix` that `asound.conf` uses, and `arecord` reads the same
`dsnoop` into `/dev/null`. Everything else joins those shared streams, so a satellite restart leaves and
rejoins them while the hardware stays open. It costs a percent or two of CPU.

It only avoids the one trigger seen so far. If the hardware gets closed anyway (the service stopped while
nothing else holds the array), or there's another trigger, it will stall again.

### When it happens

1. Confirm: `journalctl -u claude-home-satellite -f` shows the exit above every minute or so, and
   `hw_ptr` in `/proc/asound/Array/pcm0c/sub0/status` doesn't move between two reads a second apart.
2. Note what came just before (a restart, a deploy, a reboot, the hold service's log:
   `journalctl -u array-hold`) and add it to the timeline above.
3. Power-cycle the Pi: pull its power, not `reboot`. A USB reset doesn't help, and unplugging the array
   alone doesn't either, since the Pi may not notice. Whether a plain `reboot` is enough is untested.
4. Check `array-hold` opened the array first: the `owner_pid` in both status files should be its
   `arecord` and `aplay`.

### To find the cause

- Flash Seeed's standard `v2.1.1` (16 kHz) firmware and restart the satellite without the hold service.
  If that stalls too, it's this firmware version rather than the rate.
- Try the upstream USB driver (`dtoverlay=dwc2` in `config.txt`). If the stall goes away, it's the Pi's
  controller.
- Check Seeed's repo for newer firmware, and report it there with this evidence.

### Closing it

When a firmware or driver change survives a satellite restart without `array-hold.service`: remove the
service, its rows in [pi.md](pi.md), and this section.

### Fixed along the way

These came up while chasing it and are solved:

- **Undervoltage.** The 7" display and the array were too much for the supplies tried first (a PC's USB
  port, then two others); the display's backlight was the part that tipped it over. A combined 5 V 2.5 A
  supply and cable fixed it.
- **Muffled music.** The stock firmware takes only 16 kHz, which cut music off above 8 kHz. Fixed by the
  48 kHz firmware.
- **The wake word over music.** The array's left channel is its call output, which turned itself down
  about 9 dB whenever anything played, even with the speaker off. The mic now uses the right channel, the
  ASR output, with its gain set to 2 and saved to the array's flash.

## The page laid out too big after a cold boot

**Status:** seen once, not reproduced. Temporary logging is in `satellite/claude-home-display.service`.

### What it looked like

On the boot at 18:02, the Touch Display 2 (rotated to 1280x720) showed the page cropped: the clock cut off
at the left edge (the "6" of the time, "ursday" for the date), the mic button running off the bottom, and
the menu button missing from the top left. The weather at the top right sat exactly where the CSS puts it.
Measured from the screenshot, Chromium had laid the page out at about 1360x760.

### What was checked

- **The screen itself was right:** `wlr-randr` showed DSI-1 at 720x1280, `Transform: 90`, scale 1.
- **No zoom:** Chromium's profile had no zoom set.
- **The saved window size** in Chromium's profile (720 wide, from before the rotation) was the same on
  the cropped boot and on the good boots after it, so it isn't that.
- **Restarting the kiosk fixed it,** and every boot since has been fine.
- **Not a race with the rotation:** on the next boot, with logging on, the screen was rotated 6.2 s
  after cage started and Chromium opened its window at 23 s, about 17 s later.
- **Power is open:** that boot logged 4 undervoltage warnings, the first 6 s in, but the good boot after
  it logged 198, so undervoltage alone doesn't explain it. It hasn't happened in any of the boots since
  the power supply was fixed.

### Related: a blank screen

Before that boot, the screen was found blank. Nothing in the logs explains it: no crash, no out-of-memory
kill, no display error. That boot logged 72 undervoltage warnings, the last ones minutes before. Separately,
Chromium takes 20 to 30 s to draw anything after the kiosk starts, so the screen is blank (white) for that
long after any kiosk restart.

### The logging in place

`claude-home-display.service` runs cage with `-D`, logs `kiosk:` lines around the rotation and when
Chromium starts, and saves a screenshot to `/tmp/kiosk-boot.png` 20 s in (usually still white). It's marked
`shortcut:` in the unit.

### When it happens

Before restarting anything:

1. Screenshot it: `XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 grim shot.png` as `claude-pi`.
2. Read the boot's markers and cage's log: `journalctl -b -o short-precise | grep "kiosk:"` and
   `journalctl -b -o short-precise _COMM=cage`. They're logged under the login session, so
   `-u claude-home-display` doesn't show them.
3. Check power: `vcgencmd get_throttled` and `journalctl -k -b | grep -i undervoltage`.
4. Compare with a good boot, then restart the kiosk: `pkill -f "[/]usr/bin/chromium --kiosk"`.

### Closing it

Once the cause is found and fixed, or after a good stretch of boots on the new power supply, take the
logging out of the unit and delete this section.
