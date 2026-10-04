"""Which stories the digest model gets to choose from: the strongest per section, so that a
busy day on PubMed does not crowd out policy or reggae. Pure functions."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .model import SECTIONS, Source

PER_SECTION = {"research": 6, "trials": 3, "policy": 6, "community": 3, "reggae": 4}
MAX_CANDIDATES = 22

_PUBTYPE_BONUS = {
    "randomized controlled trial": 8, "meta-analysis": 6, "systematic review": 6, "clinical trial": 4,
    "clinical trial, phase iii": 6, "clinical trial, phase ii": 4, "review": 2,
}
_TRIAL_BONUS = {"results": 8, "new": 5, "status": 3}


@dataclass
class Candidate:
    n: int                          # number the digest model refers to
    story_id: int
    section: str
    score: float
    items: list[Mapping[str, Any]] = field(default_factory=list)   # rows of store.candidates, best first


def item_score(row: Mapping[str, Any], now: datetime) -> float:
    meta = row["meta"] or {}
    score = 10.0 * (row["importance"] or 2)
    score += max(_PUBTYPE_BONUS.get(str(p).lower(), 0) for p in (meta.get("pubtypes") or [""]))
    score += _TRIAL_BONUS.get(meta.get("change"), 0)
    if isinstance(meta.get("score"), int):          # Reddit upvotes
        score += min(meta["score"] / 20, 6)
    if meta.get("paid"):
        score -= 3
    hours = max((now - row["fetched_at"]).total_seconds() / 3600, 0)
    return score - hours / 12


def pick(rows: list[Mapping[str, Any]], sources: Mapping[str, Source], now: datetime, *,
         per_section: Mapping[str, int] = PER_SECTION, limit: int = MAX_CANDIDATES) -> list[Candidate]:
    stories: dict[int, list[Mapping[str, Any]]] = {}
    for row in rows:
        stories.setdefault(row["story_id"], []).append(row)
    ranked: list[Candidate] = []
    for story_id, items in stories.items():
        items.sort(key=lambda r: item_score(r, now), reverse=True)
        names = {sources[r["source"]].name if r["source"] in sources else r["source"] for r in items}
        sections = Counter(r["section"] for r in items if r["section"] in SECTIONS)
        section = items[0]["section"] if items[0]["section"] in SECTIONS else None
        if sections and (section is None or sections[section] < max(sections.values())):
            section = sections.most_common(1)[0][0]
        if section is None:
            continue
        # The same story told by several independent outlets weighs more.
        score = item_score(items[0], now) + 8 * (len(names) - 1)
        ranked.append(Candidate(0, story_id, section, score, items))
    ranked.sort(key=lambda c: c.score, reverse=True)
    taken: Counter[str] = Counter()
    out: list[Candidate] = []
    for c in ranked:
        if taken[c.section] < per_section.get(c.section, 3) and len(out) < limit:
            taken[c.section] += 1
            out.append(c)
    for n, c in enumerate(out, 1):
        c.n = n
    return out
