"""Todoist: the shopping list is a Todoist project, so it's the same list in the Todoist app and on the screens.

The brain calls Todoist's REST API (v1) itself, with a personal API token (Todoist: Settings > Integrations >
Developer). Never print or log it. Lists are named by their project name, matched case-insensitively.

Settings: TODOIST_TOKEN, TODOIST_SHOPPING_PROJECT (default "Shopping List").
"""

import asyncio
import json
import os

import httpx

API = "https://api.todoist.com/api/v1"
http = httpx.AsyncClient(base_url=API, timeout=10)  # tests swap in one with a fake transport
projects: dict[str, str] = {}  # project name, casefolded -> id


class TodoistError(Exception):
    """Something to tell the person, in words Claude can pass on."""


def shopping() -> str:
    return os.environ.get("TODOIST_SHOPPING_PROJECT", "Shopping List")


async def call(method: str, path: str, **kw):
    token = os.environ.get("TODOIST_TOKEN")
    if not token:
        raise TodoistError("Todoist isn't set up.")
    try:
        r = await http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kw)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise TodoistError("Todoist turned down the API token." if e.response.status_code in (401, 403)
                           else f"Todoist had a problem ({e.response.status_code}).") from e
    except httpx.HTTPError as e:
        raise TodoistError("Todoist isn't answering right now.") from e
    return r.json() if r.content else None


async def pages(path: str, **params) -> list[dict]:
    """Every result of a paginated GET."""
    out, cursor = [], None
    while True:
        data = await call("GET", path, params={**params, "limit": 200} | ({"cursor": cursor} if cursor else {}))
        out += data["results"]
        if not (cursor := data.get("next_cursor")):
            return out


async def project_id(name: str) -> str:
    if name.casefold() not in projects:
        projects.update({p["name"].casefold(): p["id"] for p in await pages("/projects")})
    if name.casefold() not in projects:
        raise TodoistError(f'There\'s no "{name}" project in Todoist.')
    return projects[name.casefold()]


async def sections(project: str) -> list[dict]:
    """A project's sections (the shopping list's categories) in Todoist's order, as {"id", "name"}."""
    found = await pages("/sections", project_id=await project_id(project))
    return [{"id": x["id"], "name": x["name"]} for x in sorted(found, key=lambda x: x["section_order"])]


async def tasks(project: str) -> list[dict]:
    """A project's open tasks in Todoist's order, those in no section first, as {"id", "content", "section"}
    ("section" is its name, "" for none)."""
    found, secs = await asyncio.gather(pages("/tasks", project_id=await project_id(project)), sections(project))
    order = {x["id"]: (i, x["name"]) for i, x in enumerate(secs)}
    found.sort(key=lambda t: (order.get(t["section_id"], (-1,))[0], t["child_order"]))
    return [{"id": t["id"], "content": t["content"], "section": order.get(t["section_id"], (-1, ""))[1]} for t in found]


def match(said: str, secs: list[dict]) -> dict | None:
    """The section someone means: the same name, else one containing it or contained in it ("dairy" for
    "Meat & Dairy"). Case-insensitive."""
    said = said.strip().casefold()
    if not said:
        return None
    return (next((x for x in secs if x["name"].casefold() == said), None)
            or next((x for x in secs if said in x["name"].casefold() or x["name"].casefold() in said), None))


async def add(project: str, items: list[tuple[str, str]]) -> list[str]:
    """Add (name, section) tasks, skipping any already open in the project (case-insensitive). A section that
    matches none goes in as no section. Returns the names that were added without the section asked for."""
    have = {t["content"].casefold() for t in await tasks(project)}
    secs, pid, unplaced = await sections(project), await project_id(project), []
    for name, section in ((n.strip(), s) for n, s in items):
        if name and name.casefold() not in have:
            where = match(section, secs)
            if section.strip() and not where:
                unplaced.append(name)
            await call("POST", "/tasks", json={"content": name, "project_id": pid} | (
                {"section_id": where["id"]} if where else {}))
            have.add(name.casefold())
    return unplaced


async def check_off(project: str, names: list[str]) -> list[str]:
    """Complete open tasks by name (case-insensitive). Returns the names that weren't there."""
    open_ = {t["content"].casefold(): t["id"] for t in await tasks(project)}
    missing = []
    for name in names:
        if task_id := open_.pop(name.strip().casefold(), None):
            await call("POST", f"/tasks/{task_id}/close")
        else:
            missing.append(name)
    return missing


def fake() -> dict:
    """For tests: answer from an in-memory Todoist whose "Shopping List" project has the sections Produce,
    Household, and Meat & Dairy (Todoist can report equal section_orders; ties keep Todoist's order). Returns its
    tasks by id."""
    store: dict[str, dict] = {}
    ids = iter(range(1, 10**9))
    secs = [{"id": "s1", "name": "Produce", "section_order": 1}, {"id": "s3", "name": "Meat & Dairy", "section_order": 2},
            {"id": "s2", "name": "Household", "section_order": 1}]

    def handle(req: httpx.Request) -> httpx.Response:
        path, params = req.url.path.removeprefix("/api/v1"), req.url.params
        page = lambda results: httpx.Response(200, json={"results": results, "next_cursor": None})  # noqa: E731
        if path == "/projects":
            return page([{"id": "p1", "name": "Shopping List"}])
        if path == "/sections":
            return page(secs if params["project_id"] == "p1" else [])
        if path == "/tasks" and req.method == "GET":
            return page([t for t in store.values() if t["project_id"] == params["project_id"]][::-1])
        if path == "/tasks":
            task = {"id": str(next(ids)), "section_id": None, **json.loads(req.content)}
            task["child_order"] = 1 + sum(t["section_id"] == task["section_id"] for t in store.values())
            store[task["id"]] = task
            return httpx.Response(200, json=task)
        if path.endswith("/close"):
            store.pop(path.split("/")[2])
            return httpx.Response(204)
        return httpx.Response(404)

    global http
    http = httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handle))
    projects.clear()
    os.environ.setdefault("TODOIST_TOKEN", "test")
    return store
