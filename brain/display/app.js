// Claude Home display: follows the brain over a WebSocket and turns its messages into screens.
// URL parameters: ?device=<name> (default "claude-pi"), ?ws=<url> (default: this server's /display socket).

const params = new URLSearchParams(location.search);
const device = params.get("device") || "claude-pi";
const wsUrl = params.get("ws") || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/display`;

const $ = (id) => document.getElementById(id);
const els = {
  time: $("time"), date: $("date"), mic: $("mic"), menu: $("menu"),
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

const handlers = {
  hello: () => {},
  state: (m) => setState(m.state),
  heard: (m) => { els.heard.textContent = m.text; },
  reply: (m) => addReply(m.text),
  ambient: () => {},
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
connect();
