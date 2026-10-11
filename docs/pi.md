# Working on the Pi

The Pi runs the satellite, the display kiosk, and raspotify. The brain runs on the PC. This page
collects what the unit files' headers and the scripts say, in one place and in order.

## The machine

- Raspberry Pi 3 B+, Raspberry Pi OS (Debian 13 Trixie, arm64), about 900 MB of RAM. With Chromium
  and the satellite running, about 330 MB is left.
- 5" 800x480 HDMI touchscreen (USB touch), a JBL Charge 5 over Bluetooth for sound, and a USB headset
  (ALSA card `Pro`) for its mic. Wi-Fi is on 5 GHz: on 2.4 GHz it shares the radio with Bluetooth.
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

When killing a process over SSH, bracket the first letter (`pkill -f "[p]ython satellite.py"`); a
bare pattern also matches the SSH shell running it.

## Looking at it

- Logs: `journalctl -u claude-home-satellite -f` (likewise `claude-home-display`, `raspotify`, `bt-speaker`,
  `raspotify-watchdog`).
- Screenshot, as `claude-pi`: `XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 grim shot.png`.
- Spotify: `uv run --env-file ../.env python spotify_lab.py check` from `brain/` on the PC (more modes
  in its header). It SSHes in with the same key; set `PI_HOST` if the address changes.

## Rebuilding it from scratch

1. **OS and user.** Raspberry Pi OS Trixie (64-bit), user `claude-pi`, SSH key from the PC in
   `~/.ssh/authorized_keys`, the address reserved.
2. **Files.** Copy `satellite/` to `~/claude-home/satellite/`.
3. **uv** in `~/.local/bin/uv`. The satellite needs Python 3.11 (openWakeWord's `tflite-runtime` has
   no newer builds), and uv installs it on first run.
4. **Audio.** `sudo apt install bluez-alsa-utils libasound2-plugin-bluez rfkill`, `sudo rfkill unblock
   bluetooth`, and add `claude-pi` to the `bluetooth` group. Pair the speaker as `bt-speaker.sh`'s header
   says, then set up the loopback card and `bt-speaker.service` as its header says. Then
   `sudo cp asound.conf /etc/asound.conf`. It names the headset's card `Pro`; check with `arecord -l`
   and change it if yours differs. The `Master` and `Music` controls appear the first time something plays.
5. **Satellite.** Install `claude-home-satellite.service` as its header says. It needs the brain
   reachable on TCP 8765.
6. **Kiosk.** Install `cage`, `chromium`, and `grim`. Run `python3 blank_cursor.py` as `claude-pi`,
   then install `claude-home-display.service` as its header says (this disables `getty@tty1`).
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
