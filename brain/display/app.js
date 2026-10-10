// Claude Home display: follows the brain over a WebSocket and turns its messages into screens.
// URL parameters: ?device=<name> (default "claude-pi"), ?ws=<url> (default: this server's /display socket).

const params = new URLSearchParams(location.search);
const device = params.get("device") || "claude-pi";
const wsUrl = params.get("ws") || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/display`;

const $ = (id) => document.getElementById(id);
const els = {
  time: $("time"), date: $("date"), mic: $("mic"), menu: $("menu"), weather: $("weather"), strip: $("strip"),
  overlay: $("overlay"), heard: $("heard"), reply: $("reply"), stop: $("stop"), close: $("close-overlay"),
  sheet: $("sheet"), sheetTitle: $("sheet-title"), sheetBody: $("sheet-body"),
  headerTabs: $("header-tabs"), headerHome: $("header-home"), sheetTime: $("sheet-time"), tabs: $("tabs"), tabLine: $("tab-line"), headerTime: $("header-time"),
  tabsWrap: document.querySelector(".tabs-wrap"),
  edge: $("sheet-edge"), under: $("sheet-under"), underTitle: $("under-title"), underBody: $("under-body"),
  swipeDim: $("swipe-dim"), dash: $("sheet-dash"), dashPhoto: $("dash-photo"), dashPic: $("dash-pic"), dashboard: $("dashboard"), dashEdge: $("dash-edge"), night: $("night"),
  volEdge: $("vol-edge"), volBar: $("vol-bar"), volSlider: $("vol-slider"), volLevel: $("vol-level"), volNone: $("vol-none"),
  ringing: $("ringing"), ringingTime: $("ringing-time"), ringingLabel: $("ringing-label"),
  ringingStop: $("ringing-stop"), snooze: $("snooze"),
  offline: $("offline"), nightTime: $("night-time"),
  camera: $("camera"), cameraVideo: $("camera-video"), cameraName: $("camera-name"), cameraNote: $("camera-note"),
  cameraClose: $("camera-close"),
};

// --- Connection -------------------------------------------------------------------------------

let socket = null;
let retryDelay = 1000;
let offlineTimer = null;

function connect() {
  socket = new WebSocket(`${wsUrl}?device=${encodeURIComponent(device)}`);
  socket.onopen = () => {
    retryDelay = 1000;
    clearTimeout(offlineTimer);
    offlineTimer = null;
    els.offline.hidden = true;
    document.body.classList.remove("is-offline");
    fetchRow(ROW.map((r) => r.kind));  // the row's pages ready to swipe to, from the first swipe
  };
  socket.onmessage = (e) => handle(JSON.parse(e.data));
  socket.onclose = () => {
    if (!offlineTimer) {
      offlineTimer = setTimeout(() => {
        els.offline.hidden = false;
        document.body.classList.add("is-offline");  // the mic can't do anything until the brain is back
      }, 5000);
    }
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 5000);
  };
}

function send(msg) {
  if (msg.type === "open") lastOpen = msg;
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(msg));
}

// --- Messages from the brain ------------------------------------------------------------------

let pageVersion = null;

const handlers = {
  // A brain restarted with different display code: load it (the kiosk would otherwise keep the old page).
  // The first hello also starts the slideshow: only then is it known whether it's night (no photos at night).
  hello: (m) => {
    nightHours = m.night || null;
    applyNight();
    if (pageVersion === null) {
      pageVersion = m.version;
      startPhotos();
    } else if (m.version !== pageVersion) location.reload();
  },
  state: (m) => setState(m.state),
  heard: (m) => { els.heard.textContent = m.text; },
  reply: (m) => addReply(m.text),
  ambient: (m) => {
    ambient = m;
    renderWeather();
    renderStrip();
    if (sheetCard && sheetCard.kind === "timers") renderSheet();  // keep an open timers sheet live
  },
  card: (m) => (m.card.kind === "volume" ? openVolume(m.card.level) : openSheet(m.card)),  // Claude set the volume
  page: (m) => keepRowCard(m.card),
  ringing: (m) => showRinging(m.ringing),
  volume: (m) => showVolume(m.level),
  now_playing: (m) => showNowPlaying(m.track),
  camera: (m) => (m.entity ? showCamera(m) : closeCamera()),
};

function handle(msg) {
  (handlers[msg.type] || (() => {}))(msg);
}

// --- Clock ------------------------------------------------------------------------------------

function renderClock() {
  const now = new Date();
  els.time.textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).replace(/\s?[AP]M$/i, "");
  els.time.dateTime = now.toISOString();
  els.date.textContent = now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  els.nightTime.textContent = els.headerTime.textContent = els.time.textContent;
  applyNight();
  renderStrip();  // calendar events drop off the strip when they end
}

// --- Night ------------------------------------------------------------------------------------
// Between hello.night's start and end ("22:00" to "07:00") home becomes a dim clock on black. A touch
// brings the normal home back for 30 s, and each touch while it's back keeps it for another 30 s.

const NIGHT_WAKE_MS = 30000;
let nightHours = null;
let wokenUntil = 0;
let forcedNight = false;  // put to night by a swipe from the dashboard's edge, until the next touch
let forcedUntil = Infinity;  // or until the night hours next end
let nightTimer = null;

function minutes(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
}

function inNightHours(now = new Date()) {
  if (!nightHours) return false;
  const m = now.getHours() * 60 + now.getMinutes(), start = minutes(nightHours.start), end = minutes(nightHours.end);
  return start > end ? m >= start || m < end : m >= start && m < end;  // usually it spans midnight
}

function applyNight() {
  if (forcedNight && Date.now() >= forcedUntil) forcedNight = false;  // morning
  const night = (inNightHours() || forcedNight) && Date.now() >= wokenUntil;
  const was = document.body.classList.contains("is-night");
  document.body.classList.toggle("is-night", night);
  if (was && !night && !frontLayer.classList.contains("shown")) nextPhoto();  // morning, or a touch: bring the room back
}

function wakeFromNight() {
  if (forcedNight && document.body.classList.contains("is-night")) {  // a forced night ends with a touch, for good
    forcedNight = false;
    applyNight();
    return;
  }
  if (!inNightHours()) return;
  wokenUntil = Date.now() + NIGHT_WAKE_MS;
  clearTimeout(nightTimer);
  nightTimer = setTimeout(applyNight, NIGHT_WAKE_MS);
  applyNight();
}

function startClock() {
  renderClock();
  const now = new Date();
  const toNextMinute = 60000 - (now.getSeconds() * 1000 + now.getMilliseconds());
  setTimeout(() => { renderClock(); setInterval(renderClock, 60000); }, toNextMinute + 50);
}

// --- Photos -----------------------------------------------------------------------------------

const PHOTO_SECONDS = 30;
let photoList = [];
let photoIndex = 0;
let frontLayer = $("photo-a");

async function loadPhotoList() {
  try {
    const r = await fetch("/photos", { cache: "no-store" });
    photoList = r.ok ? await r.json() : [];
  } catch {
    photoList = [];
  }
  photoIndex = 0;
}

// Load the next photo off-screen, then crossfade to it and take its accent color.
async function nextPhoto() {
  if (document.body.classList.contains("is-night")) return;
  if (photoIndex >= photoList.length) await loadPhotoList();  // each lap reshuffles and picks up new photos
  if (!photoList.length) return;
  const img = new Image();
  img.onload = () => {
    const back = frontLayer === $("photo-a") ? $("photo-b") : $("photo-a");
    back.style.backgroundImage = `url("${img.src}")`;
    back.classList.add("shown");
    frontLayer.classList.remove("shown");
    frontLayer = back;
    setAccent(photoAccent(img));
  };
  img.src = `/photos/${encodeURIComponent(photoList[photoIndex++])}`;
}

function setAccent(color) {
  if (color) document.documentElement.style.setProperty("--accent", color);
  else document.documentElement.style.removeProperty("--accent");
}

// The photo's most colorful mid-tone, clamped so it always reads as an accent against night and frost.
function photoAccent(img) {
  let px;
  try {
    const c = document.createElement("canvas");
    c.width = c.height = 8;
    const g = c.getContext("2d", { willReadFrequently: true });
    g.drawImage(img, 0, 0, 8, 8);
    px = g.getImageData(0, 0, 8, 8).data;
  } catch {
    return null;
  }
  let best = null;
  for (let i = 0; i < px.length; i += 4) {
    const [h, s, l] = hsl(px[i], px[i + 1], px[i + 2]);
    if (l < 0.18 || l > 0.88) continue;
    const score = s * (1 - Math.abs(l - 0.55));
    if (!best || score > best.score) best = { h, s, l, score };
  }
  if (!best || best.s < 0.15) return null;  // a grey photo: keep marigold
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  return `hsl(${Math.round(best.h)} ${Math.round(clamp(best.s, 0.45, 0.75) * 100)}% ${Math.round(clamp(best.l, 0.55, 0.7) * 100)}%)`;
}

function hsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2, d = max - min;
  if (!d) return [0, 0, l];
  const s = d / (1 - Math.abs(2 * l - 1));
  const h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [(h * 60 + 360) % 360, s, l];
}

function startPhotos() {
  nextPhoto();
  setInterval(nextPhoto, PHOTO_SECONDS * 1000);
}

// --- Home: weather and the glanceable strip ---------------------------------------------------

let ambient = { weather: null, timers: [] };
let ticker = null;

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}

const sentence = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

function renderWeather() {
  const w = ambient.weather;
  els.weather.replaceChildren();
  if (!w) return;
  const button = el("button", "weather-button");
  button.type = "button";
  button.setAttribute("aria-label", `Weather: ${w.now.temp} degrees, ${w.now.condition}`);
  button.append(el("span", "temp", `${w.now.temp}°`),
                el("span", "condition", `${sentence(w.now.condition)}, high ${w.days[0].high}`));
  button.addEventListener("click", () => send({ type: "open", sheet: "weather" }));
  els.weather.append(button);
}

// "06:30" -> 6:30 with a small "am", in an element of the given class
function alarmTime(hhmm, className) {
  const [h, m] = hhmm.split(":").map(Number);
  const out = el("span", className, `${h % 12 || 12}:${String(m).padStart(2, "0")}`);
  out.append(el("span", "ampm", h < 12 ? "am" : "pm"));
  return out;
}

// An event's start (epoch seconds) the same way: 3:00 with a small "pm"
function eventTime(epoch, className) {
  const at = new Date(epoch * 1000);
  return alarmTime(`${at.getHours()}:${String(at.getMinutes()).padStart(2, "0")}`, className);
}

// When an event happens, for a time column: "All day", "Now" (under way), or its start time
function eventWhen(e, now, className) {
  if (e.all_day) return el("span", `${className} all-day`, "All day");
  if (e.start <= now) return el("span", `${className} now`, "Now");
  return eventTime(e.start, className);
}

// "Today", "Tomorrow", or "Friday, October 9"
function dayTitle(day) {
  const days = Math.round((new Date(day).setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Tomorrow";
  return day.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
}

// "" today, " tomorrow", or " Monday" for when an alarm next rings
function dayWord(epoch) {
  const at = new Date(epoch * 1000), today = new Date();
  const days = Math.round((new Date(at).setHours(0, 0, 0, 0) - new Date(today).setHours(0, 0, 0, 0)) / 86400000);
  if (days <= 0) return "";
  if (days === 1) return " tomorrow";
  return ` ${at.toLocaleDateString([], { weekday: "long" })}`;
}

const WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function repeatText(days) {
  const named = { "": "Once", "01234": "Weekdays", "56": "Weekends", "0123456": "Every day" };
  if (days in named) return named[days];
  return [...days].map((d) => WEEKDAY_NAMES[Number(d)].slice(0, 3)).join(", ");
}

// m:ss under an hour, h:mm:ss over
function countdown(seconds) {
  const s = Math.max(0, Math.ceil(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

// A timer counts down to ends_at while it runs; paused, it holds paused_left
const paused = (t) => t.paused_left !== null && t.paused_left !== undefined;
const timeLeft = (t, now) => (paused(t) ? t.paused_left : t.ends_at - now);
const liveTimers = (timers, now) => timers.filter((t) => timeLeft(t, now) > 0);

// "1 hr 30 min", "45 sec": a timer's length, short enough for inside a dial
function shortLength(seconds) {
  const s = Math.round(seconds);
  return [[Math.floor(s / 3600), "hr"], [Math.floor((s % 3600) / 60), "min"], [s % 60, "sec"]]
    .filter(([n]) => n).map(([n, unit]) => `${n} ${unit}`).join(" ");
}

// What a timer is called: its label, else its length ("5 min timer"), else just "Timer"
const timerName = (t) => sentence(t.label) || (t.duration ? `${shortLength(t.duration)} timer` : "Timer");

// A ticking countdown that holds still. Fraunces has no tabular digits ("1:11" is narrower than "0:00"), so each
// digit gets a cell the width of the widest, 0 (1ch), centered in it: the number keeps one width as it ticks, so it
// can sit centered (in a dial) or beside a label (on the strip) without shifting every second.
function setCountdown(node, seconds) {
  node.replaceChildren(...[...countdown(seconds)].map((c) => (c === ":" ? c : el("span", "digit", c))));
}

function steadyCountdown(className, seconds) {
  const num = el("span", className);
  setCountdown(num, seconds);
  return num;
}

function renderStrip() {
  els.strip.replaceChildren();
  const now = Date.now() / 1000;
  const live = liveTimers(ambient.timers, now).sort((a, b) => timeLeft(a, now) - timeLeft(b, now));
  for (const t of live) {
    const item = el("button", paused(t) ? "item timer paused" : "item timer");
    item.type = "button";
    if (!paused(t)) item.dataset.endsAt = t.ends_at;
    item.append(steadyCountdown("num", timeLeft(t, now)), el("span", "label", timerName(t)));
    item.addEventListener("click", () => send({ type: "open", sheet: "timers" }));
    els.strip.append(item);
  }
  const running = live.filter((t) => !paused(t));
  const a = ambient.next_alarm;
  if (a) {
    const item = el("button", "item alarm");
    item.type = "button";
    item.append(alarmTime(a.time, "num"), el("span", "label", `${sentence(a.label) || "Alarm"}${dayWord(a.next_ring)}`));
    item.addEventListener("click", () => send({ type: "open", sheet: "alarms" }));
    els.strip.append(item);
  }
  // The next thing on the calendar today: the next timed event (or the one under way), else an all-day one
  const today = (ambient.agenda || []).filter((e) => e.end > now);
  const next = today.find((e) => !e.all_day) || today[0];
  if (next) {
    const item = el("button", "item event");
    item.type = "button";
    if (!next.all_day) item.append(next.start <= now ? el("span", "num", "Now") : eventTime(next.start, "num"));
    item.append(el("span", "label", next.title));
    item.addEventListener("click", () => send({ type: "open", sheet: "agenda" }));
    els.strip.append(item);
  }
  // Music while it plays: the album art small and square, then the title
  if (nowPlaying && nowPlaying.is_playing) {
    const item = el("button", "item music");
    item.type = "button";
    if (nowPlaying.art) {
      const thumb = el("img", "np-thumb");
      Object.assign(thumb, { src: nowPlaying.art, alt: "" });
      item.append(thumb);
    }
    item.append(el("span", "label", nowPlaying.title));
    item.addEventListener("click", () => openSheet({ kind: "music" }));
    els.strip.append(item);
  }
  // Tick once a second only while something is counting down (the Pi's CPU is better spent idle).
  if (running.length && !ticker) ticker = setInterval(tick, 1000);
  if (!running.length && ticker) { clearInterval(ticker); ticker = null; }
}

function tick() {
  const now = Date.now() / 1000;
  for (const item of els.strip.querySelectorAll(".timer[data-ends-at]")) {
    const left = Number(item.dataset.endsAt) - now;
    if (left <= 0) { renderStrip(); return; }  // finished: drop it (the brain rings and confirms)
    setCountdown(item.querySelector(".num"), left);
  }
  for (const c of els.sheetBody.querySelectorAll("[data-ends-at]")) {
    setCountdown(c, Number(c.dataset.endsAt) - now);
  }
  for (const arc of els.sheetBody.querySelectorAll(".dial-arc[data-arc-end]")) drain(arc, Number(arc.dataset.arcEnd) - now);
}

// --- Sheets -----------------------------------------------------------------------------------

const SHEET_IDLE_MS = 45000;
let sheetCard = null;
let sheetIdle = null;
// The row of top-level pages, swiped between from the dashboard in this order, and the menu's entries. kind: the
// sheet; icon: its ICONS name; local: the page draws it from what it already knows (no card from the brain needed).
const ROW = [
  { kind: "house", label: "Home", icon: "house" },
  { kind: "timers", label: "Timers", icon: "timer", local: true },  // from the ambient timers
  { kind: "alarms", label: "Alarms", icon: "alarm" },
  { kind: "shopping", label: "Shopping list", icon: "list" },
  { kind: "music", label: "Music", icon: "music", local: true },
];
const rowAt = (kind) => ROW.findIndex((r) => r.kind === kind);

// The pages a subpage opened over (Home, then Rooms, then a room), for going back. A card the brain sent in answer
// to an open keeps that request, so going back can ask again and show it fresh rather than as it was. A row page is
// top level: it opens over nothing, so it has no back. Anything else opened with no sheet up (a card Claude puts up,
// the menu) opens over the dashboard itself, and goes back to it.
const DASHBOARD = { kind: "dashboard" };
let sheetStack = [];
let lastOpen = null;
const rowCards = {};  // the last card of each row page the brain serves, to draw it while it's swiped into view

// back: going back to card, so the stack is already as it should be
function openSheet(card, back = false) {
  if (lastOpen && lastOpen.sheet === card.kind) [card.request, lastOpen] = [lastOpen, null];
  if (back) {
    // as it is
  } else if (rowAt(card.kind) >= 0) sheetStack = [];
  else if (!sheetCard) sheetStack = [DASHBOARD];
  else if (sheetCard.kind !== card.kind) sheetStack.push(sheetCard);
  else if (!card.request) card.request = sheetCard.request;  // the same kind is a refresh of it
  if (rowAt(card.kind) >= 0 && !card.placeholder) rowCards[card.kind] = card;
  els.edge.hidden = !sheetStack.length;
  sheetCard = card;
  // A timers card can beat the next ambient update by up to half a second; it is just as fresh, so use it.
  if (card.kind === "timers" && card.timers) ambient = { ...ambient, timers: card.timers };
  const wasHidden = els.sheet.hidden;
  els.sheet.hidden = false;  // before rendering, so long text can be measured and fitted
  renderSheet();
  // Slide up from the bottom: the browser has to see it there first, or it just appears in place (a landing from a
  // swipe has already put it in place, so this changes nothing there)
  if (wasHidden) void els.sheet.offsetWidth;
  requestAnimationFrame(() => els.sheet.classList.add("shown"));
  touchSheet();
  syncHeader();
}

function goBack() {
  const prev = sheetStack.pop();
  if (prev === DASHBOARD) {
    closeSheet();
    return;
  }
  if (sheetCard.kind === "recipe") send({ type: "recipe", action: "close" });  // ends recipe mode
  openSheet(prev, true);
  if (prev.request) send(prev.request);
}

// A row page as it can be drawn right now: the page's own for the local ones, else the brain's last card for it
// (fetched when the page connects, and again around each landing), else just its title until that comes back.
function rowCard(kind) {
  return ROW[rowAt(kind)].local ? { kind } : rowCards[kind] || { kind, placeholder: true };
}

// Land on a row page from a swipe: show it at once from what's known, and ask the brain for it fresh, and for the
// pages either side of it, so the next swipe shows them as they are now
function landRow(kind) {
  sheetCard = null;
  els.sheet.classList.add("shown");  // in place before it's shown (from the dashboard): no slide up from the bottom
  openSheet(rowCard(kind));
  if (!ROW[rowAt(kind)].local) send({ type: "open", sheet: kind });
  const i = rowAt(kind);
  fetchRow([ROW[i - 1], ROW[i + 1]].filter(Boolean).map((r) => r.kind));
}

// Ask the brain for row pages to have ready to swipe to (the local ones the page draws itself). It answers each with
// a page, which is kept, not opened.
function fetchRow(kinds) {
  for (const kind of kinds) if (!ROW[rowAt(kind)].local) send({ type: "fetch", sheet: kind });
}

// A row page from the brain, fetched or opened: kept to draw when it's swiped to. One being swiped into view right
// now, or showing only its title while it waits, is drawn with it at once.
function keepRowCard(card) {
  if (rowAt(card.kind) < 0) return;
  rowCards[card.kind] = card;
  if (slide && els.under.dataset.kind === card.kind && !els.under.hidden) showUnder(card);
  if (sheetCard && sheetCard.kind === card.kind && sheetCard.placeholder) openSheet(card);
}

// Where the screen is in the row: 0 the dashboard, 1 Home, and so on; null where the row doesn't apply (a subpage,
// a card Claude put up, the conversation, a ringing alarm, the camera, night)
function rowIndex() {
  if (document.body.classList.contains("is-night") || !els.overlay.hidden || !els.ringing.hidden || !els.camera.hidden) return null;
  if (!sheetCard) return 0;
  const i = rowAt(sheetCard.kind);
  return i >= 0 && !sheetStack.length ? i + 1 : null;
}

// --- Header -----------------------------------------------------------------------------------
// Along the row: every row page's title (the dashboard's first), the one you're on at the left, underlined. Mid-swipe
// the titles slide so the one coming moves under the underline, which stays put and eases to its width.
const TABS = [DASHBOARD.kind, ...ROW.map((r) => r.kind)];
els.tabs.append(...["Dashboard", ...ROW.map((r) => r.label)].map((label) => el("span", "tab", label)));

function setTabs(pos) {  // pos: where along the row, 0 the dashboard; between two whole numbers mid-swipe
  const tabs = els.tabs.children, i = Math.min(Math.floor(pos), tabs.length - 1), f = pos - i;
  const a = tabs[i], b = tabs[Math.min(i + 1, tabs.length - 1)], mix = (x, y) => x + (y - x) * f;
  els.tabs.style.transform = `translateX(${tabs[0].offsetLeft - mix(a.offsetLeft, b.offsetLeft)}px)`;
  els.tabLine.style.transform = `scaleX(${mix(a.offsetWidth, b.offsetWidth)})`;
  [...tabs].forEach((t, k) => { t.style.opacity = String(1 - Math.min(1, Math.abs(k - pos)) * 0.6); });
}

// The header for what's showing: there over any sheet, and over the dashboard while it's drawn as one; the titles
// along the row (where a row page's own title gives way to them), else just the time over the sheet's own title
function syncHeader() {
  const framed = els.dashboard.classList.contains("framed");
  const row = sheetCard ? (rowAt(sheetCard.kind) >= 0 && !sheetStack.length ? rowAt(sheetCard.kind) + 1 : null)
                        : framed ? 0 : null;
  for (const h of [els.headerTabs, els.headerHome]) h.classList.toggle("shown", Boolean(sheetCard) || framed);
  els.sheet.classList.toggle("row", row !== null);
  els.tabsWrap.style.opacity = row === null ? "0" : "1";
  if (row !== null) setTabs(row);
}

// --- Swiping ----------------------------------------------------------------------------------
// Along the row, pages sit side by side: the page you're on and the next (or previous) one move together with the
// finger, like a strip of film; the dashboard joins them drawn as a sheet too (showDash). Back from a subpage is different, a page lifted off the one under it: it slides off
// to the right over the page it opened over, which drifts a fifth of the way in and brightens as it's uncovered.
// Either way, let go past a third of the screen (or flick) and the move happens; short of that it springs back. Past
// the end of the row the page gives a little and springs back.
const SWIPE_AT = 1 / 3;
const SWIPE_FLICK = 0.5;  // px per ms
const SWIPE_LOCK_PX = 10;  // this far before it's known whether it's sideways (a swipe) or not (a scroll, a tap)
const RUBBER = 0.15;  // past the end of the row the page moves this much of the finger's way
const SETTLE_MS = 220;  // .settling's transition
let gesture = null;  // a finger that is or may become a swipe: {id, x, y, t, dx, v, row}
// The swipe under way: {mode: "side" | "over" | "rubber", page (the one you're on), next (the one coming),
// ahead (1: the finger moves right to make the move, -1: left), land}
let slide = null;

function place(q) {  // q: how far the move has gone, 0 to 1; for rubber, px the page has given
  const s = slide;
  if (s.mode === "rubber") {
    s.page.style.transform = `translateX(${q}px)`;
  } else if (s.mode === "away") {
    s.page.style.transform = `translateX(${q * 100}%)`;  // the dashboard off to the right, the night screen under it
  } else if (s.mode === "side") {
    s.page.style.transform = `translateX(${s.ahead * q * 100}%)`;
    s.next.style.transform = `translateX(${s.ahead * (q - 1) * 100}%)`;
    setTabs(s.row - s.ahead * q);  // the titles along with the pages
  } else {
    s.page.style.transform = `translateX(${q * 100}%)`;
    if (s.next) s.next.style.transform = `translateX(${(q - 1) * 20}%)`;  // (none: the dashboard, which stays)
    els.swipeDim.style.opacity = String(1 - q);
    if (s.toRow !== null) els.tabsWrap.style.opacity = String(q);  // back to a row page: its titles come up
  }
}

function showUnder(card) {
  els.under.dataset.kind = card.kind;
  els.under.classList.toggle("row", rowAt(card.kind) >= 0);
  const [title, nodes] = renderCard(card);
  els.underTitle.textContent = title;
  els.underBody.replaceChildren(...nodes);
  els.under.hidden = false;
}

// The dashboard as a sheet while it's swiped (its title, and the photo showing now as the one thing on it). It lives
// inside the dashboard, so it moves with it. animate: a swipe starting on the dashboard, where everything but the
// photo fades away and the photo shrinks from the whole screen into its frame; one coming back to the dashboard starts
// off screen, so it's already framed. hideDash(animate) is the reverse, landing back on the real dashboard.
const DASH_MS = 260;  // the transitions in style.css
let dashHiding = null;

function showDash(animate) {
  clearTimeout(dashHiding);
  els.dashPic.style.backgroundImage = frontLayer.classList.contains("shown") ? frontLayer.style.backgroundImage : "";
  els.dash.hidden = false;
  // The frame, from layout offsets (a transform under way doesn't skew them). Framed, the screen-sized picture is
  // scaled by k to cover it. Full, the frame is moved and stretched back over the whole screen and the picture
  // squeezed by the inverse, so it shows exactly as the photo behind it does.
  const f = els.dashPhoto, w = f.offsetWidth, h = f.offsetHeight;
  let x = 0, y = 0;  // the frame's place on the screen (the dashboard sheet covers it from 0, 0)
  for (let n = f; n && n !== els.dash; n = n.offsetParent) [x, y] = [x + n.offsetLeft, y + n.offsetTop];
  const cx = x + w / 2, cy = y + h / 2;
  const k = Math.max(w / innerWidth, h / innerHeight);
  els.dash.style.setProperty("--k", String(k));
  // The clock and the rest shrink toward the frame too, but to fit inside it (the picture overflows it, cropped).
  // The scrim shades the picture, so it shrinks as the picture does: its sides on the frame's, and what overflows
  // lies on the sheet's background, the same night it's tinted with.
  const toFrame = `translate(${cx - innerWidth / 2}px, ${cy - innerHeight / 2}px)`;
  els.dashboard.style.setProperty("--ui-framed", `${toFrame} scale(${Math.min(w / innerWidth, h / innerHeight)})`);
  els.dashboard.style.setProperty("--scrim-framed", `${toFrame} scale(${k})`);
  els.dash.style.setProperty("--frame-full",
    `translate(${innerWidth / 2 - cx}px, ${innerHeight / 2 - cy}px) scale(${innerWidth / w}, ${innerHeight / h})`);
  els.dash.style.setProperty("--pic-full", `translate(-50%, -50%) scale(${w / innerWidth}, ${h / innerHeight})`);
  if (animate) {
    els.dash.classList.add("full", "snap");  // jump to full at once (measuring settled it framed),
    void els.dash.offsetWidth;
  }
  els.dash.classList.remove("full", "snap");  // then shrink into the frame
  setFramed(true, animate);
}

// The clock and the rest faded and shrunk (framed) or back as they were; at once unless animate, since out of sight
// (a dashboard coming back on from off screen, or one left behind a sheet) they shouldn't play it
function setFramed(on, animate) {
  els.dashboard.classList.toggle("snap", !animate);
  els.dashboard.classList.toggle("framed", on);
  void els.dashboard.offsetWidth;
  els.dashboard.classList.remove("snap");
  syncHeader();
}

function hideDash(animate) {
  clearTimeout(dashHiding);
  setFramed(false, animate);
  if (!animate) {
    els.dash.hidden = true;
    return;
  }
  els.dash.classList.add("full");
  dashHiding = setTimeout(() => { els.dash.hidden = true; }, DASH_MS);
}

const layers = () => [slide.page, slide.next,
  ...(slide.mode === "side" ? [els.tabs, els.tabLine] : []),
  ...(slide.mode === "over" ? [els.swipeDim, els.headerTabs, els.headerHome, slide.toRow !== null ? els.tabsWrap : null]
                            : [])]
  .filter(Boolean);

// Start moving: which pages move, and what landing does. False: nothing to swipe to.
function beginSlide(back) {
  if (gesture.night) {  // the dashboard's edge: away to the night clock
    els.night.classList.add("peek");
    els.night.style.zIndex = "1";
    els.dashboard.style.zIndex = "2";
    slide = { mode: "away", page: els.dashboard, next: els.night, ahead: 1, land: forceNight };
    for (const l of layers()) l.classList.add("dragging");
    place(0);
    return true;
  }
  const i = gesture.row;
  const side = (next, land) => {
    if (!i || next === els.dashboard) showDash(!i);
    return { mode: "side", page: i ? els.sheet : els.dashboard, next, ahead: back ? 1 : -1, land, row: i };
  };
  if (i === undefined) {  // a subpage's edge: back to the page it opened over (the dashboard: nothing to draw)
    const to = sheetStack.at(-1), toRow = to !== DASHBOARD && rowAt(to.kind) >= 0 ? rowAt(to.kind) + 1 : null;
    if (to !== DASHBOARD) showUnder(to);
    if (toRow !== null) setTabs(toRow);
    slide = { mode: "over", page: els.sheet, next: to === DASHBOARD ? null : els.under, ahead: 1, land: goBack, toRow };
    // From the bottom: the page underneath, its header, the dim over both, the page sliding off, carrying its own copy
    // of the time (the dashboard underneath has its own clock, and no header)
    els.under.style.zIndex = "1";
    els.headerTabs.style.zIndex = els.headerHome.style.zIndex = "2";
    if (to === DASHBOARD) els.headerHome.style.opacity = "0";
    els.sheetTime.textContent = els.headerTime.textContent;
    els.sheet.classList.add("carry-time");
    els.swipeDim.style.zIndex = "3";
    els.sheet.style.zIndex = "4";
    els.swipeDim.hidden = false;
  } else if (back && i === 1) {
    slide = side(els.dashboard, closeSheet);
  } else if (back && i >= 2) {
    showUnder(rowCard(ROW[i - 2].kind));
    slide = side(els.under, () => landRow(ROW[i - 2].kind));
  } else if (!back && i < ROW.length) {
    showUnder(rowCard(ROW[i].kind));
    slide = side(els.under, () => landRow(ROW[i].kind));
  } else if (!back) {  // the last page: nothing further
    slide = { mode: "rubber", page: els.sheet };
  } else return false;  // the dashboard: nothing to its left
  for (const l of layers()) l.classList.add("dragging");
  place(0);
  return true;
}

function moveSlide() {
  const s = slide;
  place(s.mode === "rubber" ? gesture.dx * RUBBER : Math.min(1, Math.max(0, (s.ahead * gesture.dx) / innerWidth)));
}

// dashLands: the swipe ended on the dashboard, so its sheet opens back out into the real one
function endSlide(dashLands) {
  for (const l of layers()) l.style.transform = l.style.zIndex = "";
  void els.sheet.offsetWidth;  // apply that before transitions come back on, or a page would slide in from the side
  for (const l of layers()) l.classList.remove("dragging", "settling");
  hideDash(dashLands);
  els.swipeDim.hidden = els.under.hidden = true;
  els.underBody.replaceChildren();
  els.sheet.classList.remove("carry-time");
  els.night.classList.remove("peek");  // (landed: night is on, and its clock comes up)
  els.headerHome.style.opacity = "";
  slide = null;
  syncHeader();
}

function letGo() {
  const s = slide, g = gesture;
  gesture = null;
  if (!s) return;
  // A mouse sends a click after a drag (a finger's drag sends none): swallow it
  const swallow = (e) => { e.stopPropagation(); e.preventDefault(); };
  document.addEventListener("click", swallow, true);
  setTimeout(() => document.removeEventListener("click", swallow, true), 0);
  const go = s.mode !== "rubber" &&
    ((s.ahead * g.dx) / innerWidth > SWIPE_AT || (s.ahead * g.v > SWIPE_FLICK && s.ahead * g.dx > 40));
  for (const l of layers()) l.classList.replace("dragging", "settling");
  place(go ? 1 : 0);
  const dashLands = go ? s.next === els.dashboard : s.page === els.dashboard;
  setTimeout(() => {
    if (go) {
      for (const l of layers()) l.classList.replace("settling", "dragging");  // land without sliding in again
      s.land();
    }
    endSlide(dashLands);
  }, SETTLE_MS);
}

// A finger down on a subpage's edge is a back swipe from the start. Anywhere on the dashboard or a row page it may
// be one: it is once it has gone further sideways than up or down. One that starts on something that drags sideways
// itself (a slider, a row that scrolls sideways) is left to it.
function scrollsSideways(node) {
  for (let n = node; n && n !== document.body; n = n.parentElement) {
    if (n.scrollWidth > n.clientWidth + 1 && /auto|scroll/.test(getComputedStyle(n).overflowX)) return true;
  }
  return false;
}

document.addEventListener("pointerdown", (e) => {
  if (gesture || slide || !e.isPrimary || e.target === els.volEdge || els.volBar.contains(e.target)) return;
  const start = { id: e.pointerId, x: e.clientX, y: e.clientY, t: e.timeStamp, dx: 0, v: 0 };
  if (e.target === els.edge && sheetStack.length) {
    gesture = start;
    els.edge.setPointerCapture(e.pointerId);
    beginSlide(true);
    return;
  }
  if (e.target === els.dashEdge && rowIndex() === 0) {
    gesture = { ...start, night: true };
    els.dashEdge.setPointerCapture(e.pointerId);
    beginSlide(true);
    return;
  }
  const row = rowIndex();
  if (row === null || e.target.closest("input") || scrollsSideways(e.target)) return;
  gesture = { ...start, row };
});
document.addEventListener("pointermove", (e) => {
  const g = gesture;
  if (!g || e.pointerId !== g.id) return;
  const dx = e.clientX - g.x, dy = e.clientY - g.y;
  g.v = (dx - g.dx) / Math.max(1, e.timeStamp - g.t);
  Object.assign(g, { dx, t: e.timeStamp });
  if (!slide) {
    if (Math.abs(dx) < SWIPE_LOCK_PX && Math.abs(dy) < SWIPE_LOCK_PX) return;
    if (Math.abs(dy) >= Math.abs(dx) || !beginSlide(dx > 0)) {  // up or down is a scroll; or nothing that way
      gesture = null;
      return;
    }
    e.target.setPointerCapture?.(e.pointerId);
  }
  moveSlide();
});
for (const type of ["pointerup", "pointercancel"]) {
  document.addEventListener(type, (e) => { if (gesture && e.pointerId === gesture.id) letGo(); });
}

function closeSheet() {
  clearTimeout(sheetIdle);
  if (sheetCard && sheetCard.kind === "recipe") send({ type: "recipe", action: "close" });  // ends recipe mode
  sheetCard = null;
  sheetStack = [];
  els.sheet.classList.remove("shown");
  els.sheet.hidden = true;
  els.sheetBody.replaceChildren();
  syncHeader();
}

// Untouched for 45 s, a sheet goes away and the room comes back. A recipe waits 30 minutes: a step can
// simmer that long without anyone touching the screen.
const RECIPE_IDLE_MS = 30 * 60000;

function touchSheet() {
  clearTimeout(sheetIdle);
  sheetIdle = setTimeout(closeSheet, sheetCard && sheetCard.kind === "recipe" ? RECIPE_IDLE_MS : SHEET_IDLE_MS);
}

// Previous and Next on the recipe: move at once, and tell the brain (which moves the other screens too)
function recipeGo(action) {
  const c = sheetCard;
  c.step = action === "next" ? Math.min(c.step + 1, c.steps.length - 1) : Math.max(c.step - 1, -1);
  renderSheet();
  send({ type: "recipe", action });
}

const renderers = {
  weather: (c) => {
    const title = c.place.split(",")[0];
    const now = el("div", "wx-now");
    const words = el("div");
    words.append(el("p", "wx-condition", sentence(c.now.condition)),
                 el("p", "wx-detail", `Feels like ${c.now.feels}°, wind ${c.now.wind} mph`));
    now.append(el("span", "wx-temp", `${c.now.temp}°`), words);
    const days = el("div", "wx-days");
    for (const d of c.days) {
      const day = el("div", "wx-day");
      const range = el("p", "wx-range");
      range.append(el("span", "wx-high", `${d.high}°`), el("span", "wx-low", `${d.low}°`));
      day.append(el("p", "wx-label", d.label), range, el("p", "wx-sky", sentence(d.condition)));
      if (d.rain >= 10) day.append(el("p", "wx-rain", `${d.rain}% chance of rain`));
      days.append(day);
    }
    return [title, [now, days]];
  },

  // Timers: each one a dial that drains as time runs out. One timer gets the whole sheet with big Pause and
  // Stop beside it; several sit side by side, smaller, each with round Pause and Stop underneath.
  timers: () => {
    const live = liveTimers(ambient.timers, Date.now() / 1000);  // live list, not the card's snapshot
    if (!live.length) {
      return ["Timers", [el("p", "quiet", "No timers running."),
                         el("p", "hint", "Say “Hey Jarvis, set a timer for ten minutes.”")]];
    }
    const size = live.length === 1 ? "one" : live.length === 2 ? "two" : "many";
    const wrap = el("div", `timers ${size}`);
    wrap.append(...live.map((t) => timerDial(t, size === "one")));
    return ["Timers", [wrap]];
  },

  alarms: (c) => {
    if (!c.alarms.length) {
      return ["Alarms", [el("p", "quiet", "No alarms set."),
                         el("p", "hint", "Say “Hey Jarvis, wake me up at 7.”")]];
    }
    return ["Alarms", c.alarms.map(alarmRow)];
  },

  shopping: (c) => {
    if (!c.groups.length) {
      return ["Shopping list", [el("p", "quiet", "The shopping list is empty."),
                                el("p", "hint", "Say “Hey Jarvis, add milk to the shopping list.”")]];
    }
    // One list per Todoist section, uncategorized items first with no heading
    return ["Shopping list", c.groups.flatMap((g) => [...(g.name ? [el("p", "group-label", g.name)] : []),
                                                      shopList(g.items)])];
  },

  // Home control: a tile per kind of thing in the house, each showing its state at a glance and opening its page.
  // Lights is the tall one, and while any light is on it is lit itself.
  house: (c) => {
    const { lights, climate, media, rooms, automations } = c;
    const grid = el("div", "house");
    grid.append(
      tile("lights", "Lights", ICONS.bulb, lights.on ? String(lights.on) : "Off", lights.on ? "on" : "",
           lights.on ? lights.rooms.join(", ") : `All ${lights.total} lights`, lights.on > 0),
      tile("climate", "Climate", ICONS.thermometer, `${climate.temp}°`, "",
           climate.action === "idle" ? `Set to ${climate.target}°` : `${sentence(climate.action)} to ${climate.target}°`),
      tile("media", "Speakers & TVs", ICONS.tv, media.playing.length ? String(media.playing.length) : "Off",
           media.playing.length ? "playing" : "", media.playing.length ? media.playing.join(", ") : "Nothing playing"),
      tile("rooms", "Rooms", ICONS.house, String(rooms.length), "", rooms.join(", ")),
      tile("automations", "Automations", ICONS.automation, String(automations.on), "on",
           automations.on === automations.total ? "All running" : `${automations.total - automations.on} off`),
    );
    return ["Home", [grid]];
  },

  // Lights by room, rooms scrolling sideways. A light that's on fills with the accent from the bottom, as high as
  // it is bright: tap to switch it, drag up or down to dim it (a drag to the bottom turns it off).
  lights: (c) => {
    const rooms = deviceGroups(c.rooms.map((r) => [r.name, r.lights.map(lightTile)]));
    const all = c.rooms.flatMap((r) => r.lights);
    if (!all.some((l) => l.on)) return ["Lights", [rooms]];
    const off = el("button", "pill lights-off", "All off");
    off.type = "button";
    off.addEventListener("click", () => {
      for (const l of all) l.on = false;  // show it now; the brain's card confirms
      renderSheet();
      send({ type: "lights_off" });
    });
    return ["Lights", [rooms, off]];
  },

  // Every room, its name the big thing and what's on in it underneath. A room with a light on is lit, so the page
  // reads like the house at night from outside.
  rooms: (c) => {
    const grid = el("div", "rooms");
    for (const r of c.rooms) {
      const b = el("button", r.lights_on ? "tile room lit" : "tile room");
      b.type = "button";
      const lines = [];
      if (r.lights) lines.push(r.lights_on ? `${r.lights_on} ${r.lights_on === 1 ? "light" : "lights"} on` : "Lights off");
      lines.push(...r.playing.map((n) => `${n} playing`), ...r.switched_on.map((n) => `${n} on`));
      const what = el("span", "room-what");
      what.append(...lines.map((l) => el("span", "tile-detail", l)));
      b.append(el("span", "room-name", r.name), what);
      b.addEventListener("click", () => send({ type: "open", sheet: "room", room: r.id }));
      grid.append(b);
    }
    return ["Rooms", [grid]];
  },

  // Each thermostat a page of its own, swiping sideways between them. Opened from a room's thermostat, that one shows
  // first; a fresh card (after + or a mode) keeps whichever is showing.
  climate: (c) => {
    if (!c.thermostats.length) {
      return ["Climate", [el("p", "quiet", "No thermostats."),
                          el("p", "hint", "Thermostats and air conditioners in Home Assistant show up here.")]];
    }
    const pages = el("div", "thermostats");
    pages.append(...c.thermostats.map(thermostatPage));
    keepPlace(pages, c);
    return ["Climate", [pages]];
  },

  // Every speaker and TV, a panel each scrolling sideways: what's on it, its volume, play or pause, and power.
  // One that's playing is lit. Opened from a room's player, that one shows first.
  media: (c) => {
    if (!c.players.length) {
      return ["Speakers & TVs", [el("p", "quiet", "No speakers or TVs."),
                                 el("p", "hint", "Media players in Home Assistant show up here.")]];
    }
    const row = el("div", "players");
    row.append(...c.players.map(playerPanel));
    keepPlace(row, c);
    return ["Speakers & TVs", [row]];
  },

  // Automations: a row each like the alarms, when it last ran, Run, and a switch; one that's off is dimmed
  automations: (c) => {
    if (!c.automations.length) {
      return ["Automations", [el("p", "quiet", "No automations."),
                              el("p", "hint", "Automations made in Home Assistant show up here.")]];
    }
    return ["Automations", c.automations.map(automationRow)];
  },

  // One room's devices by kind, the way the lights page has lights by room
  room: (c) => {
    // A player is lit while it's doing something (playing, paused, a TV that's just on); idle and off are quiet
    const quiet = (m) => ["off", "standby", "idle", "unavailable"].includes(m.state);
    const media = (m) => openingTile(m, "media", sentence(m.state), !quiet(m), quiet(m));
    const climate = (t) => openingTile(t, "climate", `${t.temp}°`, false, false);
    return [c.name, [deviceGroups([["Lights", c.lights.map(lightTile)], ["Speakers & TVs", c.media.map(media)],
                                   ["Climate", c.climate.map(climate)], ["Other", c.switches.map(lightTile)]])]];
  },

  menu: () => {
    const entries = ROW.map((r) => [r.label, ICONS[r.icon],
                                    r.local && r.kind !== "timers" ? () => openSheet({ kind: r.kind }) : r.kind]);
    return ["Menu", entries.map(([label, icon, sheet]) => {
      const row = el("button", "menu-row");
      row.type = "button";
      const glyph = el("span", "menu-icon");
      glyph.innerHTML = icon;  // fixed markup from ICONS, never data
      row.append(glyph, el("span", "menu-label", label));
      row.addEventListener("click", typeof sheet === "function" ? sheet : () => send({ type: "open", sheet }));
      return row;
    })];
  },

  // Music: the album art is the one picture; the title in the far-read type; the controls bottom-right,
  // play/pause the big accent one
  music: () => {
    const t = nowPlaying;
    if (!t) {
      return ["Music", [el("p", "quiet", "Nothing is playing."),
                        el("p", "hint", "Say “Hey Jarvis, play some Fleetwood Mac.”")]];
    }
    const art = el("div", "np-art");
    if (t.art) {
      const img = el("img");
      Object.assign(img, { src: t.art, alt: "" });
      art.append(img);
    }
    const info = el("div", "np-info");
    info.append(el("p", "np-title", t.title), el("p", "np-artist", t.artist));
    if (t.album && t.album !== t.title) info.append(el("p", "np-album", t.album));
    const bar = el("div", "np-bar");
    bar.append(el("i"));
    const times = el("p", "np-times");
    times.append(el("span", "np-at"), el("span", null, countdown(Math.floor(t.duration_ms / 1000))));
    const progress = el("div", "np-progress");
    progress.append(bar, times);
    const controls = el("div", "np-controls");
    const buttons = [["previous", "Previous track", ICONS.previous],
                     t.is_playing ? ["pause", "Pause", ICONS.pause] : ["play", "Play", ICONS.play],
                     ["next", "Next track", ICONS.next]];
    for (const [action, label, icon] of buttons) {
      const b = el("button", action === "play" || action === "pause" ? "np-btn main" : "np-btn");
      b.type = "button";
      b.dataset.action = action;
      b.setAttribute("aria-label", label);
      b.innerHTML = icon;  // fixed markup from ICONS
      b.addEventListener("click", () => mediaPress(action));
      controls.append(b);
    }
    info.append(progress, controls);
    const wrap = el("div", "np");
    wrap.append(art, info);
    return ["Music", [wrap]];
  },


  // The days ahead in order: a heading per day, then each event at its time with the calendar it's on.
  // Something under way says "Now"; the next thing coming up today has its time in the accent.
  agenda: (c) => {
    const now = Date.now() / 1000;
    const events = c.events.filter((e) => e.end > now);
    if (!events.length) {
      return ["Calendar", [el("p", "quiet", "Nothing on the calendar this week."),
                           el("p", "hint", "Events from the household's calendars show up here.")]];
    }
    const nextUp = events.find((e) => !e.all_day && e.start > now);
    const nodes = [];
    let shownDay = null;
    for (const e of events) {
      const day = new Date(Math.max(e.start, now) * 1000);  // under way since yesterday: today's
      if (day.toDateString() !== shownDay) {
        shownDay = day.toDateString();
        nodes.push(el("h3", "day-title", dayTitle(day)));
      }
      const row = el("div", "event-row");
      const when = eventWhen(e, now, "event-when");
      if (e === nextUp && dayTitle(day) === "Today") when.classList.add("next");
      const what = el("div", "event-what");
      what.append(el("p", "event-title", e.title), el("p", "event-cal", e.calendar));
      row.append(when, what);
      nodes.push(row);
    }
    return ["Calendar", nodes];
  },

  // The day at a glance: the forecast on the left, the rest of today's calendar on the right, and along the
  // bottom the same glanceable items as the home strip (next alarm, timers, the shopping list).
  briefing: (c) => {
    const now = Date.now() / 1000;
    const sky = el("div", "brief-sky");
    sky.append(el("p", "brief-date", new Date().toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" })));
    if (c.weather) {
      const today = c.weather.days[0];
      sky.append(el("p", "brief-temp", `${c.weather.now.temp}°`),
                 el("p", "brief-cond", `${sentence(c.weather.now.condition)}, high ${today.high}, low ${today.low}`));
      if (today.rain >= 30) sky.append(el("p", "brief-rain", `${today.rain}% chance of rain`));
    } else {
      sky.append(el("p", "brief-cond", "No forecast right now."));
    }

    const day = el("div", "brief-day");
    day.append(el("p", "day-title", "Today"));
    const events = c.events.filter((e) => e.end > now);
    if (!events.length) day.append(el("p", "brief-free", "Nothing on the calendar."));
    for (const e of events.slice(0, 4)) {
      const row = el("div", "brief-event");
      row.append(eventWhen(e, now, "brief-when"), el("span", "brief-title", e.title));
      day.append(row);
    }
    if (events.length > 4) day.append(el("p", "brief-more", `and ${events.length - 4} more`));

    const facts = el("div", "brief-facts");
    const fact = (num, label) => {
      const item = el("span", "item");
      item.append(typeof num === "string" ? el("span", "num", num) : num, el("span", "label", label));
      facts.append(item);
    };
    if (c.next_alarm) fact(alarmTime(c.next_alarm.time, "num"), `${sentence(c.next_alarm.label) || "Alarm"}${dayWord(c.next_alarm.next_ring)}`);
    const live = liveTimers(c.timers, now);
    if (live.length === 1) {
      const num = steadyCountdown("num", timeLeft(live[0], now));
      if (!paused(live[0])) num.dataset.endsAt = live[0].ends_at;  // ticks with the other countdowns
      fact(num, timerName(live[0]));
    } else if (live.length) fact(String(live.length), "timers running");
    if (c.shopping) fact(String(c.shopping), c.shopping === 1 ? "thing on the shopping list" : "things on the shopping list");

    const top = el("div", "brief-top");
    top.append(sky, day);
    const sheet = el("div", "brief");
    sheet.append(top);
    if (facts.childElementCount) sheet.append(facts);
    return [c.greeting, [sheet]];
  },

  // Cooking along: the ingredients, then one numbered step at a time in the far-read type. Previous and Next
  // sit bottom-right; on the last step Next becomes Done, which closes the recipe.
  recipe: (c) => {
    let body;
    if (c.step < 0) {
      body = el("ul", c.ingredients.length > 6 ? "ingredients two-col" : "ingredients");
      body.append(...c.ingredients.map((i) => el("li", null, i)));
    } else {
      body = el("div", "recipe-step");
      const num = el("div", "step-num");
      num.append(el("span", "step-n", String(c.step + 1)), el("span", "step-of", `of ${c.steps.length}`));
      body.append(num, el("p", "step-text", c.steps[c.step]));
    }
    const done = c.step >= 0 && c.step === c.steps.length - 1;
    const prev = el("button", "pill recipe-prev", "Previous");
    const next = el("button", "pill recipe-next", done ? "Done" : "Next");
    prev.type = next.type = "button";
    prev.hidden = c.step < 0;  // nothing comes before the ingredients
    next.hidden = !c.steps.length;
    prev.addEventListener("click", () => recipeGo("previous"));
    next.addEventListener("click", () => (done ? closeSheet() : recipeGo("next")));
    const nav = el("div", "recipe-nav");
    nav.append(prev, next);
    const wrap = el("div", "recipe");
    wrap.append(body, nav);
    return [c.title, [wrap]];
  },

  text: (c) => {
    const lines = el("div", "text-lines");
    if (c.lines.length <= 5 && c.lines.every((l) => l.length <= 40)) lines.classList.add("roomy");
    lines.append(...c.lines.map((l) => el("p", null, l)));
    return [c.title, [lines]];
  },
};

// A home control tile: what it is up top, its state in the far-read type at the bottom, a detail line under that
function tile(sheet, label, icon, num, unit, detail, lit = false) {
  const b = el("button", `tile tile-${sheet}${lit ? " lit" : ""}`);
  b.type = "button";
  const head = el("span", "tile-head"), glyph = el("span", "tile-icon"), state = el("span", "tile-state");
  glyph.innerHTML = icon;  // fixed markup from ICONS
  head.append(glyph, el("span", null, label));
  state.append(el("span", /\d/.test(num) ? "tile-num" : "tile-num word", num));  // "Off" stays quiet
  if (unit) state.append(el("span", "tile-unit", unit));
  b.append(head, state, el("span", "tile-detail", detail));
  b.addEventListener("click", () => send({ type: "open", sheet }));
  return b;
}

// Groups of device tiles side by side, each under its name, scrolling sideways: lights by room, a room's devices by kind
function deviceGroups(groups) {
  const row = el("div", "dev-groups");
  for (const [name, tiles] of groups) {
    if (!tiles.length) continue;
    const group = el("section", "dev-group"), grid = el("div", "dev-grid");
    grid.append(...tiles);
    group.append(el("h3", "dev-group-name", name), grid);
    row.append(group);
  }
  return row;
}

// One device: its name up top, its state at the bottom, and the accent filling it from the bottom while it's on
// (as high as it's bright, for a light). The face is drawn twice, frost on the tile and night on the fill clipped to
// its height, so the name and state stay legible wherever the edge of the fill falls across them.
function deviceTile(name) {
  const b = el("button", "dev");
  b.type = "button";
  b.setAttribute("aria-label", name);
  const faces = [el("span", "dev-face"), el("span", "dev-face dev-lit")];
  faces[1].setAttribute("aria-hidden", "true");
  for (const f of faces) f.append(el("span", "dev-name", name), el("span", "dev-level"));
  b.append(...faces);
  const paint = (state, fill, off = !fill) => {
    b.style.setProperty("--b", `${fill}%`);
    b.classList.toggle("off", off);  // the state in mist: nothing going on
    b.classList.toggle("wordy", state.length > 4);  // "Playing" in the smaller size
    for (const f of faces) f.lastChild.textContent = state;
  };
  return [b, paint];
}

// Speakers, TVs, and thermostats: their state, opening their own page. fill: lit (playing, say); off: in mist.
function openingTile(d, sheet, state, fill, off) {
  const [b, paint] = deviceTile(d.name);
  paint(state, fill ? 100 : 0, off);
  b.addEventListener("click", () => send({ type: "open", sheet, entity: d.entity }));
  return b;
}

// A sideways row of devices (thermostats, players) keeps its place when a fresh card redraws it, and first opens
// on the one asked for (the thermostat tapped on a room's page)
function keepPlace(row, c) {
  const was = els.sheetBody.querySelector(`.${row.className}`)?.scrollLeft;
  requestAnimationFrame(() => {
    const asked = c.request && c.request.entity && [...row.children].find((p) => p.dataset.entity === c.request.entity);
    row.scrollLeft = was ?? (asked ? asked.offsetLeft - row.offsetLeft : 0);
  });
}

// A round button with an icon, for the device pages
function roundButton(cls, label, icon, onPress) {
  const b = el("button", cls);
  b.type = "button";
  b.setAttribute("aria-label", label);
  b.innerHTML = icon;  // fixed markup from ICONS
  b.addEventListener("click", onPress);
  return b;
}

// One player: room and name, what's on, then volume and the buttons at the bottom. Volume taps go out as one
// change once they stop, like a thermostat's.
const VOLUME_STEP = 5;
const PLAYER_ON = ["playing", "paused", "idle", "on", "buffering"];

function playerPanel(p) {
  const panel = el("section", "player");
  panel.dataset.entity = p.entity;
  const what = el("div", "pl-what");
  const volume = el("span", "pl-volume");
  const play = roundButton("pl-btn main", "", "", () => {
    p.state = p.state === "playing" ? "paused" : "playing";
    paint();
    send({ type: "media", entity: p.entity, action: p.state === "playing" ? "play" : "pause" });
  });
  const power = roundButton("pl-btn", "", ICONS.power, () => {
    p.state = PLAYER_ON.includes(p.state) ? "off" : "idle";
    paint();
    send({ type: "media", entity: p.entity, action: p.state === "off" ? "turn_off" : "turn_on" });
  });
  let pending = null;
  const nudge = (dir) => {
    p.volume = Math.min(100, Math.max(0, p.volume + dir * VOLUME_STEP));
    paint();
    clearTimeout(pending);
    pending = setTimeout(() => send({ type: "media", entity: p.entity, volume: p.volume }), NUDGE_MS);
  };
  const vol = el("div", "pl-vol");
  vol.append(roundButton("pl-btn small", `Turn down the ${p.name}`, ICONS.minus, () => nudge(-1)), volume,
             roundButton("pl-btn small", `Turn up the ${p.name}`, ICONS.plus, () => nudge(1)));
  const buttons = el("div", "pl-buttons");
  buttons.append(play, power);
  const head = el("div");
  head.append(el("p", "pl-room", p.room), el("p", "pl-name", p.name));
  panel.append(head, what, vol, buttons);

  const paint = () => {
    const on = PLAYER_ON.includes(p.state), playing = p.state === "playing";
    panel.classList.toggle("lit", playing);
    panel.classList.toggle("off", !on);
    if (!on) what.replaceChildren(el("p", "pl-state", "Off"));
    else if (p.title) what.replaceChildren(el("p", "pl-title", p.title), el("p", "pl-by", p.artist || p.app || ""));
    else what.replaceChildren(el("p", "pl-state", p.state === "idle" ? "Nothing on" : sentence(p.state)));
    if (p.title && p.state === "paused") what.append(el("p", "pl-by", "Paused"));
    volume.textContent = p.volume === null || p.volume === undefined ? "" : String(p.volume);
    vol.hidden = !on || !volume.textContent;
    play.hidden = !on || (!p.title && !playing);  // nothing loaded: nothing for Play to play
    play.innerHTML = playing ? ICONS.pause : ICONS.play;  // fixed markup from ICONS
    play.setAttribute("aria-label", `${playing ? "Pause" : "Play"} the ${p.name}`);
    power.setAttribute("aria-label", `Turn ${on ? "off" : "on"} the ${p.name}`);
  };
  paint();
  return panel;
}

// "Ran today at 7:02am", "Ran yesterday at 11:02pm", "Ran Monday", "Ran March 3", or "Hasn't run yet"
function lastRan(epoch) {
  if (!epoch) return "Hasn't run yet";
  const at = new Date(epoch * 1000);
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(at).setHours(0, 0, 0, 0)) / 86400000);
  const time = `${at.getHours() % 12 || 12}:${String(at.getMinutes()).padStart(2, "0")}${at.getHours() < 12 ? "am" : "pm"}`;
  if (days <= 0) return `Ran today at ${time}`;
  if (days === 1) return `Ran yesterday at ${time}`;
  if (days < 7) return `Ran ${at.toLocaleDateString([], { weekday: "long" })}`;
  return `Ran ${at.toLocaleDateString([], { month: "long", day: "numeric" })}`;
}

function automationRow(a) {
  const row = el("div", a.on ? "auto-row" : "auto-row off");
  const when = el("p", "auto-when", lastRan(a.last_ran));
  const what = el("div", "auto-what");
  what.append(el("p", "auto-name", a.name), when);
  const run = el("button", "pill", "Run");
  run.type = "button";
  run.setAttribute("aria-label", `Run ${a.name}`);
  run.addEventListener("click", () => {
    a.last_ran = Date.now() / 1000;  // show it now; the brain's card confirms
    when.textContent = lastRan(a.last_ran);
    send({ type: "automation_run", entity: a.entity });
  });
  const toggle = el("button", "switch");
  toggle.type = "button";
  toggle.setAttribute("role", "switch");
  toggle.setAttribute("aria-checked", String(a.on));
  toggle.setAttribute("aria-label", a.name);
  toggle.append(el("span"));
  toggle.addEventListener("click", () => {
    a.on = !a.on;
    toggle.setAttribute("aria-checked", String(a.on));
    row.classList.toggle("off", !a.on);
    send({ type: "automation_toggle", entity: a.entity, enabled: a.on });
  });
  row.append(what, run, toggle);
  return row;
}

// A thermostat: the setting as the hero with − and + beside it, then a scale with a dot where the room is and a mark
// where it's set, the stretch between them ember while heating and ice while cooling, then its modes.
const MODE_NAMES = { off: "Off", heat: "Heat", cool: "Cool", heat_cool: "Heat & cool", auto: "Auto", dry: "Dry", fan_only: "Fan" };
const SCALE_SPAN = 20;
const NUDGE_MS = 700;  // + tapped three times sends one change, not three that answer out of order

function thermostatPage(t) {
  const page = el("section", "thermo");
  page.dataset.entity = t.entity;
  const target = el("p", "th-target"), status = el("p", "th-status");
  const about = el("div");
  about.append(el("p", "th-name", t.name), target, status);
  let pending = null;
  const step = (dir, label, icon) => {
    const b = el("button", "th-step");
    b.type = "button";
    b.setAttribute("aria-label", `${label} ${t.name.toLowerCase()}`);
    b.innerHTML = icon;  // fixed markup from ICONS
    b.addEventListener("click", () => {
      t.target = Math.min(t.max, Math.max(t.min, t.target + dir * (t.step || 1)));
      paint();  // ponytail: the fill keeps the old action's colour until the brain says what it's doing now
      clearTimeout(pending);
      pending = setTimeout(() => send({ type: "climate", entity: t.entity, target: t.target }), NUDGE_MS);
    });
    return b;
  };
  const steps = el("div", "th-steps");
  steps.append(step(-1, "Lower", ICONS.minus), step(1, "Raise", ICONS.plus));
  const top = el("div", "th-top");
  top.append(about, steps);

  const track = el("div", "th-track"), fill = el("i", "th-fill"), now = el("i", "th-now"), set = el("i", "th-set");
  track.append(fill, now, set);
  const lo = el("span"), hi = el("span"), scale = el("div", "th-scale");
  scale.append(lo, hi);
  const modes = el("div", "th-modes");
  const modeButtons = t.modes.map((m) => {
    const b = el("button", "pill", MODE_NAMES[m] || sentence(m.replace("_", " ")));
    b.type = "button";
    b.addEventListener("click", () => {
      t.mode = m;
      paint();
      send({ type: "climate", entity: t.entity, mode: m });
    });
    return b;
  });
  modes.append(...modeButtons);

  // The scale is the 20° around the room and the setting (the whole 50-90° range makes a 2° gap a sliver), moving
  // in steps of 5° so the dot and mark don't shift every time + is tapped
  let from = 0;
  const at = (v) => `${Math.min(100, Math.max(0, ((v - from) / SCALE_SPAN) * 100))}%`;
  const paint = () => {
    from = Math.min(t.max - SCALE_SPAN, Math.max(t.min, Math.round(((t.temp + t.target) / 2 - SCALE_SPAN / 2) / 5) * 5));
    lo.textContent = `${from}°`;
    hi.textContent = `${from + SCALE_SPAN}°`;
    const off = t.mode === "off";
    page.className = `thermo ${off ? "off" : t.action}`;
    target.textContent = off ? "Off" : `${t.target}°`;
    const here = `${t.temp}° now${t.humidity !== null && t.humidity !== undefined ? `, ${t.humidity}% humidity` : ""}`;
    const doing = { heating: "Heating", cooling: "Cooling", fan: "Fan running", drying: "Drying" }[t.action];
    status.textContent = doing ? `${doing}, ${here}` : sentence(here);
    now.style.left = at(t.temp);
    set.style.left = at(t.target);
    fill.style.left = at(Math.min(t.temp, t.target));
    fill.style.width = `${Math.abs(parseFloat(at(t.target)) - parseFloat(at(t.temp)))}%`;
    modeButtons.forEach((b, i) => b.setAttribute("aria-pressed", String(t.modes[i] === t.mode)));
  };
  paint();
  page.append(top, track, scale, modes);
  return page;
}

// A light, or anything else that switches (a fan, a plug): tap to switch it, and a dimmer also drags up or down
const DRAG_START_PX = 8;

function lightTile(l) {
  const dims = l.brightness !== null && l.brightness !== undefined;
  const [b, fill] = deviceTile(l.name);
  b.setAttribute("role", "switch");
  const paint = () => {
    fill(l.on ? (dims ? `${l.brightness}%` : "On") : "Off", l.on ? (dims ? l.brightness : 100) : 0);
    b.setAttribute("aria-checked", String(l.on));
  };
  paint();

  let drag = null;  // {y, from, moved} while a finger is down on a dimmer
  let dragged = false;  // the last touch was a drag, so the click (if one follows) mustn't switch the light
  b.addEventListener("pointerdown", (e) => {
    dragged = false;  // a drag on a touchscreen usually has no click after it to clear this
    if (!dims) return;
    drag = { y: e.clientY, from: l.on ? l.brightness : 0, moved: false };
    b.setPointerCapture(e.pointerId);
  });
  b.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dy = drag.y - e.clientY;
    if (!drag.moved && Math.abs(dy) < DRAG_START_PX) return;
    drag.moved = true;
    const level = Math.round(Math.min(100, Math.max(0, drag.from + (dy / b.clientHeight) * 100)));
    l.on = level > 0;
    // Dragged off, it comes back on at the level it had before the drag (not one passed on the way down)
    if (level > 0 || drag.from > 0) l.brightness = level || drag.from;
    paint();
  });
  const lift = () => {
    if (drag && drag.moved) send({ type: "light", entity: l.entity, on: l.on, ...(l.on && { brightness: l.brightness }) });
    dragged = Boolean(drag && drag.moved);
    drag = null;
  };
  b.addEventListener("pointerup", lift);
  b.addEventListener("pointercancel", lift);
  b.addEventListener("click", () => {
    if (dragged) return;  // the drag already set it
    l.on = !l.on;
    paint();
    send({ type: "light", entity: l.entity, on: l.on });
  });
  return b;
}

// The arc is what's left of the timer, from 12 o'clock clockwise; it drains back toward 12 as time runs out
function drain(arc, left) {
  const total = Number(arc.dataset.duration);
  arc.style.strokeDashoffset = total ? String(1 - Math.min(1, Math.max(0, left / total))) : "0";
}

function timerDial(t, big) {
  const left = timeLeft(t, Date.now() / 1000);
  const box = el("div", paused(t) ? "tmr paused" : "tmr");
  const dial = el("div", "dial");
  dial.innerHTML = '<svg viewBox="0 0 100 100" aria-hidden="true"><circle class="dial-track" cx="50" cy="50" r="46"/>'
    + '<circle class="dial-arc" cx="50" cy="50" r="46" pathLength="1"/></svg>';  // fixed markup, never data
  const arc = dial.querySelector(".dial-arc");
  arc.dataset.duration = t.duration || 0;  // timers set before durations were kept show a full ring
  if (!paused(t)) arc.dataset.arcEnd = t.ends_at;
  drain(arc, left);
  const face = el("div", "dial-face");
  const time = steadyCountdown(left >= 3600 ? "dial-time long" : "dial-time", left);
  if (!paused(t)) time.dataset.endsAt = t.ends_at;
  face.append(time, el("span", "dial-name", timerName(t)));
  dial.append(face);

  const button = (cls, label, icon, onPress) => {
    const b = el("button", `tmr-btn ${cls}`);
    b.type = "button";
    b.setAttribute("aria-label", `${label} the ${t.label ? `${t.label} timer` : timerName(t).toLowerCase()}`);
    b.innerHTML = icon;  // fixed markup from ICONS
    if (big) b.append(el("span", null, label));
    b.addEventListener("click", onPress);
    return b;
  };
  const actions = el("div", "tmr-actions");
  actions.append(
    paused(t) ? button("tmr-main", "Resume", ICONS.play, () => pauseTimer(t))
              : button("tmr-main", "Pause", ICONS.pause, () => pauseTimer(t)),
    button("tmr-stop", "Stop", ICONS.stop, () => {
      box.classList.add("leaving");
      send({ type: "timer_cancel", id: t.id });  // the brain's next ambient update drops it
    }),
  );
  box.append(dial, actions);
  return box;
}

// Pause and Resume show at once; the brain's next ambient update confirms it within half a second
function pauseTimer(t) {
  const now = Date.now() / 1000;
  const changed = paused(t) ? { ...t, ends_at: now + t.paused_left, paused_left: null }
                            : { ...t, paused_left: Math.max(0, t.ends_at - now) };
  ambient = { ...ambient, timers: ambient.timers.map((x) => (x.id === t.id ? changed : x)) };
  send({ type: paused(t) ? "timer_resume" : "timer_pause", id: t.id });
  renderSheet();
  renderStrip();
}

function alarmRow(a) {
  const row = el("div", a.enabled ? "alarm-row" : "alarm-row off");
  const what = el("div", "alarm-what");
  what.append(el("p", "alarm-name", sentence(a.label) || "Alarm"), el("p", "alarm-days", repeatText(a.days)));
  const toggle = el("button", "switch");
  toggle.type = "button";
  toggle.setAttribute("role", "switch");
  toggle.setAttribute("aria-checked", String(a.enabled));
  toggle.setAttribute("aria-label", `Alarm at ${a.time}`);
  toggle.append(el("span"));
  toggle.addEventListener("click", () => {
    const on = toggle.getAttribute("aria-checked") !== "true";
    toggle.setAttribute("aria-checked", String(on));  // show the change now; the brain's card confirms it
    row.classList.toggle("off", !on);
    send({ type: "alarm_toggle", id: a.id, enabled: on });
  });
  const del = el("button", "pill", "Delete");
  del.type = "button";
  del.addEventListener("click", () => {
    row.classList.add("leaving");
    send({ type: "alarm_delete", id: a.id });
  });
  row.append(alarmTime(a.time, "alarm-time"), what, toggle, del);
  return row;
}

// --- Ringing ----------------------------------------------------------------------------------

function showRinging(r) {
  if (!r) {
    els.ringing.hidden = true;
    return;
  }
  if (r.kind === "timer") {  // a timer has no time of day to show, and nothing to snooze
    els.ringingTime.textContent = "Time’s up";
    els.ringingLabel.textContent = r.label ? `${sentence(r.label)} timer` : timerName(r);
  } else {
    els.ringingTime.replaceChildren(...alarmTime(r.time, "t").childNodes);
    els.ringingLabel.textContent = sentence(r.label) || "Alarm";
  }
  els.snooze.hidden = r.kind === "timer";
  els.ringing.hidden = false;
}

function shopList(items) {
  const list = el("ul", "shop-list");
  for (const item of items) {
    const row = el("li", "shop-row");
    const tick = el("button", "shop-item");
    tick.type = "button";
    tick.append(el("span", "shop-check"), el("span", "shop-name", sentence(item)));  // as said; shown like a list
    tick.addEventListener("click", () => toggleItem(row, item));
    const undo = el("button", "pill shop-undo", "Undo");
    undo.type = "button";
    undo.addEventListener("click", () => toggleItem(row, item));
    row.append(tick, undo);
    if (shopPending.has(item)) row.classList.add("done");
    list.append(row);
  }
  return list;
}

// Checking an item off: struck through right away, removed for good after 3 s unless Undo is tapped.
const SHOP_UNDO_MS = 3000;
const shopPending = new Map();  // item -> timeout

function toggleItem(row, item) {
  if (shopPending.has(item)) {
    clearTimeout(shopPending.get(item));
    shopPending.delete(item);
    row.classList.remove("done");
    return;
  }
  row.classList.add("done");
  shopPending.set(item, setTimeout(() => {
    shopPending.delete(item);
    send({ type: "shopping_remove", item });  // the brain answers with the updated list
  }, SHOP_UNDO_MS));
}

const svg = (body) => `<svg viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
const ICONS = {
  timer: svg('<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2.5M9.5 2.5h5"/>'),
  alarm: svg('<circle cx="12" cy="13" r="7.5"/><path d="M12 9.5V13l2.2 1.6M4.5 6l3-2.6M19.5 6l-3-2.6"/>'),
  stop: svg('<rect x="6" y="6" width="12" height="12" rx="2"/>'),
  list: svg('<path d="M9 6h11M9 12h11M9 18h11"/><path d="M3.5 6l1.2 1.2L7 4.8M3.5 12l1.2 1.2L7 10.8M3.5 18l1.2 1.2L7 16.8"/>'),
  music: svg('<path d="M9 18V5l11-2v13"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>'),
  volume: svg('<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11"/>'),
  quiet: svg('<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/>'),
  previous: svg('<path d="M6 5h2.5v14H6zM19 5.5v13L9.5 12z"/>'),
  play: svg('<path d="M8 5v14l11-7z"/>'),
  pause: svg('<path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/>'),
  next: svg('<path d="M15.5 5H18v14h-2.5zM5 5.5v13l9.5-6.5z"/>'),
  power: svg('<path d="M12 3v8"/><path d="M6.8 6.5a7.5 7.5 0 1 0 10.4 0"/>'),
  minus: svg('<path d="M5 12h14"/>'),
  plus: svg('<path d="M12 5v14M5 12h14"/>'),
  house: svg('<path d="M3.5 11L12 4l8.5 7"/><path d="M6 9.5V20h12V9.5"/><path d="M10 20v-5h4v5"/>'),
  bulb: svg('<path d="M9.5 18h5M10.5 21h3"/><path d="M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.3 1.1 2.2h5c0-.9.4-1.6 1.1-2.2A6 6 0 0 0 12 3z"/>'),
  thermometer: svg('<path d="M14 14.8V5.5a2 2 0 0 0-4 0v9.3a4 4 0 1 0 4 0z"/><path d="M12 11v6"/>'),
  tv: svg('<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8.5 21h7"/>'),
  automation: svg('<path d="M17 2.5l3 3-3 3"/><path d="M4 11.5v-2a4 4 0 0 1 4-4h12M7 21.5l-3-3 3-3"/><path d="M20 12.5v2a4 4 0 0 1-4 4H4"/>'),
};

// --- Music ------------------------------------------------------------------------------------

let nowPlaying = null;  // the brain's now_playing track, or null when nothing is playing
let nowPlayingAt = 0;  // Date.now() when it arrived: the position advances locally from there while playing
let musicTicker = null;

function showNowPlaying(track) {
  nowPlaying = track;
  nowPlayingAt = Date.now();
  renderStrip();
  if (sheetCard && sheetCard.kind === "music") renderSheet();
}

function musicPosition() {
  const t = nowPlaying;
  return Math.min(t.duration_ms, t.progress_ms + (t.is_playing ? Date.now() - nowPlayingAt : 0));
}

// Play and Pause show at once; the brain's next now_playing confirms (or corrects) it a moment later
function mediaPress(action) {
  if (action === "play" || action === "pause") {
    nowPlaying = { ...nowPlaying, progress_ms: musicPosition(), is_playing: action === "play" };
    nowPlayingAt = Date.now();
    renderSheet();
    renderStrip();
  }
  send({ type: "media", action });
}

// The progress bar and time, once a second while the music sheet is open and something plays
function paintMusic() {
  const fill = els.sheetBody.querySelector(".np-bar i");
  if (fill) {
    const at = musicPosition();
    fill.style.transform = `scaleX(${nowPlaying.duration_ms ? at / nowPlaying.duration_ms : 0})`;
    els.sheetBody.querySelector(".np-at").textContent = countdown(Math.floor(at / 1000));
  }
  const keepGoing = fill && nowPlaying.is_playing && sheetCard && sheetCard.kind === "music";
  if (keepGoing && !musicTicker) musicTicker = setInterval(paintMusic, 1000);
  if (!keepGoing && musicTicker) { clearInterval(musicTicker); musicTicker = null; }
}

// --- Volume -----------------------------------------------------------------------------------

let speakerVolume = null;  // the speaker's level as the brain last reported it; null until it does

// A new level from the brain (the satellite's report, another screen, or Claude): the bar follows it, if it's up,
// unless a finger is on the slider right now
function showVolume(level) {
  speakerVolume = level;
  paintVolume();
}

// The bar: swiped up from the bottom edge (it rises with the finger; let go a third of the way up, or flick, and it
// stays), or put up when Claude changes the volume. A tap anywhere else, or 5 s untouched, puts it away. The level is
// sent once, when the finger lifts off the slider.
const VOL_IDLE_MS = 5000;
let volUp = false;
let volIdle = null;
let volDrag = null;  // {id, y, t, dy, v} while a finger brings it up

function paintVolume() {
  const known = speakerVolume !== null;
  els.volSlider.hidden = els.volLevel.hidden = !known;
  els.volNone.hidden = known;
  if (known && !els.volSlider.matches(":active")) els.volSlider.value = speakerVolume;
  els.volLevel.textContent = els.volSlider.value;
  els.volSlider.style.setProperty("--fill", `${els.volSlider.value}%`);
}

const volBelow = () => els.volBar.offsetHeight + 26;  // far enough down to be off the screen

function placeVolume(y, settle) {  // y: px below its place
  els.volBar.classList.toggle("dragging", !settle);
  els.volBar.classList.toggle("settling", settle);
  els.volBar.style.transform = `translateY(${y}px)`;
}

const volumeAllowed = () =>
  !document.body.classList.contains("is-night") && els.overlay.hidden && els.ringing.hidden && els.camera.hidden;

function openVolume(level) {
  if (level !== undefined) speakerVolume = level;
  paintVolume();
  if (els.volBar.hidden) {  // from nowhere (Claude, a keyboard): start below, so it rises; one a finger has brought
    els.volBar.hidden = false;  // partway up, or that's on its way down, carries on from where it is
    placeVolume(volBelow(), false);
    void els.volBar.offsetWidth;
  }
  placeVolume(0, true);
  volUp = true;
  touchVolume();
}

function closeVolume() {
  clearTimeout(volIdle);
  volUp = false;
  placeVolume(volBelow(), true);
  setTimeout(() => { if (!volUp) els.volBar.hidden = true; }, SETTLE_MS);
}

function touchVolume() {
  clearTimeout(volIdle);
  volIdle = setTimeout(closeVolume, VOL_IDLE_MS);
}

els.volEdge.addEventListener("pointerdown", (e) => {
  if (volUp || !volumeAllowed()) return;
  volDrag = { id: e.pointerId, y: e.clientY, t: e.timeStamp, dy: 0, v: 0 };
  els.volEdge.setPointerCapture(e.pointerId);
  paintVolume();
  els.volBar.hidden = false;
  placeVolume(volBelow(), false);
});
els.volEdge.addEventListener("pointermove", (e) => {
  if (!volDrag || e.pointerId !== volDrag.id) return;
  const dy = Math.max(0, volDrag.y - e.clientY);
  volDrag.v = (dy - volDrag.dy) / Math.max(1, e.timeStamp - volDrag.t);
  Object.assign(volDrag, { dy, t: e.timeStamp });
  placeVolume(Math.max(0, volBelow() - dy), false);
});
for (const type of ["pointerup", "pointercancel"]) {
  els.volEdge.addEventListener(type, (e) => {
    if (!volDrag || e.pointerId !== volDrag.id) return;
    const { dy, v } = volDrag;
    volDrag = null;
    if (dy > volBelow() * SWIPE_AT || (v > SWIPE_FLICK && dy > 20)) openVolume();
    else closeVolume();
  });
}
els.volEdge.addEventListener("click", (e) => { if (e.detail === 0) openVolume(); });  // from a keyboard
els.volBar.addEventListener("pointerdown", touchVolume);
els.volSlider.addEventListener("input", () => { paintVolume(); touchVolume(); });
els.volSlider.addEventListener("change", () => send({ type: "volume", level: Number(els.volSlider.value) }));
// A tap anywhere else puts it away, and only does that (it doesn't press what's under it)
document.addEventListener("pointerdown", (e) => {
  if (!volUp || els.volBar.contains(e.target) || e.target === els.volEdge) return;
  closeVolume();
  const swallow = (c) => { c.stopPropagation(); c.preventDefault(); };
  document.addEventListener("click", swallow, true);
  setTimeout(() => document.removeEventListener("click", swallow, true), 0);
  e.stopPropagation();
}, true);

// A card's title and body; a row page the brain hasn't sent yet is just its title
function renderCard(card) {
  if (card.placeholder) return [ROW[rowAt(card.kind)].label, []];
  return renderers[card.kind](card);
}

function renderSheet() {
  if (!renderers[sheetCard.kind]) return;
  const [title, nodes] = renderCard(sheetCard);
  els.sheetTitle.textContent = title;
  els.sheetBody.replaceChildren(...nodes);
  if (sheetCard.kind === "timers" && !ticker && ambient.timers.length) ticker = setInterval(tick, 1000);
  const step = els.sheetBody.querySelector(".step-text");
  if (step) shrinkToFit(step, 24);
  paintMusic();
}

// --- Conversation overlay ---------------------------------------------------------------------

let hideTimer = null;

function setState(state) {
  clearTimeout(hideTimer);
  const wasHidden = els.overlay.hidden;
  if (state === "idle") {
    els.overlay.dataset.state = "idle";
    if (!wasHidden) hideTimer = setTimeout(hideOverlay, 4000);
    return;
  }
  if (wasHidden || els.overlay.dataset.state === "idle") {
    els.heard.textContent = "";
    els.reply.textContent = "";
    els.reply.style.fontSize = "";
  }
  els.overlay.dataset.state = state;
  showOverlay();
}

function showOverlay() {
  els.overlay.hidden = false;
  document.body.classList.add("talking");
  requestAnimationFrame(() => els.overlay.classList.add("shown"));
}

function hideOverlay() {
  clearTimeout(hideTimer);
  els.overlay.classList.remove("shown");
  document.body.classList.remove("talking");
  els.overlay.hidden = true;
}

function addReply(text) {
  els.reply.textContent = els.reply.textContent ? `${els.reply.textContent} ${text}` : text;
  fitReply();
}

// Long text shrinks 2px at a time to fit its box, down to a floor
function shrinkToFit(node, min) {
  let size = parseFloat(getComputedStyle(node).fontSize);
  while (node.scrollHeight > node.clientHeight && size > min) {
    size -= 2;
    node.style.fontSize = `${size}px`;
  }
}

// Long replies shrink to fit instead of scrolling: 36px down to 24px, then the oldest words scroll off.
function fitReply() {
  shrinkToFit(els.reply, 24);
  els.reply.scrollTop = els.reply.scrollHeight;
}

// --- Camera -----------------------------------------------------------------------------------
// A live view over WebRTC from Home Assistant. The brain's /camera socket relays the setup (offer, answer, ICE
// candidates); the video itself comes straight to this page. Closing the socket ends the stream.

// ponytail: one limit for every camera. Ring reports a battery even for plugged-in ones, so a battery camera
// can't be told apart to get a shorter view.
const CAMERA_MAX_MS = 2 * 60000;
let cam = null;  // the live view showing: {ws, pc, timer}

function showCamera(m) {
  closeCamera();
  els.cameraName.textContent = m.name;
  els.cameraNote.textContent = "Connecting…";
  els.camera.hidden = false;
  const ws = new WebSocket(wsUrl.replace(/\/display$/, "/camera") + `?entity=${encodeURIComponent(m.entity)}`);
  const view = cam = { ws, pc: null, timer: setTimeout(closeCamera, CAMERA_MAX_MS) };
  const say = (text) => { if (cam === view) els.cameraNote.textContent = text; };
  // Ring ends a live view on its own after a while: say so, then go back to the room
  const ended = () => {
    if (cam !== view) return;
    say("The camera stopped.");
    clearTimeout(view.timer);
    view.timer = setTimeout(closeCamera, 3000);
  };
  ws.onmessage = async (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "config") {
      const pc = view.pc = new RTCPeerConnection(msg.config);
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.addTransceiver("audio", { direction: "recvonly" });
      pc.ontrack = (ev) => { els.cameraVideo.srcObject = ev.streams[0] || new MediaStream([ev.track]); };
      pc.onicecandidate = (ev) => {
        if (ev.candidate) ws.send(JSON.stringify({ candidate: ev.candidate.toJSON() }));
      };
      pc.onconnectionstatechange = () => { if (["failed", "closed"].includes(pc.connectionState)) ended(); };
      await pc.setLocalDescription(await pc.createOffer());
      ws.send(JSON.stringify({ offer: pc.localDescription.sdp }));
    } else if (msg.type === "answer") {
      await view.pc.setRemoteDescription({ type: "answer", sdp: msg.answer });
    } else if (msg.type === "candidate") {
      const c = msg.candidate;
      await view.pc.addIceCandidate({ candidate: c.candidate, sdpMid: c.sdpMid ?? c.sdp_mid ?? null,
                                      sdpMLineIndex: c.sdpMLineIndex ?? c.sdp_m_line_index ?? 0 });
    } else if (msg.type === "error") {
      say(`Couldn't open the camera: ${msg.message}`);
    }
  };
  ws.onclose = () => (els.cameraVideo.srcObject ? ended() : say("Couldn't reach the camera."));
  els.cameraVideo.onplaying = () => say("");
}

function closeCamera() {
  if (!cam) return;
  clearTimeout(cam.timer);
  cam.pc?.close();
  cam.ws.close();
  cam = null;
  els.cameraVideo.srcObject = null;
  els.camera.hidden = true;
}

// --- Touch ------------------------------------------------------------------------------------

els.mic.addEventListener("click", () => send({ type: "talk" }));
els.stop.addEventListener("click", () => send({ type: "stop" }));
els.close.addEventListener("click", hideOverlay);
els.edge.addEventListener("click", (e) => { if (e.detail === 0) goBack(); });  // Enter or Space; a tap does nothing
els.headerHome.addEventListener("click", closeSheet);  // the time: back to the dashboard
els.dashEdge.addEventListener("click", (e) => { if (e.detail === 0) forceNight(); });  // from a keyboard

// Night now, whatever the hour, until the next touch or the night hours next end (with none set, just the touch)
function forceNight() {
  forcedNight = true;
  wokenUntil = 0;
  forcedUntil = Infinity;
  if (nightHours) {
    const end = new Date(), [h, m] = nightHours.end.split(":").map(Number);
    end.setHours(h, m, 0, 0);
    if (end <= new Date()) end.setDate(end.getDate() + 1);
    forcedUntil = end.getTime();
  }
  applyNight();
}
els.cameraClose.addEventListener("click", closeCamera);
els.menu.addEventListener("click", () => openSheet({ kind: "menu" }));
els.ringingStop.addEventListener("click", () => send({ type: "stop" }));
els.snooze.addEventListener("click", () => send({ type: "snooze" }));
els.sheet.addEventListener("pointerdown", touchSheet);
document.addEventListener("pointerdown", wakeFromNight, true);

startClock();
connect();
