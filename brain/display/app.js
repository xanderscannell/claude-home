// Claude Home display: follows the brain over a WebSocket and turns its messages into screens.
// URL parameters: ?device=<name> (default "claude-pi"), ?ws=<url> (default: this server's /display socket).

const params = new URLSearchParams(location.search);
const device = params.get("device") || "claude-pi";
const wsUrl = params.get("ws") || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/display`;

const $ = (id) => document.getElementById(id);
const els = {
  time: $("time"), date: $("date"), mic: $("mic"), menu: $("menu"), weather: $("weather"), strip: $("strip"),
  overlay: $("overlay"), heard: $("heard"), reply: $("reply"), stop: $("stop"), close: $("close-overlay"),
  sheet: $("sheet"), sheetTitle: $("sheet-title"), sheetBody: $("sheet-body"), sheetClose: $("sheet-close"),
  ringing: $("ringing"), ringingTime: $("ringing-time"), ringingLabel: $("ringing-label"),
  ringingStop: $("ringing-stop"), snooze: $("snooze"),
  offline: $("offline"), nightTime: $("night-time"),
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
  card: (m) => openSheet(m.card),
  ringing: (m) => showRinging(m.ringing),
  volume: (m) => showVolume(m.level),
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
  els.nightTime.textContent = els.time.textContent;
  applyNight();
  renderStrip();  // calendar events drop off the strip when they end
}

// --- Night ------------------------------------------------------------------------------------
// Between hello.night's start and end ("22:00" to "07:00") home becomes a dim clock on black. A touch
// brings the normal home back for 30 s, and each touch while it's back keeps it for another 30 s.

const NIGHT_WAKE_MS = 30000;
let nightHours = null;
let wokenUntil = 0;
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
  const night = inNightHours() && Date.now() >= wokenUntil;
  const was = document.body.classList.contains("is-night");
  document.body.classList.toggle("is-night", night);
  if (was && !night && !frontLayer.classList.contains("shown")) nextPhoto();  // morning, or a touch: bring the room back
}

function wakeFromNight() {
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

function renderStrip() {
  els.strip.replaceChildren();
  const now = Date.now() / 1000;
  const running = ambient.timers.filter((t) => t.ends_at > now);
  for (const t of running) {
    const item = el("button", "item timer");
    item.type = "button";
    item.dataset.endsAt = t.ends_at;
    item.append(el("span", "num", countdown(t.ends_at - now)), el("span", "label", sentence(t.label) || "Timer"));
    item.addEventListener("click", () => send({ type: "open", sheet: "timers" }));
    els.strip.append(item);
  }
  const a = ambient.next_alarm;
  if (a) {
    const item = el("button", "item alarm");
    item.type = "button";
    item.append(alarmTime(a.time, "num"), el("span", "label", `${sentence(a.label) || "Alarm"}${dayWord(a.next_ring)}`));
    item.addEventListener("click", () => send({ type: "open", sheet: "timers" }));
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
  // Tick once a second only while something is counting down (the Pi's CPU is better spent idle).
  if (running.length && !ticker) ticker = setInterval(tick, 1000);
  if (!running.length && ticker) { clearInterval(ticker); ticker = null; }
}

function tick() {
  const now = Date.now() / 1000;
  for (const item of els.strip.querySelectorAll(".timer")) {
    const left = Number(item.dataset.endsAt) - now;
    if (left <= 0) { renderStrip(); return; }  // finished: drop it (the brain rings and confirms)
    item.querySelector(".num").textContent = countdown(left);
  }
  for (const c of els.sheetBody.querySelectorAll("[data-ends-at]")) {
    c.textContent = countdown(Number(c.dataset.endsAt) - now);
  }
}

// --- Sheets -----------------------------------------------------------------------------------

const SHEET_IDLE_MS = 45000;
let sheetCard = null;
let sheetIdle = null;

function openSheet(card) {
  sheetCard = card;
  // A timers card can beat the next ambient update by up to half a second; it is just as fresh, so use it.
  if (card.kind === "timers") ambient = { ...ambient, timers: card.timers };
  if (card.kind === "volume" && card.level !== undefined) speakerVolume = card.level;
  els.sheet.hidden = false;  // before rendering, so long text can be measured and fitted
  renderSheet();
  requestAnimationFrame(() => els.sheet.classList.add("shown"));
  touchSheet();
}

function closeSheet() {
  clearTimeout(sheetIdle);
  if (sheetCard && sheetCard.kind === "recipe") send({ type: "recipe", action: "close" });  // ends recipe mode
  sheetCard = null;
  els.sheet.classList.remove("shown");
  els.sheet.hidden = true;
  els.sheetBody.replaceChildren();
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

  timers: (c) => {
    const now = Date.now() / 1000;
    const running = ambient.timers.filter((t) => t.ends_at > now);  // live list, not the card's snapshot
    const alarms = c.alarms || [];
    if (!running.length && !alarms.length) {
      return ["Timers and alarms", [el("p", "quiet", "No timers or alarms."),
                                    el("p", "hint", "Say “Hey Jarvis, set a timer for ten minutes” or “wake me up at 7.”")]];
    }
    const both = running.length && alarms.length;  // group labels only when there are two groups to tell apart
    const nodes = [];
    if (both) nodes.push(el("p", "group-label", "Timers"));
    nodes.push(...running.map(timerRow));
    if (both) nodes.push(el("p", "group-label", "Alarms"));
    nodes.push(...alarms.map(alarmRow));
    return [both ? "Timers and alarms" : running.length ? "Timers" : "Alarms", nodes];
  },

  shopping: (c) => {
    if (!c.items.length) {
      return ["Shopping list", [el("p", "quiet", "The shopping list is empty."),
                                el("p", "hint", "Say “Hey Jarvis, add milk to the shopping list.”")]];
    }
    const list = el("ul", "shop-list");
    for (const item of c.items) {
      const row = el("li", "shop-row");
      const tick = el("button", "shop-item");
      tick.type = "button";
      tick.append(el("span", "shop-check"), el("span", "shop-name", item));
      tick.addEventListener("click", () => toggleItem(row, item));
      const undo = el("button", "pill shop-undo", "Undo");
      undo.type = "button";
      undo.addEventListener("click", () => toggleItem(row, item));
      row.append(tick, undo);
      if (shopPending.has(item)) row.classList.add("done");
      list.append(row);
    }
    return ["Shopping list", [list]];
  },

  menu: () => {
    const entries = [
      ["Timers and alarms", ICONS.timer, "timers"],
      ["Shopping list", ICONS.list, "shopping"],
      ["Music", ICONS.music, null],
      ["Volume", ICONS.volume, () => openSheet({ kind: "volume" })],  // the page already knows the level
    ];
    return ["Menu", entries.map(([label, icon, sheet]) => {
      const row = el("button", "menu-row");
      row.type = "button";
      const glyph = el("span", "menu-icon");
      glyph.innerHTML = icon;  // fixed markup from ICONS, never data
      row.append(glyph, el("span", "menu-label", label));
      if (typeof sheet === "function") row.addEventListener("click", sheet);
      else if (sheet) row.addEventListener("click", () => send({ type: "open", sheet }));
      else { row.disabled = true; row.append(el("span", "menu-soon", "Coming soon")); }
      return row;
    })];
  },

  // One big number and a slider as wide as the screen. The level is sent once, when the finger lifts.
  volume: () => {
    if (speakerVolume === null) {
      return ["Volume", [el("p", "quiet", "The speaker isn't connected."),
                         el("p", "hint", "Volume can be changed once it's back.")]];
    }
    const level = el("p", "vol-level");
    const slider = el("input", "vol-slider");
    Object.assign(slider, { type: "range", min: 0, max: 100, step: 1, value: speakerVolume });
    slider.setAttribute("aria-label", "Volume");
    const paint = () => {
      level.textContent = slider.value;
      slider.style.setProperty("--fill", `${slider.value}%`);
    };
    slider.addEventListener("input", paint);
    slider.addEventListener("change", () => send({ type: "volume", level: Number(slider.value) }));
    paint();
    const quiet = el("span", "vol-end"), loud = el("span", "vol-end");
    quiet.innerHTML = ICONS.quiet;  // fixed markup from ICONS
    loud.innerHTML = ICONS.volume;
    const row = el("div", "vol-row");
    row.append(quiet, slider, loud);
    const sheet = el("div", "vol");
    sheet.append(level, row);
    return ["Volume", [sheet]];
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
    const running = c.timers.filter((t) => t.ends_at > now);
    if (running.length === 1) {
      const num = el("span", "num", countdown(running[0].ends_at - now));
      num.dataset.endsAt = running[0].ends_at;  // ticks with the other countdowns
      fact(num, sentence(running[0].label) || "Timer");
    } else if (running.length) fact(String(running.length), "timers running");
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

function timerRow(t) {
  const row = el("div", "timer-row");
  const num = el("span", "countdown", countdown(t.ends_at - Date.now() / 1000));
  num.dataset.endsAt = t.ends_at;
  const cancel = el("button", "pill", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => {
    row.classList.add("leaving");
    send({ type: "timer_cancel", id: t.id });
  });
  row.append(num, el("span", "label", sentence(t.label) || "Timer"), cancel);
  return row;
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
  els.ringingTime.replaceChildren(...alarmTime(r.time, "t").childNodes);
  els.ringingLabel.textContent = sentence(r.label) || "Alarm";
  els.ringing.hidden = false;
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
  list: svg('<path d="M9 6h11M9 12h11M9 18h11"/><path d="M3.5 6l1.2 1.2L7 4.8M3.5 12l1.2 1.2L7 10.8M3.5 18l1.2 1.2L7 16.8"/>'),
  music: svg('<path d="M9 18V5l11-2v13"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>'),
  volume: svg('<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11"/>'),
  quiet: svg('<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/>'),
};

// --- Volume -----------------------------------------------------------------------------------

let speakerVolume = null;  // the speaker's level as the brain last reported it; null until it does

// A new level from the brain (the satellite's report, another screen, or Claude). An open volume sheet
// follows it, unless a finger is on the slider right now.
function showVolume(level) {
  speakerVolume = level;
  if (!sheetCard || sheetCard.kind !== "volume") return;
  const slider = els.sheetBody.querySelector(".vol-slider");
  if (!slider) renderSheet();  // it was saying the speaker isn't connected
  else if (!slider.matches(":active")) {
    slider.value = level;
    slider.dispatchEvent(new Event("input"));
  }
}

function renderSheet() {
  const render = renderers[sheetCard.kind];
  if (!render) return;
  const [title, nodes] = render(sheetCard);
  els.sheetTitle.textContent = title;
  els.sheetBody.replaceChildren(...nodes);
  if (sheetCard.kind === "timers" && !ticker && ambient.timers.length) ticker = setInterval(tick, 1000);
  const step = els.sheetBody.querySelector(".step-text");
  if (step) shrinkToFit(step, 24);
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

// --- Touch ------------------------------------------------------------------------------------

els.mic.addEventListener("click", () => send({ type: "talk" }));
els.stop.addEventListener("click", () => send({ type: "stop" }));
els.close.addEventListener("click", hideOverlay);
els.sheetClose.addEventListener("click", closeSheet);
els.menu.addEventListener("click", () => openSheet({ kind: "menu" }));
els.ringingStop.addEventListener("click", () => send({ type: "stop" }));
els.snooze.addEventListener("click", () => send({ type: "snooze" }));
els.sheet.addEventListener("pointerdown", touchSheet);
document.addEventListener("pointerdown", wakeFromNight, true);

startClock();
connect();
