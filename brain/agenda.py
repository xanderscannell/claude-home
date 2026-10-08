"""Calendar: every environment variable named CALENDAR_<LABEL> holds one secret iCal address.

The label ("CALENDAR_KIDS_SPORTS" -> "Kids Sports") is shown next to each event and given to Claude. The
addresses are secrets: never print or log them (httpx errors include the URL, so log the label and the
error's type only).

Feeds are fetched in the background every 15 minutes. A feed that fails keeps its last good copy, and every
refresh re-expands the copies it has, so the window (today through 7 days, local time) stays current.
"""

import asyncio
import datetime as dt
import os
import time

import httpx
import icalendar
import recurring_ical_events
import x_wr_timezone

PREFIX = "CALENDAR_"
REFRESH_S = 900
TIMEOUT_S = 60
DAYS = 7  # how far ahead events are expanded, after today

feeds_raw: dict[str, bytes] = {}  # label -> the last copy that fetched cleanly
events: list[dict] = []  # every event in the window, all feeds, by start time


def feeds(environ=os.environ) -> dict[str, str]:
    """Label -> address for every CALENDAR_<LABEL> variable."""
    return {k.removeprefix(PREFIX).replace("_", " ").strip().title(): v
            for k, v in sorted(environ.items()) if k.startswith(PREFIX) and v.strip()}


def _epoch(value) -> float:
    """A DTSTART/DTEND value as epoch seconds: dates are local midnight, floating times are local time."""
    if not isinstance(value, dt.datetime):
        value = dt.datetime.combine(value, dt.time())
    return value.timestamp()  # naive datetimes count as local time


def expand(label: str, ics: bytes, today: dt.date, days: int = DAYS) -> list[dict]:
    """The feed's events overlapping today through today + days, recurrences expanded, cancellations dropped."""
    cal = x_wr_timezone.to_standard(icalendar.Calendar.from_ical(ics))
    out = []
    for ev in recurring_ical_events.of(cal, skip_bad_series=True).between(today, today + dt.timedelta(days=days + 1)):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start = ev["DTSTART"].dt
        all_day = not isinstance(start, dt.datetime)
        if "DTEND" in ev:
            end = ev["DTEND"].dt
        else:
            end = start + (ev["DURATION"].dt if "DURATION" in ev else dt.timedelta(days=1 if all_day else 0))
        out.append({"title": str(ev.get("SUMMARY", "")).strip() or "Busy", "calendar": label,
                    "start": _epoch(start), "end": _epoch(end), "all_day": all_day})
    return out


def _rebuild(today: dt.date):
    merged = []
    for label, ics in feeds_raw.items():
        try:
            merged += expand(label, ics, today)
        except Exception as e:  # one unreadable feed mustn't take the others down
            print(f"calendar {label}: couldn't read it ({type(e).__name__})")
    merged.sort(key=lambda e: (e["start"], not e["all_day"], e["title"]))
    events[:] = merged


async def refresh(client: httpx.AsyncClient | None = None, environ=os.environ,
                  today: dt.date | None = None) -> list[str]:
    """Fetch every feed (all at once), keep the ones that worked, and re-expand the window.
    Returns the labels that failed, so the caller can try again sooner."""
    sources = feeds(environ)
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
    failed = []

    async def fetch(label: str, url: str):
        try:
            r = await client.get(url.replace("webcal://", "https://", 1))
            r.raise_for_status()
            feeds_raw[label] = r.content
        except httpx.HTTPError as e:
            failed.append(label)
            kept = "keeping the last copy" if label in feeds_raw else "nothing to show from it yet"
            print(f"calendar {label}: fetch failed ({type(e).__name__}); {kept}")

    try:
        await asyncio.gather(*(fetch(label, url) for label, url in sources.items()))
    finally:
        if own:
            await client.aclose()
    for gone in set(feeds_raw) - set(sources):
        del feeds_raw[gone]
    await asyncio.to_thread(_rebuild, today or dt.date.today())  # big feeds take a while to parse; keep the loop free
    return failed


def upcoming(now: float, days: int) -> list[dict]:
    """Events not yet over, from now through the end of the day `days - 1` days from today."""
    until = _epoch(dt.date.fromtimestamp(now) + dt.timedelta(days=days))
    return [e for e in events if e["end"] > now and e["start"] < until]


def today(now: float | None = None, limit: int = 3) -> list[dict]:
    """The rest of today, for the home screen: all-day events, then the next timed ones."""
    rest = upcoming(time.time() if now is None else now, 1)
    return sorted(rest, key=lambda e: not e["all_day"])[:limit]
