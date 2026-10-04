"""Europe PMC search, preprints only (SRC:PPR): one query covers every preprint server it
indexes (medRxiv, bioRxiv, PsyArXiv, Research Square, SSRN...), with keyword search and
abstracts.

Quirks:
- Without a field prefix the query also matches open-access full text, so the terms are
  wrapped in TITLE_ABS:(...).
- FIRST_IDATE is the day Europe PMC indexed the record; it lags the preprint by 1-4 days,
  hence the window of the last three days (the store dedupes).
- Paging: cursorMark=* and then nextCursorMark, until the mark repeats or a page is empty.
- abstractText and sometimes title are HTML (<h4>, <p>, <i>, <sup>).
- DOIs may carry a version: PsyArXiv "_v1", Research Square "/v1", Preprints.org ".v1". The
  item's doi drops it so that versions and other sources meet; the link keeps the DOI as
  given, because an unversioned DOI does not always resolve.
- No published rate limit (staff: "applied per IP"); Http keeps it to one request a second.
  EMBL-EBI expects attribution: the digest links Europe PMC as the source.
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

PAGE_SIZE = 100
MAX_PAGES = 5
WINDOW_DAYS = 3
SUMMARY_MAX = 1500

_ID = re.compile(r"[A-Z]{2,4}[0-9]{1,12}")
_SRC = re.compile(r"[A-Z]{3}")
_DOI = re.compile(r"10\.\d{4,9}/\S+")
_DOI_VERSION = re.compile(r"(?:[_./]v|(?<=\d)v)\d{1,3}$")
_WS = re.compile(r"\s+")
_BLOCK = re.compile(r"<(?=/?(?:p|br|div|h[1-6]|li|ul|ol|tr|td|th|table|blockquote|section)\b)", re.I)
# A tag or an entity; else a "<" or "&" that is text ("A<B, C>D", "<LOD in 3%", a final "R&D"),
# which html.parser would swallow up to the next ">" (or to the end) unless escaped first.
_MARKUP = re.compile(r"(</?[A-Za-z][\w.:-]*(?:\s+[\w.:-]+\s*=\s*(?:\"[^\"<>]*\"|'[^'<>]*'|[^\s\"'<>]+))*\s*/?>"
                     r"|&(?:[A-Za-z][A-Za-z0-9]{1,31}|#\d{1,7}|#[xX][0-9A-Fa-f]{1,6});)|[<&]")


class Page(NamedTuple):
    items: list[Item]
    results: int                # entries on the page, including the ones skipped
    next_mark: str | None
    hit_count: int | None


async def fetch(http: Http, source: Source, cursor: dict, now: datetime) -> FetchResult:
    today = _today(now)
    query = (f"TITLE_ABS:({source.params['terms']}) AND SRC:PPR "
             f"AND FIRST_IDATE:[{today - timedelta(days=WINDOW_DAYS)} TO {today}]")
    items: list[Item] = []
    seen: set[str] = set()
    mark, total = "*", 0
    for _ in range(MAX_PAGES):
        resp = await http.get(source.url, params={"query": query, "format": "json", "resultType": "core",
                                                  "pageSize": str(PAGE_SIZE), "cursorMark": mark})
        page = parse_search(resp.body)
        for item in page.items:
            if item.ext_id not in seen:
                seen.add(item.ext_id)
                items.append(item)
        total += page.results
        if (not page.results or not page.next_mark or page.next_mark == mark
                or (page.hit_count is not None and total >= page.hit_count)):
            break
        mark = page.next_mark
    return FetchResult(items)


def parse_search(body: bytes) -> Page:
    try:
        data = json.loads(body)
    except ValueError:
        raise ValueError("europepmc: not JSON") from None
    if not isinstance(data, dict):
        raise ValueError("europepmc: unexpected JSON")
    if data.get("errMsg") or data.get("error"):
        raise ValueError(f"europepmc: {_WS.sub(' ', str(data.get('errMsg') or data.get('error')))[:120]}")
    hits = data.get("hitCount")
    if isinstance(hits, str) and hits.isascii() and hits.isdigit():
        hits = int(hits)
    hit_count = hits if isinstance(hits, int) else None
    result_list = data.get("resultList")
    results = result_list.get("result") if isinstance(result_list, dict) else None
    if results is None and hit_count == 0:
        results = []
    if not isinstance(results, list):
        raise ValueError("europepmc: no resultList")
    items = [item for item in (_record(r) for r in results) if item is not None]
    mark = data.get("nextCursorMark")
    return Page(items, len(results), mark if isinstance(mark, str) and mark else None, hit_count)


def _record(r: Any) -> Item | None:
    if not isinstance(r, dict):
        return None
    pid = str(r.get("id") or "").strip()
    title = _plain(r.get("title"))
    if not _ID.fullmatch(pid) or not title:
        return None
    src = str(r.get("source") or "PPR")
    raw_doi = _doi(r.get("doi"))
    if raw_doi:
        url = "https://doi.org/" + quote(raw_doi, safe="/:;()._-")
    else:
        url = f"https://europepmc.org/article/{src if _SRC.fullmatch(src) else 'PPR'}/{pid}"

    meta: dict[str, Any] = {"preprint": True}
    details = r.get("bookOrReportDetails")
    if isinstance(details, dict) and (server := _plain(details.get("publisher"))):
        meta["server"] = server
    comments = r.get("commentCorrectionList")
    for c in (comments.get("commentCorrection") if isinstance(comments, dict) else None) or ():
        # The preprint already has a journal version in PubMed: its PMID, to tell the two apart later.
        if isinstance(c, dict) and c.get("type") == "Preprint of" and c.get("source") == "MED":
            if (pmid := str(c.get("id") or "")).isdigit():
                meta["published_pmid"] = pmid
                break
    return Item(ext_id=pid, url=url, title=title, summary=_clip(_plain(r.get("abstractText")), SUMMARY_MAX),
                published_at=_day(r.get("firstIndexDate")) or _day(r.get("firstPublicationDate")),
                doi=_unversioned(raw_doi) if raw_doi else None, lang="en", meta=meta)


def _today(now: datetime) -> date:
    return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()


def _day(value: Any) -> datetime | None:
    try:
        d = date.fromisoformat(value.strip()[:10]) if isinstance(value, str) else None
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d else None


def _doi(value: Any) -> str | None:
    doi = value.strip().lower().removeprefix("https://doi.org/").removeprefix("doi:") if isinstance(value, str) else ""
    return doi if _DOI.fullmatch(doi) else None


def _unversioned(doi: str) -> str:
    bare = _DOI_VERSION.sub("", doi)
    return bare if _DOI.fullmatch(bare) else doi


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
