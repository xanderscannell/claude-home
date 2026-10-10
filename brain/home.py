"""Home control for the screen: Home Assistant's state as the display's Home cards.

The house is a dict (see index()): Home Assistant's areas, entity and device registries, and states, as its
WebSocket API returns them, keyed by id. Lights, thermostats and automations always show; switches and media
players only when they carry the label "Claude Home" (LABEL). Nothing hidden, disabled, or with an entity_category
ever shows.
"""

import asyncio
import contextlib
import itertools
import json
import math
import re
from datetime import datetime

import websockets

LABEL = "claude_home"
HOUSE_THERMOSTAT = "climate.thermostat"  # the one the Home tile sums up
ALWAYS = ("light", "climate", "automation")
LABELLED = ("switch", "media_player")
DOMAINS = ALWAYS + LABELLED
VOLUME_SET = 4  # media_player supported_features bit


def index(raw: dict) -> dict:
    """{areas, entities, devices, states} as lists (the WebSocket results) to the house dict."""
    h = {"areas": {a["area_id"]: a["name"] for a in raw["areas"]},
         "entities": {e["entity_id"]: e for e in raw["entities"]},
         "devices": {d["id"]: d for d in raw["devices"]},
         "states": {}, "seen": {}}
    for s in raw["states"]:
        remember(h, s)
    return h


def remember(h: dict, s: dict):
    """A new state for an entity. A light's brightness is kept while it's on, so it can be shown while it's off."""
    h["states"][s["entity_id"]] = s
    if s["entity_id"].startswith("light.") and s["state"] == "on" and s["attributes"].get("brightness") is not None:
        h["seen"][s["entity_id"]] = pct(s["attributes"]["brightness"])


def pct(brightness: int) -> int:
    """0-255 to 1-100: a light that's on never reads 0%"""
    return max(1, min(100, round(brightness * 100 / 255)))


def shown(h: dict, eid: str) -> bool:
    e = h["entities"].get(eid, {})
    if e.get("entity_category") or e.get("hidden_by") or e.get("disabled_by"):
        return False
    domain = eid.split(".")[0]
    return domain in ALWAYS or (domain in LABELLED and LABEL in e.get("labels", []))


def area_id(h: dict, eid: str) -> str | None:
    e = h["entities"].get(eid, {})
    return e.get("area_id") or h["devices"].get(e.get("device_id"), {}).get("area_id")


def area_name(h: dict, eid: str) -> str:
    return h["areas"].get(area_id(h, eid), "")


def name(h: dict, s: dict) -> str:
    """The friendly name without its area in front ("Kitchen Cabinets" in the Kitchen is "Cabinets"), said once
    ("Window AC Window AC" is "Window AC"), and not shouted ("THERMOSTAT" is "Thermostat", "TV" stays). A name that
    is just its area stays whole."""
    n = " ".join((s["attributes"].get("friendly_name") or s["entity_id"].split(".", 1)[1].replace("_", " ")).split())
    area = area_name(h, s["entity_id"])
    if area and n.lower().startswith(area.lower() + " "):
        n = n[len(area) + 1:]
        n = n[:1].upper() + n[1:]
    n = re.sub(r"^(.+) \1$", r"\1", n)
    return n.capitalize() if n.isupper() and " " not in n and len(n) > 4 else n


def unavailable(s: dict, d: dict) -> dict:
    if s["state"] == "unavailable":
        d["unavailable"] = True
    return d


def light(h: dict, s: dict) -> dict:
    """A light, or a switch (which never dims). A dimmer that's off shows the brightness it last had, or 100."""
    eid, a = s["entity_id"], s["attributes"]
    d = {"entity": eid, "name": name(h, s), "on": s["state"] == "on", "brightness": None}
    if eid.startswith("light.") and set(a.get("supported_color_modes") or ["onoff"]) - {"onoff"}:
        d["brightness"] = (pct(a["brightness"]) if d["on"] and a.get("brightness") is not None
                           else h["seen"].get(eid, 100))
    return unavailable(s, d)


def player(h: dict, s: dict) -> dict:
    a = s["attributes"]
    volume = (round(a["volume_level"] * 100) if a.get("supported_features", 0) & VOLUME_SET
              and a.get("volume_level") is not None else None)
    return unavailable(s, {"entity": s["entity_id"], "room": area_name(h, s["entity_id"]), "name": name(h, s),
                           "state": s["state"], "title": a.get("media_title"), "artist": a.get("media_artist"),
                           "app": a.get("app_name"), "volume": volume})


def thermostat(h: dict, s: dict) -> dict:
    """The setting is the current temperature when Home Assistant has none (off, or a heat_cool range). Off is off,
    whatever hvac_action says (some report idle while off)."""
    a = s["attributes"]
    temp = a.get("current_temperature")
    return unavailable(s, {
        "entity": s["entity_id"], "name": name(h, s), "temp": temp,
        "target": a["temperature"] if a.get("temperature") is not None else temp,
        "mode": s["state"], "modes": a.get("hvac_modes", []),
        "action": "off" if s["state"] in ("off", "unavailable") else a.get("hvac_action") or "idle",
        "min": a.get("min_temp", 45), "max": a.get("max_temp", 95), "step": a.get("target_temp_step"),
        "humidity": a.get("current_humidity")})


def automation(h: dict, s: dict) -> dict:
    ran = s["attributes"].get("last_triggered")
    return {"entity": s["entity_id"], "name": s["attributes"].get("friendly_name") or s["entity_id"],
            "on": s["state"] == "on", "last_ran": datetime.fromisoformat(ran).timestamp() if ran else None}


KINDS = {"light": ("lights", light), "switch": ("switches", light), "media_player": ("media", player),
         "climate": ("climate", thermostat)}


def devices(h: dict) -> dict:
    """Every shown device by area id (None: no area): {area: {lights, switches, media, climate}}, each by name."""
    out = {}
    for eid, s in h["states"].items():
        domain = eid.split(".")[0]
        if domain in KINDS and shown(h, eid):
            kind, build = KINDS[domain]
            out.setdefault(area_id(h, eid), {k: [] for k, _ in KINDS.values()})[kind].append(build(h, s))
    for room in out.values():
        for kind in room.values():
            kind.sort(key=lambda d: d["name"].lower())
    return out


def areas(h: dict, by_area: dict) -> list[tuple[str, str]]:
    """(id, name) of the areas with something shown in them, by name"""
    return sorted(((a, h["areas"][a]) for a in by_area if a in h["areas"]), key=lambda a: a[1].lower())


def on(d: dict) -> bool:
    return d["on"] and not d.get("unavailable")


def thermostats(h: dict, by_area: dict) -> list[dict]:
    """The house thermostat first, then the rest by name"""
    return sorted((t for room in by_area.values() for t in room["climate"]),
                  key=lambda t: (t["entity"] != HOUSE_THERMOSTAT, t["name"].lower()))


def players(h: dict, by_area: dict) -> list[dict]:
    return sorted((p for room in by_area.values() for p in room["media"]),
                  key=lambda p: (not p["room"], p["room"].lower(), p["name"].lower()))


def playing(p: dict) -> str:
    """How the Home tile names a playing player: "Den TV", or just "Studio" for the Studio's own"""
    return p["name"] if not p["room"] or p["name"].lower().startswith(p["room"].lower()) else f"{p['room']} {p['name']}"


def card(h: dict, m: dict) -> dict | None:
    """The card for an open (or fetch) of a Home sheet, or None for a sheet that isn't one (or a room that's gone)."""
    by_area = devices(h)
    rooms = areas(h, by_area)
    sheet = m.get("sheet")
    if sheet == "house":
        lights = [l for room in by_area.values() for l in room["lights"]]
        ts, ps = thermostats(h, by_area), players(h, by_area)
        autos = [automation(h, s) for eid, s in h["states"].items() if eid.startswith("automation.") and shown(h, eid)]
        return {"kind": "house",
                "lights": {"on": sum(map(on, lights)), "total": len(lights),
                           "rooms": [n for a, n in rooms if any(map(on, by_area[a]["lights"]))]},
                "climate": {k: ts[0][k] for k in ("temp", "target", "action")} if ts else None,
                "media": {"playing": [playing(p) for p in ps if p["state"] == "playing"], "total": len(ps)},
                "rooms": [n for _, n in rooms],
                "automations": {"on": sum(a["on"] for a in autos), "total": len(autos)}}
    if sheet == "lights":
        groups = [(n, by_area[a]["lights"]) for a, n in rooms]
        if None in by_area:
            groups.append(("Other", by_area[None]["lights"]))
        return {"kind": "lights", "rooms": [{"name": n, "lights": ls} for n, ls in groups if ls]}
    if sheet == "rooms":
        return {"kind": "rooms", "rooms": [
            {"id": a, "name": n, "lights": len(r["lights"]), "lights_on": sum(map(on, r["lights"])),
             "playing": [p["name"] for p in r["media"] if p["state"] == "playing"],
             "switched_on": [s["name"] for s in r["switches"] if on(s)]}
            for a, n in rooms for r in [by_area[a]]]}
    if sheet == "room":
        if m.get("room") not in dict(rooms):
            return None
        return {"kind": "room", "name": h["areas"][m["room"]], **by_area[m["room"]]}
    if sheet == "climate":
        return {"kind": "climate", "thermostats": thermostats(h, by_area)}
    if sheet == "media":
        return {"kind": "media", "players": players(h, by_area)}
    if sheet == "automations":
        autos = [automation(h, s) for eid, s in h["states"].items() if eid.startswith("automation.") and shown(h, eid)]
        return {"kind": "automations", "automations": sorted(autos, key=lambda a: a["name"].lower())}
    return None


SHEETS = ("house", "lights", "rooms", "room", "climate", "media", "automations")
MEDIA = {"play": "media_play", "pause": "media_pause", "turn_on": "turn_on", "turn_off": "turn_off"}


def number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def service(h: dict, m: dict) -> tuple[str, str, dict] | None:
    """A tap on a Home page as the Home Assistant service to call: (domain, service, data), or None to ignore it.
    The screen is untrusted: an entity is used only if it's one the screen is shown, in the domain the tap is for (a
    light tap naming the car's remote start does nothing). media without an entity is Spotify's, not this."""
    kind, eid = m.get("type"), m.get("entity")
    if kind == "lights_off":
        ids = [l["entity"] for room in devices(h).values() for l in room["lights"] if on(l)]
        return ("light", "turn_off", {"entity_id": ids}) if ids else None
    if not isinstance(eid, str) or eid not in h["states"] or not shown(h, eid):
        return None
    domain, a, target = eid.split(".")[0], h["states"][eid]["attributes"], {"entity_id": eid}
    if kind == "light" and domain in ("light", "switch") and isinstance(m.get("on"), bool):
        if m["on"] and domain == "light" and number(m.get("brightness")):
            return "light", "turn_on", {**target, "brightness_pct": max(1, min(100, round(m["brightness"])))}
        return domain, "turn_on" if m["on"] else "turn_off", target
    if kind == "climate" and domain == "climate":
        if number(m.get("target")):
            return "climate", "set_temperature", {
                **target, "temperature": max(a.get("min_temp", 45), min(a.get("max_temp", 95), m["target"]))}
        if m.get("mode") in a.get("hvac_modes", []):
            return "climate", "set_hvac_mode", {**target, "hvac_mode": m["mode"]}
    if kind == "media" and domain == "media_player":
        if m.get("action") in MEDIA:
            return "media_player", MEDIA[m["action"]], target
        if number(m.get("volume")):
            return "media_player", "volume_set", {**target, "volume_level": max(0, min(100, m["volume"])) / 100}
    if kind == "automation_run" and domain == "automation":
        return "automation", "trigger", target
    if kind == "automation_toggle" and domain == "automation" and isinstance(m.get("enabled"), bool):
        return "automation", "turn_on" if m["enabled"] else "turn_off", target
    return None


# --- The connection ------------------------------------------------------------------------------

class HAError(Exception):
    """Home Assistant refused a request, or isn't connected."""


def socket(url: str):
    """Home Assistant's WebSocket API, not yet authenticated: use as `async with socket(url) as ha`, then auth.
    max_size: a whole house's states are well over websockets' 1 MiB default."""
    return websockets.connect(url.replace("http", "ws", 1).rstrip("/") + "/api/websocket", max_size=2**24)


async def auth(ha, token: str):
    await ha.recv()  # auth_required
    await ha.send(json.dumps({"type": "auth", "access_token": token}))
    if json.loads(await ha.recv())["type"] != "auth_ok":
        raise PermissionError("Home Assistant refused HA_TOKEN")


REGISTRY_EVENTS = ("entity_registry_updated", "device_registry_updated", "area_registry_updated")


class Connection:
    """Home Assistant kept open: the house in memory (None while disconnected), kept current by its events, and
    service calls over the same socket. on_change(entity_ids) is told what changed (None: anything may have, after a
    connect or a registry change); it's called from the reader, so it must return at once."""

    def __init__(self, url: str, token: str, on_change=None, backoff: float = 5.0):
        self.url, self.token, self.on_change, self.backoff = url, token, on_change, backoff
        self.house: dict | None = None
        self.ws = None
        self.ids = itertools.count(1)
        self.waiting: dict[int, asyncio.Future] = {}
        self.early: list[dict] = []  # state changes that came in while the house was loading
        self.tasks: set = set()

    async def run(self):
        """Stay connected: reconnect after a drop or a refusal, 5 s doubling to a minute apart."""
        delay, said = self.backoff, ""
        while True:
            try:
                async with socket(self.url) as ha:
                    await auth(ha, self.token)
                    self.ws = ha
                    reader = asyncio.create_task(self.read(ha))
                    try:
                        for event in ("state_changed", *REGISTRY_EVENTS):
                            await self.request(type="subscribe_events", event_type=event)
                        await self.load()
                        delay, said = self.backoff, ""
                        print("home assistant: connected")
                        self.changed(None)
                        await reader
                    finally:
                        reader.cancel()
                        self.give_up()
                        self.ws, self.house, self.early = None, None, []
            except (OSError, TimeoutError, PermissionError, HAError, websockets.WebSocketException) as e:
                if repr(e) != said:  # say each problem once, not every retry
                    print(f"home assistant: {e!r}; reconnecting")
                    said = repr(e)
            self.changed(None)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def load(self):
        raw = {"states": await self.request(type="get_states"), **await self.registries()}
        house = index(raw)
        for event in self.early:
            self.apply(house, event)
        self.house, self.early = house, []

    async def registries(self) -> dict:
        return {"areas": await self.request(type="config/area_registry/list"),
                "entities": await self.request(type="config/entity_registry/list"),
                "devices": await self.request(type="config/device_registry/list")}

    async def reload_registries(self):
        try:
            raw = await self.registries()
        except HAError:
            return  # disconnected meanwhile: the reconnect reads everything
        if self.house is not None:
            fresh = index({**raw, "states": []})
            self.house.update(areas=fresh["areas"], entities=fresh["entities"], devices=fresh["devices"])
            self.changed(None)

    async def request(self, **msg):
        """One command; its result, or HAError if Home Assistant refused it."""
        if self.ws is None:
            raise HAError("Home Assistant isn't connected")
        i = next(self.ids)
        self.waiting[i] = fut = asyncio.get_running_loop().create_future()
        try:
            await self.ws.send(json.dumps({"id": i, **msg}))
            return await asyncio.wait_for(fut, 15)
        except (websockets.ConnectionClosed, TimeoutError) as e:
            raise HAError(f"Home Assistant didn't answer ({e!r})") from e
        finally:
            self.waiting.pop(i, None)

    async def call(self, domain: str, service: str, data: dict):
        return await self.request(type="call_service", domain=domain, service=service, service_data=data)

    async def read(self, ha):
        with contextlib.suppress(websockets.ConnectionClosed):
            async for raw in ha:
                m = json.loads(raw)
                if m.get("type") == "result" and (fut := self.waiting.get(m.get("id"))) and not fut.done():
                    if m["success"]:
                        fut.set_result(m.get("result"))
                    else:
                        fut.set_exception(HAError(m.get("error", {}).get("message", "failed")))
                elif m.get("type") == "event":
                    self.event(m["event"])
        self.give_up()

    def give_up(self):
        """Disconnected: nothing more is coming for the requests still waiting"""
        for fut in self.waiting.values():
            fut.done() or fut.set_exception(HAError("Home Assistant disconnected"))

    def event(self, ev: dict):
        if ev.get("event_type") in REGISTRY_EVENTS:
            task = asyncio.create_task(self.reload_registries())  # not awaited here: the reader must keep reading
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        elif ev.get("event_type") == "state_changed":
            if self.house is None:
                self.early.append(ev)
            else:
                self.apply(self.house, ev)
                self.changed({ev["data"]["entity_id"]})

    @staticmethod
    def apply(house: dict, ev: dict):
        if (new := ev["data"].get("new_state")) is None:  # removed
            house["states"].pop(ev["data"]["entity_id"], None)
        else:
            remember(house, new)

    def changed(self, ids: set | None):
        if self.on_change:
            self.on_change(ids)


async def snapshot(url: str, token: str) -> dict:
    """The house read once, read only: the registries and states, nothing else asked of Home Assistant."""
    async with socket(url) as ha:
        await auth(ha, token)
        raw = {}
        for i, (key, kind) in enumerate([("areas", "config/area_registry/list"), ("entities", "config/entity_registry/list"),
                                         ("devices", "config/device_registry/list"), ("states", "get_states")], 1):
            await ha.send(json.dumps({"id": i, "type": kind}))
            while (m := json.loads(await ha.recv())).get("id") != i:
                pass
            raw[key] = m["result"]
        return index(raw)


def show(h: dict):
    """Every Home card, readably"""
    def onoff(d):
        return ("unavailable" if d.get("unavailable") else "on" if d["on"] else "off") + (
            f" {d['brightness']}%" if d.get("brightness") is not None and d["on"] else "")

    house = card(h, {"sheet": "house"})
    print(f"HOME  lights {house['lights']['on']}/{house['lights']['total']} on in {house['lights']['rooms']}"
          f" | climate {house['climate']} | playing {house['media']['playing']} of {house['media']['total']}"
          f" | automations {house['automations']['on']}/{house['automations']['total']}")
    print("\nLIGHTS")
    for r in card(h, {"sheet": "lights"})["rooms"]:
        print(f"  {r['name']}: " + ", ".join(f"{l['name']} ({onoff(l)})" for l in r["lights"]))
    print("\nROOMS")
    for r in card(h, {"sheet": "rooms"})["rooms"]:
        room = card(h, {"sheet": "room", "room": r["id"]})
        print(f"  {r['name']} [{r['id']}]: {r['lights_on']}/{r['lights']} lights on, playing {r['playing']},"
              f" on {r['switched_on']}; players {[p['name'] for p in room['media']]},"
              f" thermostats {[t['name'] for t in room['climate']]}, other {[s['name'] for s in room['switches']]}")
    print("\nCLIMATE")
    for t in card(h, {"sheet": "climate"})["thermostats"]:
        print(f"  {t['name']}: {t['temp']}° now, set {t['target']}°, {t['mode']} of {t['modes']}, {t['action']},"
              f" {t['min']}-{t['max']} step {t['step']}, humidity {t['humidity']}" + (" UNAVAILABLE" if t.get("unavailable") else ""))
    print("\nSPEAKERS & TVS")
    for p in card(h, {"sheet": "media"})["players"]:
        print(f"  {p['room'] or '(no room)'} / {p['name']}: {p['state']}, {p['title']!r} {p['artist'] or p['app'] or ''},"
              f" volume {p['volume']}")
    print("\nAUTOMATIONS")
    for a in card(h, {"sheet": "automations"})["automations"]:
        ran = datetime.fromtimestamp(a["last_ran"]).strftime("%b %d %H:%M") if a["last_ran"] else "never"
        print(f"  {'on ' if a['on'] else 'off'} {a['name']} (ran {ran})")


if __name__ == "__main__":
    # Prints every card from the live house, read only: uv run --env-file ../.env python home.py
    import os
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    show(asyncio.run(snapshot(os.environ["HA_URL"], os.environ.get("HA_TOKEN", ""))))
