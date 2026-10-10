"""Home control for the screen: Home Assistant's state as the display's Home cards.

The house is a dict (see index()): Home Assistant's areas, entity and device registries, and states, as its
WebSocket API returns them, keyed by id. Lights, thermostats and automations always show; switches and media
players only when they carry the label "Claude Home" (LABEL). Nothing hidden, disabled, or with an entity_category
ever shows.
"""

import re
from datetime import datetime

LABEL = "claude_home"
HOUSE_THERMOSTAT = "climate.thermostat"  # the one the Home tile sums up
ALWAYS = ("light", "climate", "automation")
LABELLED = ("switch", "media_player")
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
    """The friendly name without its area in front ("Kitchen Cabinets" in the Kitchen is "Cabinets"), and said once
    ("Window AC Window AC" is "Window AC"). A name that is just its area stays whole."""
    n = s["attributes"].get("friendly_name") or s["entity_id"].split(".", 1)[1].replace("_", " ")
    area = area_name(h, s["entity_id"])
    if area and n.lower().startswith(area.lower() + " "):
        n = n[len(area) + 1:]
        n = n[:1].upper() + n[1:]
    return re.sub(r"^(.+) \1$", r"\1", n)


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
    """The setting is the current temperature when Home Assistant has none (off, or a heat_cool range)."""
    a = s["attributes"]
    temp = a.get("current_temperature")
    return unavailable(s, {
        "entity": s["entity_id"], "name": name(h, s), "temp": temp,
        "target": a["temperature"] if a.get("temperature") is not None else temp,
        "mode": s["state"], "modes": a.get("hvac_modes", []),
        "action": a.get("hvac_action") or ("idle" if s["state"] not in ("off", "unavailable") else "off"),
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
