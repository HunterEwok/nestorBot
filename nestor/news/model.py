"""Shared types of the news pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

SECTIONS = ("research", "trials", "policy", "community", "reggae")
SECTION_TITLES = {
    "main": "Главное за день",
    "research": "Исследования",
    "trials": "Клинические испытания",
    "policy": "Законы и индустрия",
    "community": "Сообщество",
    "reggae": "Регги и даб",
}


@dataclass
class Item:
    """One entry of a source, as parsed. Text fields are plain text, never HTML."""
    ext_id: str                     # unique within the source: guid, PMID, NCT id + update date, post id
    url: str                        # the link the digest shows, http(s) only
    title: str
    summary: str = ""               # teaser, abstract or post text for the triage model; "" if terms forbid it
    published_at: datetime | None = None   # timezone-aware
    doi: str | None = None          # lowercase, without https://doi.org/
    lang: str = "en"
    categories: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)   # pubtypes, trial status, score, is_self, paid, ...
    full_text: str = ""             # the whole post text for the filters (summary is clipped); never stored


@dataclass(frozen=True)
class Source:
    key: str                        # stable id, stored in news_items.source
    name: str                       # link text in the digest
    kind: str                       # adapter: rss | pubmed | europepmc | biorxiv | ctgov | arctic | tme | reggaeville
    url: str
    section: str | None             # one of SECTIONS; None: the triage model decides
    every_h: float                  # minimal time between runs
    mode: str = "curated"           # filter strictness: science | curated | community
    topic_required: bool = False    # broad feeds: keep only items that mention the topics
    lang: str = "en"
    max_age_days: float = 14        # newest item older than this: the source is reported as stale
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    items: list[Item]
    cursor: dict[str, Any] | None = None   # new adapter state to keep (None: unchanged)
    skipped: bool = False                  # nothing new upstream (e.g. the trial registry was not refreshed)
