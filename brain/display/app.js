// Claude Home display: follows the brain over a WebSocket and turns its messages into screens.
// URL parameters: ?device=<name> (default "claude-pi"), ?ws=<url> (default: this server's /display socket).

const params = new URLSearchParams(location.search);
const device = params.get("device") || "claude-pi";
const wsUrl = params.get("ws") || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/display`;

const $ = (id) => document.getElementById(id);
const els = {
  time: $("time"), date: $("date"), mic: $("mic"), menu: $("menu"), weather: $("weather"), strip: $("strip"),
  overlay: $("overlay"), heard: $("heard"), reply: $("reply"), stop: $("stop"), close: $("close-overlay"),
  offline: $("offline"),
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
  hello: (m) => {
    if (pageVersion === null) pageVersion = m.version;
    else if (m.version !== pageVersion) location.reload();
  },
  state: (m) => setState(m.state),
  heard: (m) => { els.heard.textContent = m.text; },
  reply: (m) => addReply(m.text),
  ambient: (m) => { ambient = m; renderWeather(); renderStrip(); },
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

// Long replies shrink to fit instead of scrolling: 36px down to 24px, then the oldest words scroll off.
function fitReply() {
  let size = parseFloat(getComputedStyle(els.reply).fontSize);
  while (els.reply.scrollHeight > els.reply.clientHeight && size > 24) {
    size -= 2;
    els.reply.style.fontSize = `${size}px`;
  }
  els.reply.scrollTop = els.reply.scrollHeight;
}

// --- Touch ------------------------------------------------------------------------------------

els.mic.addEventListener("click", () => send({ type: "talk" }));
els.stop.addEventListener("click", () => send({ type: "stop" }));
els.close.addEventListener("click", hideOverlay);

startClock();
startPhotos();
connect();
