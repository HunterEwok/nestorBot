"""Polite HTTP for the news sources: an honest User-Agent, one request at a time per host
with a pause between requests, a size cap, and no retries inside a run (the next run retries).

Some hosts refuse Python's TLS fingerprint (ClinicalTrials.gov); for them get() can fall
back to the system curl, which the Pi has.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

import aiohttp

log = logging.getLogger("nestor.news")

MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_S = 40
DEFAULT_PAUSE_S = 1.0
# Pauses some hosts ask for (robots.txt Crawl-delay) or that their docs recommend.
HOST_PAUSE_S = {
    "www.marijuanamoment.net": 3.0,
    "eutils.ncbi.nlm.nih.gov": 0.5,     # 3 requests per second without an API key
    "www.ebi.ac.uk": 1.0,
    "arctic-shift.photon-reddit.com": 1.5,
    "t.me": 2.0,
    "web.archive.org": 1.5,     # the Wayback Machine asks for gentle crawling
}


def user_agent(contact: str = "") -> str:
    where = f"mailto:{contact}" if contact else "https://github.com/HunterEwok/nestorBot"
    return f"Mozilla/5.0 (compatible; nestorBot/1.0; +{where})"


class HttpError(Exception):
    def __init__(self, status: int, url: str, retry_after: float | None = None):
        super().__init__(f"HTTP {status}")
        self.status, self.url, self.retry_after = status, url, retry_after


@dataclass
class Response:
    status: int
    body: bytes
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    charset: str | None = None

    def text(self) -> str:
        return self.body.decode(self.charset or "utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body)


def _retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None  # an HTTP date: treat as unknown, the source's schedule decides


class Http:
    """async with Http(contact) as http: await http.get(url, params={...})

    contact: the owner's e-mail for the User-Agent and NCBI's email= parameter; may be empty."""

    def __init__(self, contact: str = "", *, max_bytes: int = MAX_BYTES, timeout_s: float = TIMEOUT_S):
        self.contact, self.ua, self.max_bytes = contact, user_agent(contact), max_bytes
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def __aenter__(self) -> "Http":
        self._session = aiohttp.ClientSession(timeout=self.timeout, headers={"User-Agent": self.ua})
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()

    async def get(self, url: str, *, params: Mapping[str, Any] | None = None,
                  headers: Mapping[str, str] | None = None, curl_fallback: bool = False,
                  proxy: str | None = None) -> Response:
        """proxy: an http(s):// proxy for this request only (the Yandex chart from outside Russia)."""
        try:
            return await self._request("GET", url, params=params, headers=headers, proxy=proxy or None)
        except HttpError as e:
            if not (curl_fallback and e.status == 403):
                raise
        full = url + (("&" if "?" in url else "?") + urlencode(params, doseq=True) if params else "")
        return await self._curl(full, headers)

    async def post(self, url: str, *, data: Mapping[str, Any]) -> Response:
        return await self._request("POST", url, data=data)

    async def _polite(self, host: str):
        lock = self._locks.setdefault(host, asyncio.Lock())
        await lock.acquire()
        wait = self._last.get(host, 0) + HOST_PAUSE_S.get(host, DEFAULT_PAUSE_S) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        return lock

    def _done(self, host: str, lock: asyncio.Lock) -> None:
        self._last[host] = time.monotonic()
        lock.release()

    async def _request(self, method: str, url: str, **kw) -> Response:
        assert self._session is not None, "use Http as an async context manager"
        host = urlsplit(url).hostname or ""
        lock = await self._polite(host)
        try:
            async with self._session.request(method, url, max_redirects=5, **kw) as resp:
                if resp.status >= 400:
                    raise HttpError(resp.status, url, _retry_after(resp.headers.get("Retry-After")))
                if (resp.content_length or 0) > self.max_bytes:
                    raise HttpError(413, url)
                # read(n) returns whatever is buffered, so read to the end; the stream is already
                # decompressed, so the cap also stops a small gzip that unpacks into gigabytes
                body = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    body += chunk
                    if len(body) > self.max_bytes:
                        raise HttpError(413, url)
                return Response(resp.status, bytes(body), str(resp.url), dict(resp.headers), resp.charset)
        finally:
            self._done(host, lock)

    async def _curl(self, url: str, headers: Mapping[str, str] | None) -> Response:
        host = urlsplit(url).hostname or ""
        lock = await self._polite(host)
        try:
            args = ["curl", "-sS", "--compressed", "--location", "--max-redirs", "5",
                    "--max-time", str(int(self.timeout.total or TIMEOUT_S)), "--max-filesize", str(self.max_bytes),
                    "-A", self.ua, "-w", "\n%{http_code}"]
            for k, v in (headers or {}).items():
                args += ["-H", f"{k}: {v}"]
            proc = await asyncio.create_subprocess_exec(*args, "--", url, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            # --max-filesize counts the packed bytes, so the unpacked body is counted here
            # (+ 4 for the status code that -w appends)
            out = bytearray()
            while len(out) <= self.max_bytes + 4 and (chunk := await proc.stdout.read(64 * 1024)):
                out += chunk
            if len(out) > self.max_bytes + 4:
                with contextlib.suppress(ProcessLookupError):   # it may have finished meanwhile
                    proc.kill()
            await proc.communicate()
        except OSError:
            raise HttpError(403, url) from None
        finally:
            self._done(host, lock)
        if len(out) > self.max_bytes + 4 or proc.returncode == 63:   # 63: curl stopped at --max-filesize itself
            raise HttpError(413, url)
        body, _, code = bytes(out).rpartition(b"\n")
        status = int(code) if code.strip().isdigit() else 0
        if proc.returncode != 0 or status >= 400 or status == 0:
            raise HttpError(status or 599, url)
        log.info("fetched with curl", extra={"host": host})
        return Response(status, body, url)
