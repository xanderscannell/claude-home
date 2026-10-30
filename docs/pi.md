# Working on the Pi

The Pi runs the satellite, the display kiosk, and raspotify. The brain runs on the PC. This page
collects what the unit files' headers and the scripts say, in one place and in order.

## The machine

- Raspberry Pi 3 B+, Raspberry Pi OS (Debian 13 Trixie, arm64), about 900 MB of RAM. With Chromium
  and the satellite running, about 330 MB is left.
- Raspberry Pi Touch Display 2 (7", 720x1280 portrait panel over DSI, Goodix touch), mounted landscape
  and rotated in software (see step 6), and a ReSpeaker XVF3800 4-mic
  array over USB (ALSA card `Array`) for the mic and, through its 3.5 mm jack, the sound. Wi-Fi is on 5 GHz:
  on 2.4 GHz it shares the radio with Bluetooth.
- User `claude-pi`, at `192.168.0.178`. The brain PC is `192.168.0.172`; both addresses are in the
  unit files, so reserve them in the router.
- SSH by key only: `ssh -i ~/.ssh/claude_home_pi claude-pi@192.168.0.178`.
- The project lives in `~/claude-home/satellite/`. Files are copied over with `scp`, not pulled with
  git; `.gitattributes` keeps LF line endings so they run as-is.

## Deploying a change

| Changed | Do |
|---|---|
| `brain/display/*` | Restart the brain. Screens see a new `version` in `hello` and reload themselves. |
| `satellite/satellite.py` | Copy it to `~/claude-home/satellite/`, then `sudo systemctl restart claude-home-satellite` |
| `satellite/pyproject.toml` or `uv.lock` | Copy both; the unit runs `uv run --frozen`, which syncs on start |
| `kiosk.html` or `claude-home-display.service` | Copy, `sudo systemctl daemon-reload` if the unit changed, then `sudo systemctl restart claude-home-display` |
| `asound.conf` | Copy to `/etc/asound.conf`, then restart the satellite and raspotify |
| `touch-rotate.rules` | Copy to `/etc/udev/rules.d/99-touch-rotate.rules`, `sudo udevadm trigger`, then restart `claude-home-display` |
| `respeaker.rules` | Copy to `/etc/udev/rules.d/99-respeaker.rules`, then `sudo udevadm trigger` |
| `array-hold.service` | Copy, `sudo systemctl daemon-reload`, then reboot: it must open the array before anything else does |

When killing a process over SSH, bracket the first letter (`pkill -f "[p]ython satellite.py"`); a
bare pattern also matches the SSH shell running it.

## Looking at it

- Logs: `journalctl -u claude-home-satellite -f` (likewise `claude-home-display`, `raspotify`, `bt-speaker`,
  `raspotify-watchdog`).
- Screenshot, as `claude-pi`: `XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 grim shot.png`.
- Spotify: `uv run --env-file ../.env python spotify_lab.py check` from `brain/` on the PC (more modes
  in its header). It SSHes in with the same key; set `PI_HOST` if the address changes.

## Known issues

Two problems are worked around or waiting to happen again: the mic array stalling after its audio is
reopened, and the page laid out too big after one cold boot. [issues.md](issues.md) has the evidence
and what to do when either happens.

## Rebuilding it from scratch

1. **OS and user.** Raspberry Pi OS Trixie (64-bit), user `claude-pi`, SSH key from the PC in
   `~/.ssh/authorized_keys`, the address reserved.
2. **Files.** Copy `satellite/` to `~/claude-home/satellite/`.
3. **uv** in `~/.local/bin/uv`. The satellite needs Python 3.11 (openWakeWord's `tflite-runtime` has
   no newer builds), and uv installs it on first run.
4. **Audio.** Flash the array with Seeed's 48 kHz USB firmware, which `asound.conf` expects (the stock 16 kHz
   firmware muffles music): download `respeaker_xvf3800_usb_dfu_firmware_v2.1.1_48k2ch.bin` from
   [`xmos_firmwares/usb`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/usb),
   `sudo apt install dfu-util`, stop anything using the array, then
   `sudo dfu-util -R -e -a 1 -D <file>`. `/proc/asound/Array/stream0` should list `Rates: 48000` for both
   directions. If the array stops showing up, Safe Mode reflashes it: unplug it, hold Mute while plugging it
   back in, and let go when the red LED blinks.
   Install `respeaker.rules` as its header says, so `claude-pi` can reach the array's controls, and set the
   ASR output's gain with Seeed's [`python_control/xvf_host.py`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/python_control)
   (kept in `~/claude-home/xvf/`, run as
   `uv run --no-project --with pyusb --with libusb-package python xvf_host.py <PARAM> [--values ...]`):
   `AEC_ASROUTGAIN --values 2.0`, then `SAVE_CONFIGURATION --values 1` to keep it across power cycles
   (`CLEAR_CONFIGURATION --values 1` goes back to the defaults). The mic is the right channel, the ASR
   output (`AUDIO_MGR_OP_R` `7 3` with `AEC_ASROUTONOFF 1`, Seeed's defaults); asound.conf's header says
   why. Install `array-hold.service` as its header says: with this firmware the array stops streaming once
   its audio is closed and reopened, until it's power-cycled, and the service keeps it open from boot.
   If the satellite keeps exiting with "no audio from the mic", see [issues.md](issues.md).
   Then load the loopback card (it holds the volume controls) as `bt-speaker.service`'s header says,
   then `sudo cp asound.conf /etc/asound.conf`. It names the mic array's card `Array`; check with
   `arecord -l` and change it if yours differs. Raise the array's own volume, which is too quiet to hear
   at its default: `amixer -c Array sset PCM,0 100%`, `amixer -c Array sset PCM,1 100%`, then
   `sudo alsactl store`. The `Master` and `Music` controls appear the first time something plays. (For a
   Bluetooth speaker instead, see `bt-speaker.sh`, `bt-speaker.service`, and asound.conf's header.)
5. **Satellite.** Install `claude-home-satellite.service` as its header says. It needs the brain
   reachable on TCP 8765.
6. **Kiosk.** Install `cage`, `chromium`, `grim`, and `wlr-randr`. Run `python3 blank_cursor.py` as `claude-pi`,
   then install `touch-rotate.rules` and `claude-home-display.service` as the service's header says (this
   disables `getty@tty1`). The display auto-detects (`display_auto_detect=1`); the unit turns the screen
   to landscape and the udev rule turns touch to match.
7. **raspotify.** Install it from [its README](https://github.com/dtcooper/raspotify), then
   append to `/etc/raspotify/conf` (keep the original as `conf.orig`):

   ```
   LIBRESPOT_NAME="Claude Home"
   LIBRESPOT_DEVICE=music
   LIBRESPOT_DEVICE_TYPE=speaker
   LIBRESPOT_INITIAL_VOLUME=100
   ```

   and comment out `LIBRESPOT_DISABLE_CREDENTIAL_CACHE`, `LIBRESPOT_ENABLE_VOLUME_NORMALISATION`, and
   `LIBRESPOT_QUIET`. Why each one matters is in [architecture.md](architecture.md#on-the-pi).
   Then pick "Claude Home" as the speaker in a Spotify app once, so librespot logs in and caches it.
8. **Watchdog.** `chmod +x raspotify-watchdog.sh`, then
   `sudo cp raspotify-watchdog.service /etc/systemd/system/ && sudo systemctl enable --now raspotify-watchdog`.
9. **For `spotify_lab.py`** (optional): add `claude-pi` to the `systemd-journal` group, and add a
   sudoers file `/etc/sudoers.d/raspotify-lab` allowing `ss`, `fuser`, `systemctl restart raspotify`,
   and `tee /etc/raspotify/conf` without a password.
