"""ClinicalTrials.gov API v2: trials of our substances updated in the last two days, each
labelled new / results / status / update.

Quirks:
- The CDN may refuse Python's TLS fingerprint with a 403, so every GET may fall back to curl.
- The data is refreshed about once a day: /api/v2/version's dataTimestamp is compared with
  the cursor and the run is skipped when it has not moved.
- query.intr searches the whole intervention area (titles, other names, MeSH, arm groups),
  so a study is kept only when one of its interventions names a term, in its name or its
  other names: industry trials name the drug by its code ("COMP360", "MM120") and give the
  substance as an other name. Only the names go into meta. Acronyms must stand alone: "LSD"
  is not the "LSD1 inhibitor" of oncology trials, "DMT" not "DMTS", and not an acronym
  spelled out just before it: "Disease-Modifying Therapy (DMT)", "Dance/Movement Therapy (DMT)".
- Paging: nextPageToken, with exactly the same parameters as the first page.
- Text fields are CommonMark: list markers and backslash escapes ("\\>18 years") are removed.
- Dose details live in armsInterventionsModule descriptions: never requested, never read.
- The status labels need the previous overallStatus per study: the cursor keeps the last
  MAX_STATUSES of them next to the dataTimestamp. jsonb sorts keys, so after a round trip
  through news_sources "oldest" means the lowest NCT numbers, the oldest registrations.
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple

from ..http import Http
from ..model import FetchResult, Item, Source

API = "https://clinicaltrials.gov/api/v2/"
FIELDS = ("NCTId,BriefTitle,BriefSummary,OverallStatus,LastUpdatePostDate,StudyFirstPostDate,ResultsFirstPostDate,"
          "Phase,HasResults,Condition,InterventionName,InterventionOtherName,LeadSponsorName")
PAGE_SIZE = 100
MAX_PAGES = 5
WINDOW_DAYS = 2
SUMMARY_MAX = 800
MAX_STATUSES = 3000

_NCT = re.compile(r"NCT[0-9]{8}")
_WORD = re.compile(r"[^\W\d_]+")
_WS = re.compile(r"\s+")
_MD_LIST = re.compile(r"(?m)^[ \t]*(?:[*+-]|\d{1,3}[.)])[ \t]+")
_MD_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!<>~|=&^\"'])")


class Study(NamedTuple):
    nct: str
    title: str
    summary: str
    status: str
    updated: date
    first_posted: date | None
    results_posted: date | None
    phases: list[str]
    has_results: bool
    conditions: list[str]
    interventions: list[str]
    other_names: list[str]
    sponsor: str


async def fetch(http: Http, source: Source, cursor: dict, now: datetime) -> FetchResult:
    stamp = parse_version((await http.get(API + "version", curl_fallback=True)).body)
    if stamp == cursor.get("data_timestamp"):
        return FetchResult([], skipped=True)

    raw = source.params.get("interventions") or ()
    terms = [str(t) for t in ((raw,) if isinstance(raw, str) else raw) if str(t).strip()]
    if not terms:
        raise ValueError("ctgov: no interventions")
    today = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()
    since = today - timedelta(days=WINDOW_DAYS)
    params = {"format": "json", "query.intr": query_intr(terms),
              "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{since},MAX]", "fields": FIELDS,
              "sort": "LastUpdatePostDate:desc", "pageSize": str(PAGE_SIZE)}
    studies: list[Study] = []
    token: str | None = None
    for _ in range(MAX_PAGES):
        resp = await http.get(API + "studies", params={**params, "pageToken": token} if token else params,
                              curl_fallback=True)
        page, next_token = parse_studies(resp.body)
        studies += page
        if not next_token or next_token == token:
            break
        token = next_token

    named = intervention_matcher(terms)
    previous = cursor.get("statuses")
    previous = {k: v for k, v in previous.items() if isinstance(v, str)} if isinstance(previous, dict) else {}
    statuses = dict(previous)
    items: list[Item] = []
    seen: set[str] = set()
    for s in studies:
        if s.nct in seen or not any(named(name) for name in (*s.interventions, *s.other_names)):
            continue
        seen.add(s.nct)
        items.append(_item(s, since, previous.get(s.nct)))
        if s.status:
            statuses.pop(s.nct, None)  # re-inserted: the oldest-inserted entries go first
            statuses[s.nct] = s.status
    if len(statuses) > MAX_STATUSES:
        statuses = dict(list(statuses.items())[-MAX_STATUSES:])
    return FetchResult(items, cursor={"data_timestamp": stamp, "statuses": statuses})


def query_intr(terms: Iterable[str]) -> str:
    """psilocybin OR "lysergic acid diethylamide" OR "5-MeO-DMT": anything but one plain word is quoted."""
    out = []
    for term in terms:
        term = _WS.sub(" ", str(term).replace('"', " ")).strip()
        if term:
            out.append(term if term.isalnum() else f'"{term}"')
    return " OR ".join(out)


def intervention_matcher(terms: Iterable[str]) -> Callable[[str], bool]:
    """name -> whether it names one of the terms (case-insensitive; acronyms of up to 5 characters stand alone)."""
    parts = []
    for term in terms:
        term = _WS.sub(" ", str(term)).strip()
        if term:
            esc = re.escape(term)
            parts.append(rf"(?<![^\W_]){esc}(?![^\W_])" if len(term) <= 5 else esc)
    if not parts:
        raise ValueError("ctgov: no interventions")
    pattern = re.compile("|".join(parts), re.I)
    return lambda name: any(not _spelled_out(name, m) for m in pattern.finditer(name))


def _spelled_out(name: str, m: re.Match[str]) -> bool:
    """'Disease-Modifying Therapy (DMT)': the acronym in parentheses stands for the words just before it."""
    acronym = m.group()
    if len(acronym) > 5 or name[m.start() - 1:m.start()] != "(" or name[m.end():m.end() + 1] != ")":
        return False
    words = _WORD.findall(name[:m.start() - 1])[-len(acronym):]
    return len(words) == len(acronym) and "".join(w[0] for w in words).upper() == acronym.upper()


def parse_version(body: bytes) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        raise ValueError("ctgov version: not JSON") from None
    stamp = data.get("dataTimestamp") if isinstance(data, dict) else None
    if not isinstance(stamp, str) or not stamp.strip():
        raise ValueError("ctgov version: no dataTimestamp")
    return stamp.strip()[:40]


def parse_studies(body: bytes) -> tuple[list[Study], str | None]:
    """Studies of one page and the next page token (None on the last page)."""
    try:
        data = json.loads(body)
    except ValueError:
        raise ValueError("ctgov: not JSON") from None
    raw = data.get("studies") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("ctgov: no studies")
    token = data.get("nextPageToken")
    return [s for s in (_study(r) for r in raw) if s is not None], token if isinstance(token, str) and token else None


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def _strings(value: Any, key: str | None = None) -> list[str]:
    out = []
    for v in value if isinstance(value, list) else ():
        text = _plain(v.get(key) if key and isinstance(v, dict) else v)
        if text and text not in out:
            out.append(text)
    return out


def _study(r: Any) -> Study | None:
    proto = _get(r, "protocolSection")
    nct = _get(proto, "identificationModule", "nctId")
    title = _plain(_get(proto, "identificationModule", "briefTitle"))
    status_module = _get(proto, "statusModule")
    updated = _day(_get(status_module, "lastUpdatePostDateStruct", "date"))
    if not isinstance(nct, str) or not _NCT.fullmatch(nct.strip()) or not title or updated is None:
        return None
    status = _get(status_module, "overallStatus")
    summary = _clip(_plain(_get(proto, "descriptionModule", "briefSummary")), SUMMARY_MAX)
    interventions = _get(proto, "armsInterventionsModule", "interventions")
    interventions = interventions if isinstance(interventions, list) else []
    return Study(
        nct=nct.strip(), title=title, summary=summary, updated=updated,
        status=status.strip()[:40] if isinstance(status, str) else "",
        first_posted=_day(_get(status_module, "studyFirstPostDateStruct", "date")),
        results_posted=_day(_get(status_module, "resultsFirstPostDateStruct", "date")),
        phases=_strings(_get(proto, "designModule", "phases")), has_results=r.get("hasResults") is True,
        conditions=_strings(_get(proto, "conditionsModule", "conditions")),
        # names only: the interventions' descriptions carry doses
        interventions=_strings(interventions, "name"),
        other_names=[n for i in interventions if isinstance(i, dict) for n in _strings(i.get("otherNames"))],
        sponsor=_plain(_get(proto, "sponsorCollaboratorsModule", "leadSponsor", "name")))


def _item(s: Study, since: date, old_status: str | None) -> Item:
    if s.first_posted is not None and s.first_posted >= since:
        change = "new"
    elif s.results_posted is not None and s.results_posted >= since:
        change = "results"
    elif old_status is not None and s.status and old_status != s.status:
        change = "status"
    else:
        change = "update"
    return Item(
        ext_id=f"{s.nct}:{s.updated.isoformat()}", url=f"https://clinicaltrials.gov/study/{s.nct}", title=s.title,
        summary=s.summary, published_at=datetime(s.updated.year, s.updated.month, s.updated.day, tzinfo=timezone.utc),
        lang="en", meta={"nct": s.nct, "status": s.status, "phases": s.phases, "has_results": s.has_results,
                         "conditions": s.conditions, "interventions": s.interventions, "sponsor": s.sponsor,
                         "change": change})


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(value.strip()) if isinstance(value, str) else None
    except ValueError:
        return None


def _plain(value: Any) -> str:
    """CommonMark to plain text: list markers, **strong** and backslash escapes go, entities are unescaped."""
    if not isinstance(value, str):
        return ""
    text = _MD_ESCAPE.sub(r"\1", _MD_STRONG.sub(r"\2", _MD_LIST.sub("", value)))
    return _WS.sub(" ", html.unescape(text)).strip()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:.") + "…"
