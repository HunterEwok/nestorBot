"""Who climbs the charts: pure code over the snapshots, no model.

For each chart its latest edition is compared with the one closest to a week before it: the
places climbed, as a share of the chart, are the artist's points there, and an entry that was
not in the chart before earns a bonus. Before there is a week of our own snapshots (the first
run) the chart's own marks stand in: Yandex's move since its previous chart, a new entry, a
release of the last month. An artist that has been in a chart for most of the last weeks is an
old hit, not a riser; one in several charts at once gets more. Artists already known (/music
known, the check model, a big audience on Last.fm) or recommended lately are left out.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .sources import CHARTS

WEEK = 7
STALE_DAYS = 2              # a chart not read for longer is left out of today's comparison
ENTERED_BONUS = 15          # a new entry
EXTRA_CHART_BONUS = 10      # for each chart beyond the first
ESTABLISHED_DAYS = 21       # in a chart this many of the days we have: an old hit
ESTABLISHED_FACTOR = 0.3
MIN_SCORE = 8
COOLDOWN = timedelta(days=90)   # an artist recommended once is not suggested again for this long
NAMES = {c.key: c.name for c in CHARTS}


@dataclass
class Candidate:
    key: str
    artist: str
    title: str                  # the artist's best placed song in the chart that gave most points
    url: str | None
    score: float = 0.0
    facts: list[str] = field(default_factory=list)
    charts: list[str] = field(default_factory=list)
    top: float = 0.0            # the most points one chart gave


def _best(rows) -> dict[str, object]:
    best: dict[str, object] = {}
    for r in rows:
        if r["artist_key"] not in best or r["position"] < best[r["artist_key"]]["position"]:
            best[r["artist_key"]] = r
    return best


def _base_day(days: list[date], last: date) -> date | None:
    """Of the earlier editions, the one closest to a week before the latest."""
    earlier = [d for d in days if d < last]
    if not earlier:
        return None
    target = last - timedelta(days=WEEK)
    return min(earlier, key=lambda d: (abs((d - target).days), -d.toordinal()))


def rising(rows, today: date, artists: dict, now: datetime, *, max_listeners: int = 0,
           limit: int = 12) -> list[Candidate]:
    """rows: snapshots of the last weeks (store.history); artists: store.artists of their keys."""
    by_chart: dict[str, dict[date, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_chart[r["source"]][r["day"]].append(r)
    found: dict[str, Candidate] = {}
    for source, editions in by_chart.items():
        days = sorted(editions)
        last = days[-1]
        if last < today - timedelta(days=STALE_DAYS):
            continue
        size = max(r["position"] for r in editions[last])
        base = _base_day(days, last)
        before = _best(editions[base]) if base else {}
        presence: dict[str, int] = defaultdict(int)
        for d in days:
            for key in {r["artist_key"] for r in editions[d]}:
                presence[key] += 1
        name = NAMES.get(source, source)
        for key, r in _best(editions[last]).items():
            pos = r["position"]
            if base is not None:
                prev = before.get(key)
                prev_pos = prev["position"] if prev else None
                climb = (prev_pos or size + 1) - pos
                entered = prev_pos is None
                fact = f"{name}: {prev_pos or 'не было'} → {pos} место за {(last - base).days} дн."
            else:
                climb = r["shift"] or 0
                entered = r["is_new"]
                if entered and climb <= 0:
                    climb = (size + 1 - pos) // 2
                fact = f"{name}: {pos} место" + (f", поднялся на {r['shift']}" if (r["shift"] or 0) > 0 else "") \
                    + (", новинка" if r["is_new"] else "")
            points = max(climb, 0) / size * 100 + (ENTERED_BONUS if entered else 0)
            if points <= 0:
                continue
            if len(days) >= ESTABLISHED_DAYS and presence[key] >= ESTABLISHED_DAYS:
                points *= ESTABLISHED_FACTOR
                fact += f", в чарте {presence[key]} дн. из {len(days)}"
            c = found.get(key)
            if c is None:
                c = found[key] = Candidate(key, r["artist"], r["title"], r["url"])
            elif points > c.top:        # the chart that gives most points names the song
                c.artist, c.title, c.url = r["artist"], r["title"], r["url"]
            c.top = max(c.top, points)
            c.score += points + (EXTRA_CHART_BONUS if c.charts else 0)
            c.facts.append(fact)
            c.charts.append(source)
    out = []
    for c in found.values():
        a = artists.get(c.key)
        if a is not None and (a["known"] or (a["recommended_at"] is not None and a["recommended_at"] > now - COOLDOWN)
                              or (max_listeners and a["listeners"] is not None and a["listeners"] > max_listeners)
                              or a["russian"] is False):
            continue
        if c.score >= MIN_SCORE:
            out.append(c)
    out.sort(key=lambda c: (-c.score, c.key))
    return out[:limit]
