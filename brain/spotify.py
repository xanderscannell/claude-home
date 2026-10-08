"""Spotify: control playback on the Pi's Spotify Connect speaker (raspotify) through the Web API.

Auth is the authorization code flow with PKCE, so there is no client secret: open /spotify/login on the
brain's own machine once (the redirect is http://127.0.0.1:8765/spotify/callback). Tokens are kept in
spotify_token.json as {"access_token", "refresh_token", "expires_at"} and refreshed within a minute of
expiry. Never print or log them.

Settings: SPOTIFY_CLIENT_ID, SPOTIFY_DEVICE (default "Claude Home", the speaker's name in Spotify), SPOTIFY_TOKEN
(where the tokens are kept; default brain/spotify_token.json).
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import httpx

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"
REDIRECT = "http://127.0.0.1:8765/spotify/callback"
TOKEN_PATH = Path(os.environ.get("SPOTIFY_TOKEN") or Path(__file__).parent / "spotify_token.json")
KINDS = ("track", "artist", "album", "playlist")


def verifier() -> str:
    """A PKCE code verifier: 43 to 128 characters from the unreserved set (token_urlsafe gives 86 here)."""
    return secrets.token_urlsafe(64)


def challenge(code_verifier: str) -> str:
    """The S256 code challenge for a verifier."""
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def login_url(client_id: str, code_verifier: str, state: str, redirect: str = REDIRECT) -> str:
    return f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge(code_verifier), "state": state})


class SpotifyError(Exception):
    """Something to tell the person, in words Claude can pass on."""


def track(playback: dict | None) -> dict | None:
    """Current playback (GET /me/player) as the screens show it, or None when there's nothing to show."""
    item = (playback or {}).get("item")
    if not item:
        return None
    if item.get("type") == "episode":
        show = item.get("show") or {}
        artist, album, images = show.get("name", ""), "", item.get("images") or show.get("images") or []
    else:
        album_info = item.get("album") or {}
        artist = ", ".join(a["name"] for a in item.get("artists", []))
        album, images = album_info.get("name", ""), album_info.get("images") or []
    big_enough = [i for i in images if (i.get("width") or 0) >= 300]
    art = min(big_enough, key=lambda i: i["width"]) if big_enough else (images[0] if images else None)
    return {"title": item.get("name", ""), "artist": artist, "album": album, "art": art and art["url"],
            "uri": item.get("uri"), "is_playing": bool(playback.get("is_playing")),
            "progress_ms": playback.get("progress_ms") or 0, "duration_ms": item.get("duration_ms") or 0}


def moved(old: dict | None, new: dict | None, elapsed_s: float) -> bool:
    """Whether screens need fresh now-playing info: another track, play/pause, or a jump in position (a seek).
    Plain progress isn't a change: the screens advance it themselves."""
    if old is None or new is None:
        return old != new
    if (old["uri"], old["is_playing"]) != (new["uri"], new["is_playing"]):
        return True
    expected = old["progress_ms"] + (elapsed_s * 1000 if old["is_playing"] else 0)
    return abs(new["progress_ms"] - expected) > 3000


class Spotify:
    def __init__(self, client_id: str, token_path: Path = TOKEN_PATH, transport: httpx.AsyncBaseTransport | None = None,
                 clock=time.time, device_name: str | None = None):
        self.client_id = client_id
        self.token_path = Path(token_path)
        self.clock = clock
        self.device_name = device_name or os.environ.get("SPOTIFY_DEVICE", "Claude Home")
        self.http = httpx.AsyncClient(transport=transport, timeout=10)
        self.blocked_until = 0.0  # after a 429, no requests until then
        self.changed = asyncio.Event()  # set after a control, so the now-playing poller looks again soon
        self.poll_s = 0.5  # between looks while confirming that play started

    async def close(self):
        await self.http.aclose()

    # --- Tokens ---------------------------------------------------------------------------------

    def _save(self, t: dict):
        tmp = self.token_path.with_name(self.token_path.name + ".tmp")
        tmp.write_text(json.dumps(t))
        tmp.replace(self.token_path)  # never a half-written token file

    async def _token_request(self, data: dict) -> dict:
        r = await self.http.post(f"{ACCOUNTS}/api/token", data={**data, "client_id": self.client_id})
        if r.status_code != 200:
            raise SpotifyError("Spotify sign-in has lapsed. Open /spotify/login on the brain to connect again.")
        return r.json()

    async def exchange(self, code: str, code_verifier: str, redirect: str = REDIRECT):
        """The login callback's code for tokens."""
        got = await self._token_request({"grant_type": "authorization_code", "code": code,
                                         "redirect_uri": redirect, "code_verifier": code_verifier})
        self._save({"access_token": got["access_token"], "refresh_token": got["refresh_token"],
                    "expires_at": self.clock() + got["expires_in"]})

    async def access_token(self, force_refresh: bool = False) -> str:
        try:
            t = json.loads(self.token_path.read_text())
        except (OSError, ValueError):
            raise SpotifyError("Spotify isn't connected yet. Open /spotify/login on the brain.") from None
        if force_refresh or t["expires_at"] - self.clock() < 60:
            got = await self._token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
            t = {"access_token": got["access_token"], "refresh_token": got.get("refresh_token", t["refresh_token"]),
                 "expires_at": self.clock() + got["expires_in"]}
            self._save(t)
        return t["access_token"]

    # --- Requests -------------------------------------------------------------------------------

    async def call(self, method: str, path: str, **kw) -> dict | None:
        """One Web API request. 204 gives None. A 429 blocks every request until Retry-After has passed."""
        if self.clock() < self.blocked_until:
            raise SpotifyError(f"Spotify asked us to slow down; try again in {round(self.blocked_until - self.clock())} seconds.")
        for retry in (False, True):
            headers = {"Authorization": f"Bearer {await self.access_token(force_refresh=retry)}"}
            r = await self.http.request(method, f"{API}{path}", headers=headers, **kw)
            if r.status_code != 401:
                break  # 401 once: the token was revoked or expired early; refresh and try again
        if r.status_code == 429:
            self.blocked_until = self.clock() + int(r.headers.get("Retry-After", "5"))
            raise SpotifyError("Spotify asked us to slow down; try again shortly.")
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code >= 400:
            try:
                why = r.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                why = r.reason_phrase
            raise SpotifyError(f"Spotify said: {why} ({r.status_code}).")
        try:
            return r.json()
        except ValueError:
            return None  # the player commands can answer 200 with a body that isn't JSON

    async def devices(self) -> list[dict]:
        return (await self.call("GET", "/me/player/devices") or {}).get("devices", [])

    async def playback(self) -> dict | None:
        """What's playing (or paused) now, or None when nothing is."""
        return await self.call("GET", "/me/player")

    async def search(self, query: str, kind: str = "track") -> dict:
        """The best match: {uri, name, by}."""
        if kind not in KINDS:
            raise SpotifyError(f"Can't search Spotify for a {kind}.")
        got = await self.call("GET", "/search", params={"q": query, "type": kind, "limit": 1})
        items = [i for i in (got or {}).get(f"{kind}s", {}).get("items", []) if i]
        if not items:
            raise SpotifyError(f"Nothing on Spotify matched {query!r}.")
        i = items[0]
        by = ", ".join(a["name"] for a in i.get("artists", [])) or (i.get("owner") or {}).get("display_name", "")
        return {"uri": i["uri"], "name": i["name"], "by": by}

    async def speaker(self) -> dict:
        for d in await self.devices():
            if d.get("name") == self.device_name:
                return d
        raise SpotifyError(f"The {self.device_name} speaker isn't showing up on Spotify. Is raspotify running on the Pi?")

    async def started(self) -> bool:
        """Whether Spotify reports the speaker playing within 12 looks (6 s; it took about 1.5 s on the Pi)."""
        for _ in range(12):
            p = await self.playback()
            if p and p.get("is_playing") and (p.get("device") or {}).get("name") == self.device_name:
                return True
            await asyncio.sleep(self.poll_s)
        return False

    async def play(self, query: str, kind: str = "track") -> dict:
        """Find something and play it on the speaker, moving playback there first if it isn't active.

        Tried twice: librespot (0.8.0) only notices that Spotify closed its session when the next command arrives.
        That command wakes it up (it logs in again in about 2 s) but is itself lost. Seen on the Pi 2026-10-08."""
        found = await self.search(query, kind)
        body = {"uris": [found["uri"]]} if kind == "track" else {"context_uri": found["uri"]}
        for _ in range(2):
            dev = await self.speaker()
            if not dev.get("is_active"):
                await self.call("PUT", "/me/player", json={"device_ids": [dev["id"]], "play": False})
            await self.call("PUT", "/me/player/play", params={"device_id": dev["id"]}, json=body)
            if await self.started():
                return found
        raise SpotifyError(f"The {self.device_name} speaker didn't start playing. Try again in a moment.")

    async def pause(self):
        await self.call("PUT", "/me/player/pause")

    async def resume(self):
        await self.call("PUT", "/me/player/play")

    async def next(self):
        await self.call("POST", "/me/player/next")

    async def previous(self):
        await self.call("POST", "/me/player/previous")
