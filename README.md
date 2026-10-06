# Claude Home

A self-hosted smart speaker, and later a smart display, built on the Claude API and
Home Assistant. It replaces a Google Home / Nest Hub.

**Status:** planning. No code yet. See [docs/architecture.md](docs/architecture.md) for the
tech stack and system design.

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

### Later (not committed)

Multi-room announcements and intercom, routines ("good morning" briefing), calendar and
shopping list via Home Assistant, recipe step-through mode, speaker identification,
cheaper audio-only satellites for other rooms.

## Open questions

These change the plan, so they need answers before Phase 1 starts:

1. **Where does Home Assistant run today, and on what hardware?** Decides whether the brain can live next to it.
2. **Is there an always-on machine with a GPU?** If yes, local STT and a better local TTS become viable, which also improves offline behavior.
3. **Cloud STT/TTS or local?** Default is cloud STT (Groq) and local TTS (Piper). Cloud is faster to build and costs a few dollars a month; local is more private.
4. **Which Google Home features do you actually use?** Ranks the Phase 1 tool list.
5. **One room or several?** Several rooms moves the "audio-only satellite" work earlier.
