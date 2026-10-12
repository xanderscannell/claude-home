"""Persona eval: real Claude on the real home tools, with nothing real behind them. Prints every reply with its length
and time to first sentence, then a summary per persona. Not a test: it spends Claude usage, and the replies are yours to
judge (is the routine confirmation bare, is the humor dry and only where it belongs, how often "sir").

Run from brain/:  uv run --env-file ../.env python persona_eval.py personas/jarvis.md none
Each argument is a persona file (its card is the .card.txt beside it) or "none" for the plain assistant.

Timers and alarms go to a temporary database, the shopping list to todoist.fake(), and Home Assistant to a stand-in
MCP server over test_data/ha_house.json (its Reading Light is unavailable, so one command fails). Weather is a real
lookup for HOME_LOCATION. Music isn't connected, so nothing plays.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["CLAUDE_HOME_DB"], os.environ["CLAUDE_HOME_LOG"] = str(TMP / "home.db"), str(TMP / "turns.jsonl")

import brain  # noqa: E402  (after the paths above, which it and tools read on import)
import todoist  # noqa: E402
import tools  # noqa: E402
from claude_agent_sdk import ClaudeSDKClient, create_sdk_mcp_server, tool  # noqa: E402

# What people actually say to it, in one session so the timer can be checked and cancelled.
ROUTINE = ["Turn off the kitchen lights.", "Set the den strip to forty percent.", "Set a timer for ten minutes for the pasta.",
           "How long is left on the pasta timer?", "Add milk and eggs to the shopping list.", "Add oat milk.",
           "What's on the shopping list?", "What's the weather tomorrow?", "Turn on the reading light.",
           "Set the thermostat to seventy two.", "Wake me up at six thirty tomorrow.", "Cancel the pasta timer.",
           "Is the pickup locked?", "Turn off the den fan.", "Thanks."]
# Where the character shows, in a fresh session.
CHAT = ["I don't even remember what I ate today.", "Be honest, should I have a fourth coffee?",
        "What's the capital of Australia?", "Ugh, can this day get any worse?", "How many ounces in a cup?",
        "I'm going to learn the bagpipes.", "Are you Jarvis? What's Tony Stark like?", "Thanks Jarvis."]

HOUSE = json.loads((brain.HERE / "test_data" / "ha_house.json").read_text(encoding="utf-8"))


def house() -> dict:
    """entity_id -> {name, state, area, attributes}, the way GetLiveContext describes a device"""
    areas = {a["area_id"]: a["name"] for a in HOUSE["areas"]}
    device_area = {d["id"]: d.get("area_id") for d in HOUSE["devices"]}
    states = {s["entity_id"]: s for s in HOUSE["states"]}
    return {e["entity_id"]: {"name": s["attributes"].get("friendly_name", e["entity_id"]), "state": s["state"],
                             "area": areas.get(e.get("area_id") or device_area.get(e.get("device_id")), ""),
                             "attributes": {k: v for k, v in s["attributes"].items() if k != "friendly_name"}}
            for e in HOUSE["entities"] if (s := states.get(e["entity_id"]))}


TARGET = {"type": "object", "properties": {"name": {"type": "string"}, "area": {"type": "string"},
                                           "domain": {"type": "array", "items": {"type": "string"}}}}


def _text(d: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(d)}]}


def fake_ha(state: dict):
    """A few of Home Assistant's MCP tools, under the names its server gives them, answering the way its intents do"""
    def act(a: dict, new: str) -> dict:
        name, area, domains = (a.get("name") or "").lower(), (a.get("area") or "").lower(), a.get("domain") or []
        hits = [eid for eid, e in state.items() if name in e["name"].lower() and area in e["area"].lower()
                and (not domains or eid.split(".")[0] in domains)]
        if not hits:
            return _text({"response_type": "error", "data": {"code": "no_valid_targets"},
                          "speech": {"plain": {"speech": "No device matched"}}})
        done = [eid for eid in hits if state[eid]["state"] != "unavailable"]
        for eid in done:
            state[eid]["state"] = new
        return _text({"response_type": "action_done", "data": {
            "success": [{"name": state[e]["name"], "id": e} for e in done],
            "failed": [{"name": state[e]["name"], "id": e} for e in hits if e not in done]}})

    @tool("homeassistant__GetLiveContext", "Live state of every device exposed to Assist, by area.",
          {"type": "object", "properties": {}})
    async def live(a):
        return _text(list(state.values()))

    @tool("intent__HassTurnOn", "Turns on/opens/locks a device or entity.", TARGET)
    async def turn_on(a):
        return act(a, "on")

    @tool("intent__HassTurnOff", "Turns off/closes/unlocks a device or entity.", TARGET)
    async def turn_off(a):
        return act(a, "off")

    @tool("light__HassLightSet", "Sets the brightness percentage or color of a light.",
          {**TARGET, "properties": {**TARGET["properties"], "brightness": {"type": "integer"}}})
    async def light_set(a):
        return act(a, "on")

    @tool("climate__HassClimateSetTemperature", "Sets the target temperature of a climate device.",
          {"type": "object", "properties": {"temperature": {"type": "number"}, "name": {"type": "string"}}})
    async def set_temperature(a):
        state["climate.thermostat"]["attributes"]["temperature"] = a["temperature"]
        return _text({"response_type": "action_done",
                      "data": {"success": [{"name": "Thermostat", "id": "climate.thermostat"}], "failed": []}})

    return create_sdk_mcp_server("ha", tools=[live, turn_on, turn_off, light_set, set_temperature])


async def session(system: str, prompts: list[str]) -> list[dict]:
    """A fresh house, list, and timers; each prompt's reply, length, "sir", and seconds to the first sentence"""
    for table in ("timers", "alarms", "memory"):
        tools.q(f"DELETE FROM {table}")
    todoist.fake()
    servers = {"home": tools.make_tools("eval", categories=await tools.shopping_categories()), "ha": fake_ha(house())}
    rows = []
    async with ClaudeSDKClient(replace(brain.OPTIONS, system_prompt=system, mcp_servers=servers)) as client:
        for p in prompts:
            start, first, said = time.monotonic(), None, []
            async for s in brain.ask(client, p):
                first = first or time.monotonic() - start
                said.append(s)
            reply = " ".join(said)
            rows.append({"you": p, "reply": reply, "words": len(reply.split()), "first": first or 0,
                         "sir": bool(re.search(r"\bsir\b", reply, re.I)), "question": reply.rstrip().endswith("?")})
    return rows


def report(label: str, rows: list[dict]) -> None:
    print(f"\n== {label}")
    for r in rows:
        print(f"  {r['you']}\n    -> {r['reply']}   [{r['words']} words, {r['first']:.2f}s]")
    n = len(rows)
    print(f"  {label}: {sum(r['words'] for r in rows) / n:.1f} words on average, sir in {sum(r['sir'] for r in rows)}/{n},"
          f" {sum(r['question'] for r in rows)}/{n} end in a question,"
          f" median first sentence {sorted(r['first'] for r in rows)[n // 2]:.2f}s")


async def main(personas: list[str]):
    for arg in personas:
        if arg == "none":
            system, brain.VOICE_CHECK = brain.SYSTEM_PROMPT, ""
        else:
            md = Path(arg)
            system = brain.SYSTEM_PROMPT + f"\n\n<persona>\n{md.read_text(encoding='utf-8')}</persona>"
            brain.VOICE_CHECK = f"[{md.with_suffix('.card.txt').read_text(encoding='utf-8').strip()}] "
        report(f"{arg}, routine", await session(system, ROUTINE))
        report(f"{arg}, chat", await session(system, CHAT))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["personas/jarvis.md"]))
