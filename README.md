# Claude Home

A self-hosted smart speaker, and later a smart display, built on the Claude API and
Home Assistant. It replaces a Google Home / Nest Hub.

**Status:** Phase 1 works on a Raspberry Pi 3 with a USB headset, talking to a brain on a
Windows PC. Most of Phase 3's display runs on the same Pi's 5" touchscreen (see
[Phase 3 progress](#phase-3-progress)). Phase 2 (Home Assistant) hasn't started. See
[docs/architecture.md](docs/architecture.md) for the system design.

## Running it

**Brain** (the PC): Python 3.13 with [uv](https://docs.astral.sh/uv/).

```
cd brain
uv run --env-file ../.env brain.py
```

It serves the satellite and display WebSockets and the display page on port 8765. Set
`BRAIN_HOST=0.0.0.0` so the Pi can reach it, and allow inbound TCP 8765 from the LAN only.
Claude runs through the Agent SDK on the Claude Code login of this machine (or an API key).

**Satellite and display** (the Pi): `satellite/claude-home-satellite.service` runs the mic,
wake word, and playback; `satellite/claude-home-display.service` runs Chromium in kiosk mode on
the touchscreen. Install steps are in each unit file's header. `satellite/asound.conf` goes to
`/etc/asound.conf` (shared playback, a `Master` volume for everything, and a `Music` volume
control for ducking).

**Music:** [raspotify](https://github.com/dtcooper/raspotify) on the Pi makes it a Spotify
Connect speaker; the brain controls it through the Spotify Web API. Open
`http://127.0.0.1:8765/spotify/login` on the brain's machine once to connect an account.

**Settings** go in `.env` at the repo root (never committed):

| Setting | What it's for |
|---|---|
| `GROQ_KEY` | Speech to text (Groq Whisper) |
| `ANTHROPIC_API_KEY` | Optional: bill the Claude API instead of the Claude Code subscription |
| `BRAIN_HOST`, `BRAIN_PORT` | Where the brain listens (default `localhost`, `8765`) |
| `HOME_LOCATION` | City name or `lat,lon`, for weather |
| `CALENDAR_<LABEL>` | One secret iCal address per calendar, e.g. `CALENDAR_WORK`; the label is shown with its events |
| `PHOTOS_DIR` | Folder of photos for the display (default `~/Pictures/Claude Home`) |
| `NIGHT_START`, `NIGHT_END` | When the display becomes a dim night clock (default `22:00`, `07:00`) |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_DEVICE` | A Spotify app's client ID, and the speaker's name in Spotify (default `Claude Home`) |
| `TODOIST_TOKEN`, `TODOIST_SHOPPING_PROJECT` | A Todoist API token (Settings > Integrations > Developer), and the project that is the shopping list (default `Shopping List`) |
| `CLAUDE_HOME_DB` | Where the SQLite file lives (default `brain/home.db`) |

On the Pi, the satellite's unit file sets `BRAIN_URL`, `DEVICE`, `AUDIO_IN`, and `AUDIO_OUT`;
`MIXER_CARD` (default `2`) and `WAKE_THRESHOLD` (default `0.5`) are optional.

**Tests** (from `brain/`): `uv run python test_brain.py`, `test_server.py`, and `test_display.py`
(Playwright, 800x480 with touch); from `satellite/`: `uv run python test_satellite.py`.

**Display work without the Pi or Claude** (from `brain/`): `uv run python fake_brain.py`, then open
http://localhost:8001/ at 800x480. It serves the real `display/` files with canned data and shows the mouse
cursor. Visit `/scene/<name>` to push a screen to the page (`/scene` lists them: replies, every sheet, ringing,
night, and more). To design a screen the brain can't drive yet, add a scene that sends the new message, then
build it in `app.js`.

## Roadmap

Each phase is usable on its own. Later phases add to earlier ones; nothing gets rewritten.

### Phase 1: Voice assistant

Say the wake word, ask anything, get a spoken answer from Claude. No Home Assistant yet.

Build order:

1. **Laptop prototype.** Press Enter to talk: mic -> STT -> Claude -> TTS -> speaker, with Claude running on your subscription through the Agent SDK. Proves the turn loop before any hardware or wake word work.
2. **Sentence streaming.** Speak each sentence as Claude produces it. Add per-stage latency logging.
3. **Wake word + VAD.** Split into satellite and brain processes talking over a WebSocket.
4. **Move to the Pi** with a USB speakerphone. Run under systemd.
5. **Tools:** timers, memory ("remember that..."), weather, web search. Follow-up questions without repeating the wake word.
6. **Switch from the Claude subscription to an API key** (one environment variable) before it runs 24/7.

Done when:

- Median time from end of speech to first audio is under 2 s over a day of real use.
- Timers survive a brain restart.
- Follow-ups work ("what about tomorrow?").
- It has replaced the Google Home for questions and timers for a week.

### Phase 2: Home control

Claude controls Home Assistant, and Home Assistant can make Claude Home speak.

Build order:

1. Brain connects to the Home Assistant MCP Server; Claude sees the exposed entities as tools.
2. **Local fast path:** simple commands ("kitchen lights off") are handled by Home Assistant's built-in intent matcher, with no Claude call.
3. Spoken confirmation for locks, alarms, and garage doors, enforced in code.
4. `/announce` endpoint so Home Assistant automations can make it speak ("the washer is done").
5. Music via Music Assistant, with music ducking while it listens.

Done when:

- Lights, scenes, and media are voice controlled; fast-path commands respond in under 1 s.
- Home Assistant automations can trigger announcements.
- It has fully replaced the Google Home in its room.

### Phase 3: Smart display

The Nest Hub equivalent: a screen, better speakers, and a mic array that can hear over its own music.

Build order:

1. **Hardware build:** Pi 5, touchscreen, ReSpeaker XVF3800, amp and speakers. Verify echo cancellation first, because everything else depends on it.
2. **Display shell:** kiosk browser, listening/thinking/speaking states, ambient clock and weather.
3. **Visual answers:** Claude renders cards on screen while giving a short spoken answer.
4. Timers, now playing, and touch tiles for Home Assistant devices.
5. Doorbell camera popup, photo frame, night mode.

Done when it does everything above and can hear the wake word over its own music at a normal volume.

#### Phase 3 progress

Built on the hardware at hand (the Pi 3, a 5" 800x480 HDMI touchscreen, a USB headset) rather
than the Pi 5 build, with the brain on the PC:

- **Home screen:** a large clock over a slideshow of the household's photos (the accent color
  follows each photo), weather, and a strip of what's next: timers, the next alarm, the next
  calendar event, and the song playing.
- **Conversation:** listening, thinking, and speaking states, what you said, and Claude's reply as
  it's spoken, with a Stop button. Tap the mic to talk without the wake word.
- **Screens Claude or a tap can open:** weather, timers (a draining dial each, with Pause and Stop), alarms, shopping list (the Todoist
  project, by category; tap to check off), calendar, morning briefing, recipe steps, music with controls, volume, and anything else
  Claude wants to show.
- **Alarms and timers** that ring until stopped (alarms can snooze), by touch or by voice: just say
  "stop", no wake word needed.
- **Spotify:** voice control of the Pi as a Spotify Connect speaker, now playing on screen, and
  music that dips while Claude listens or talks.
- **Night mode:** a dim clock on black overnight; a tap brings the home screen back for 30 s.

Not done yet: the Pi 5 hardware with the mic array, Home Assistant tiles, the doorbell camera,
and hearing the wake word over loud music.

### Later (not committed)

Multi-room announcements and intercom, routines beyond the morning briefing, calendar via Home
Assistant, more of Todoist than the shopping list, speaker identification, cheaper audio-only satellites for
other rooms.

## Open questions

These were written before Phase 1; the ones still open change the plan:

1. **Where does Home Assistant run today, and on what hardware?** Decides whether the brain can live next to it.
2. **Is there an always-on machine with a GPU?** If yes, local STT and a better local TTS become viable, which also improves offline behavior.
3. **Cloud STT/TTS or local?** Default is cloud STT (Groq) and local TTS (Piper). Cloud is faster to build and costs a few dollars a month; local is more private.
4. **Which Google Home features do you actually use?** Ranks the Phase 1 tool list.
5. **One room or several?** Several rooms moves the "audio-only satellite" work earlier.
