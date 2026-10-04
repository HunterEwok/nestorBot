"""bioRxiv and medRxiv through api.biorxiv.org: every preprint posted in the last days, one to
four days before Europe PMC indexes it. The API has no search, so all of it is fetched and
the topic filter (source.topic_required) keeps what is ours.

Quirks:
- Only explicit dates work with a cursor: /details/<server>/<from>/<to>/<cursor>/json. The
  "2d" and "N" interval forms answer "Both dates must be in yyyy-mm-dd format" as soon as a
  cursor is added.
- Pages hold 30 posts; loop on messages[0].total (a string on medRxiv, a number on bioRxiv).
  An empty interval is status "no posts found"; any other status but "ok" is an error.
- The interval also returns revisions: the same DOI shows up once per version, and only the
  highest is kept. DOIs carry no version (it is a separate field); the prefix moved to
  10.64898 in 2026, so it is never hard-coded.
- Dates are days without a timezone; the window overlaps the previous runs and the store dedupes.
- www.biorxiv.org rate-limits scrapers: the article pages are only linked, never fetched.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..http import Http
from ..model import FetchResult, Item, Source

API = "https://api.biorxiv.org/details"
SERVERS = {"biorxiv": "bioRxiv", "medrxiv": "medRxiv"}
PAGE_SIZE = 30
MAX_PAGES = 40
WINDOW_DAYS = 2
SUMMARY_MAX = 1500

_DOI = re.compile(r"10\.\d{4,9}/\S+")
_WS = re.compile(r"\s+")
_BLOCK = re.compile(r"<(?=/?(?:p|br|div|h[1-6]|li|ul|ol|tr|td|th|table|blockquote|section)\b)", re.I)
# A tag or an entity; else a "<" or "&" that is text ("A<B, C>D", "<LOD in 3%", a final "R&D"),
# which html.parser would swallow up to the next ">" (or to the end) unless escaped first.
_MARKUP = re.compile(r"(</?[A-Za-z][\w.:-]*(?:\s+[\w.:-]+\s*=\s*(?:\"[^\"<>]*\"|'[^'<>]*'|[^\s\"'<>]+))*\s*/?>"
                     r"|&(?:[A-Za-z][A-Za-z0-9]{1,31}|#\d{1,7}|#[xX][0-9A-Fa-f]{1,6});)|[<&]")


class Page(NamedTuple):
    items: list[Item]
    results: int                # posts on the page, including the ones skipped
    total: int


async def fetch(http: Http, source: Source, cursor: dict, now: datetime) -> FetchResult:
    server = source.params.get("server")
    if server not in SERVERS:
        raise ValueError(f"biorxiv: unknown server {str(server)[:20]!r}")
    today = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()
    start = today - timedelta(days=WINDOW_DAYS)
    best: dict[str, Item] = {}
    first: set[str] = set()     # DOIs whose version 1 is in this window: new preprints, whatever the latest version
    offset = 0
    for _ in range(MAX_PAGES):
        resp = await http.get(f"{API}/{server}/{start}/{today}/{offset}/json")
        page = parse_details(resp.body, server)
        for item in page.items:
            if item.meta["version"] == "1":
                first.add(item.ext_id)
            kept = best.get(item.ext_id)
            if kept is None or int(item.meta["version"]) > int(kept.meta["version"]):
                best[item.ext_id] = item
        offset += max(PAGE_SIZE, page.results)
        if not page.results or offset >= page.total:
            break
    for doi in first:
        if best[doi].meta["version"] != "1":
            best[doi].meta["first_in_window"] = True
    return FetchResult(list(best.values()))


def parse_details(body: bytes, server: str) -> Page:
    try:
        data = json.loads(body)
    except ValueError:
        raise ValueError("biorxiv: not JSON") from None
    messages = data.get("messages") if isinstance(data, dict) else None
    message = messages[0] if isinstance(messages, list) and messages and isinstance(messages[0], dict) else None
    if message is None:
        raise ValueError("biorxiv: no messages")
    status = str(message.get("status") or "")
    if status == "no posts found":
        return Page([], 0, 0)
    if status != "ok":
        raise ValueError(f"biorxiv: {_WS.sub(' ', status)[:120] or 'no status'}")
    try:
        total = int(str(message.get("total")).strip())
    except ValueError:
        raise ValueError("biorxiv: no total") from None
    collection = data.get("collection")
    if not isinstance(collection, list):
        raise ValueError("biorxiv: no collection")
    items = [item for item in (_post(p, server) for p in collection) if item is not None]
    return Page(items, len(collection), total)


def _post(p: Any, server: str) -> Item | None:
    if not isinstance(p, dict):
        return None
    doi = p.get("doi").strip().lower() if isinstance(p.get("doi"), str) else ""
    title = _plain(p.get("title"))
    if not _DOI.fullmatch(doi) or not title:
        return None
    version = str(p.get("version") or "1").strip()
    version = version if version.isascii() and version.isdigit() and len(version) < 4 else "1"  # not "²"
    meta: dict[str, Any] = {"server": SERVERS[server], "version": version, "category": _plain(p.get("category")),
                            "preprint": True}
    published = p.get("published").strip().lower() if isinstance(p.get("published"), str) else ""
    if _DOI.fullmatch(published):  # the journal version, "NA" until there is one
        meta["published_doi"] = published
    return Item(ext_id=doi, url=f"https://www.{server}.org/content/{quote(doi, safe='/:;()._-')}v{version}",
                title=title, summary=_clip(_plain(p.get("abstract")), SUMMARY_MAX), published_at=_day(p.get("date")),
                doi=doi, lang="en", meta=meta)


def _day(value: Any) -> datetime | None:
    try:
        d = date.fromisoformat(value.strip()[:10]) if isinstance(value, str) else None
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d else None


def _plain(value: Any) -> str:
    """Tags stripped, entities unescaped, whitespace collapsed."""
    if not isinstance(value, str):
        return ""
    if "<" in value or "&" in value:
        value = _MARKUP.sub(lambda m: m.group(1) or ("&lt;" if m.group(0) == "<" else "&amp;"), value)
        soup = BeautifulSoup(_BLOCK.sub(" <", value), "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        value = soup.get_text()
    return _WS.sub(" ", value).strip()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:.") + "…"
