"""Which song went viral: pure code over the daily snapshots of the charts, no model.

«Завирусилась» here means: a song's popularity jumped at least twofold within three days, it is in the
top 50 of some chart today, and the jump is not a one-day blip (another chart confirms it, or it rose two
days running). Among such songs the one with the sharpest, newest rise is the song of the day.

Popularity from a place in a chart: streams fall off as a power of the rank (≈ rank^-0.54 on Spotify's
top 200, arXiv 1910.01445), so the log of the place is the natural scale: 40 → 20 is the same gain as
4 → 2. A song outside a chart that was read counts as one place below its end, a lower bound; a chart
that was not read that day is a gap, filled from its edition of a day or two before.

Each chart scores its songs by their growth over 1, 3 and about 7 days, as a robust z-score among the
songs of that chart today: a chart where everything moves a lot does not outshout a calm one. Shazam
weighs more, as it leads streaming by days; a rise seen in several charts at once gets more. A song long
in the top 20 has peaked and gets less; a new one gets more; stars are not left out.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .sources import CHARTS, song_key

A = 0.6                     # the rank exponent: popularity ∝ rank^-A
GAP_DAYS = 2                # a chart edition stands in for this many unread days after it
TOP = 50                    # the song is this high in some chart today
MIN_GAIN = math.log(2)      # popularity at least doubled within three days
Z_CONFIRM = 2.0             # a chart where the song's growth stands out this much counts as confirming it
CROSS_BONUS = 0.3           # for each confirming chart beyond the first
FRESH_DAYS = 21             # novelty decays over this many days since the song first charted
DWELL_TOP = 20
DWELL_DAYS = 14             # each this many days in some top 20 within the last 60 cuts the score by e
PEAK_FROM, PEAK_TO = 30, 8  # an earlier peak in this window (days ago) not beaten today: a comeback, not a burst
COMEBACK = 0.6
THRESHOLD = 3.0             # below it: no viral song today
SONG_COOLDOWN = 30          # days before the same song can be the song of the day again
ARTIST_COOLDOWN = 3
HISTORY_DAYS = 60           # what the score looks back at
MOVE_MARKS = {"yandex_ru"}  # charts whose own marks say the move since their previous edition (Apple's «new» is
                            # a fresh release, not a new entry)
WEIGHTS = {"shazam_ru": 1.2, "shazam_msk": 1.2, "yandex_ru": 1.0, "apple_ru": 0.8, "lastfm_ru": 0.5}
NAMES = {c.key: c.name for c in CHARTS}


@dataclass
class Candidate:
    key: str                    # song_key
    artist_key: str
    artist: str
    title: str
    url: str | None
    score: float = 0.0
    best: int = 0               # the best place today
    facts: list[str] = field(default_factory=list)
    charts: list[str] = field(default_factory=list)     # the charts that confirm the rise


@dataclass(frozen=True)
class Pick:
    """An earlier song of the day: the cooldowns."""
    day: date
    key: str
    artist_key: str


class _Chart:
    def __init__(self, source: str):
        self.source = source
        self.size: dict[date, int] = {}                     # edition -> its last place
        self.pos: dict[date, dict[str, int]] = defaultdict(dict)
        self.rows: dict[date, dict[str, object]] = defaultdict(dict)

    def add(self, r, key: str) -> None:
        d = r["day"]
        self.size[d] = max(self.size.get(d, 0), r["position"])
        if key not in self.pos[d] or r["position"] < self.pos[d][key]:
            self.pos[d][key] = r["position"]
            self.rows[d][key] = r

    def edition(self, d: date) -> date | None:
        """The edition that stands for day d: its own, or one of up to GAP_DAYS before."""
        for back in range(GAP_DAYS + 1):
            e = d - timedelta(days=back)
            if e in self.size:
                return e
        return None

    def place(self, key: str, d: date) -> int | None:
        """The song's place on day d; one below the end when the chart was read without it; None: not read."""
        e = self.edition(d)
        if e is None:
            return None
        return self.pos[e].get(key, self.size[e] + 1)

    def level(self, key: str, d: date) -> float | None:
        p = self.place(key, d)
        return None if p is None else -A * math.log(p)


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


@dataclass(frozen=True)
class _Move:
    """A song's growth in one chart: log-popularity gained over 1, 3 and about 7 days, and the day before."""
    g1: float | None
    g3: float | None
    g7: float | None
    before: float | None        # the 1-day growth of the day before: a rise two days running
    z: float = 0.0

    @property
    def gain(self) -> float:
        return max((g for g in (self.g1, self.g3) if g is not None), default=0.0)


def _growth(chart: _Chart, key: str, e: date) -> _Move:
    """The growth of log-popularity up to edition e."""
    now = chart.level(key, e)
    if now is None:
        return _Move(None, None, None, None)
    prev = chart.level(key, e - timedelta(days=1))
    if prev is None:
        r = chart.rows[e].get(key) if chart.source in MOVE_MARKS else None
        # No edition of yesterday: the chart's own mark of the move, if it has one.
        if r is not None and r.get("is_new"):
            prev = -A * math.log(chart.size[e] + 1)
        elif r is not None and r.get("shift") is not None:
            prev = -A * math.log(max(1, chart.pos[e][key] + r["shift"]))
    g1 = now - prev if prev is not None else None
    base3 = _mean([chart.level(key, e - timedelta(days=n)) for n in (3, 4)
                   if chart.edition(e - timedelta(days=n)) is not None])
    if base3 is None and chart.edition(e - timedelta(days=2)) is not None:
        base3 = chart.level(key, e - timedelta(days=2))
    g3 = now - base3 if base3 is not None else None
    base7 = _median([chart.level(key, e - timedelta(days=n)) for n in (5, 6, 7)
                     if chart.edition(e - timedelta(days=n)) is not None])
    g7 = now - base7 if base7 is not None else None
    before = None
    if chart.edition(e - timedelta(days=1)) is not None and chart.edition(e - timedelta(days=2)) is not None \
            and chart.edition(e - timedelta(days=1)) != chart.edition(e - timedelta(days=2)):
        before = chart.level(key, e - timedelta(days=1)) - chart.level(key, e - timedelta(days=2))
    return _Move(g1, g3, g7, before)


def _raw(g1, g3, g7) -> float | None:
    parts = [(0.5, g1), (0.3, g3), (0.2, g7)]
    weight = sum(w for w, g in parts if g is not None)
    return sum(w * g for w, g in parts if g is not None) / weight if weight else None


def _z(values: dict[str, float]) -> dict[str, float]:
    """Robust z-scores: (x - median) / (1.4826 · MAD), the MAD kept off zero."""
    if not values:
        return {}
    med = statistics.median(values.values())
    mad = statistics.median(abs(v - med) for v in values.values())
    scale = max(1.4826 * mad, 0.05)
    return {k: (v - med) / scale for k, v in values.items()}


def _fact(chart: _Chart, key: str, e: date) -> str:
    now = chart.pos[e][key]
    for back in (3, 2, 1):
        d = e - timedelta(days=back)
        if chart.edition(d) is not None:
            was = chart.pos[chart.edition(d)].get(key)
            return f"{NAMES.get(chart.source, chart.source)}: {was or 'не было'} → {now} место за {back} дн."
    r = chart.rows[e][key]
    move = (", новинка" if r.get("is_new") else
            f", поднялся на {r['shift']}" if (r.get("shift") or 0) > 0 else "")
    return f"{NAMES.get(chart.source, chart.source)}: {now} место{move}"


def viral(rows, today: date, *, picks: list[Pick] = (), banned: frozenset[str] = frozenset(),
          threshold: float = THRESHOLD, limit: int = 5) -> list[Candidate]:
    """The songs that went viral by `today`, best first; [] when none reaches the threshold.

    rows: snapshots up to today (store.history: day, source, position, artist, artist_key, title, shift,
    is_new, url); picks: the earlier songs of the day; banned: artist keys never to suggest (/music known)."""
    charts: dict[str, _Chart] = {}
    first: dict[str, date] = {}
    names: dict[str, tuple] = {}
    for r in rows:
        if r["day"] > today:
            continue
        key = song_key(r["artist"], r["title"])
        charts.setdefault(r["source"], _Chart(r["source"])).add(r, key)
        if key not in first or r["day"] < first[key]:
            first[key] = r["day"]
    scores: dict[str, dict[str, _Move]] = defaultdict(dict)     # song -> chart -> its move there
    for source, chart in charts.items():
        e = chart.edition(today)
        if e is None or e < today - timedelta(days=1):
            continue                    # not read lately: left out of today
        raws, growth = {}, {}
        for key in chart.pos[e]:
            move = _growth(chart, key, e)
            raw = _raw(move.g1, move.g3, move.g7)
            if raw is not None:
                raws[key], growth[key] = raw, move
        for key, z in _z(raws).items():
            m = growth[key]
            scores[key][source] = _Move(m.g1, m.g3, m.g7, m.before, z)
            r = chart.rows[e][key]
            best = names.get(key)
            # The chart that ranks it highest names it; a link from Yandex or Apple when one has it.
            if best is None or chart.pos[e][key] < best[0]:
                names[key] = (chart.pos[e][key], r["artist"], r["title"], r["artist_key"],
                              r.get("url") or (best[4] if best else None))
            elif best[4] is None and r.get("url"):
                names[key] = best[:4] + (r["url"],)
    recent_songs = {p.key for p in picks if today - timedelta(days=SONG_COOLDOWN) <= p.day < today}
    recent_artists = {p.artist_key for p in picks if today - timedelta(days=ARTIST_COOLDOWN) <= p.day < today}
    out = []
    for key, by_chart in scores.items():
        best, artist, title, akey, url = names[key]
        if best > TOP or key in recent_songs or akey in recent_artists or akey in banned:
            continue
        gain_chart = max(by_chart, key=lambda c: by_chart[c].gain)
        top = by_chart[gain_chart]
        if top.gain < MIN_GAIN:
            continue
        # A chart confirms only a real rise there: a high z alone may be a smaller fall than the others'.
        confirming = [c for c, m in by_chart.items() if m.z >= Z_CONFIRM and m.gain > 0]
        two_days = (top.g1 or 0) > 0 and (top.before or 0) > 0
        # Not a one-day blip: another chart sees it too, or it rose two days running. Before there is a day
        # before (the first run), a chart's own mark of the move stands alone only when it stands out a lot.
        if not (len(confirming) >= 2 or two_days
                or (top.before is None and top.z >= 1.5 * Z_CONFIRM)):
            continue
        weight = sum(WEIGHTS.get(c, 1.0) for c in by_chart)
        s = sum(WEIGHTS.get(c, 1.0) * max(m.z, 0.0) for c, m in by_chart.items()) / weight
        s *= 1 + CROSS_BONUS * max(len(confirming) - 1, 0)
        age = (today - first[key]).days
        s *= 0.5 + 0.5 * math.exp(-age / FRESH_DAYS)
        dwell = len({d for c in charts.values() for d, pos in c.pos.items()
                     if today - timedelta(days=HISTORY_DAYS) <= d < today and pos.get(key, TOP + 1) <= DWELL_TOP})
        s *= math.exp(-dwell / DWELL_DAYS)
        level_now = max(v for c in by_chart if (v := charts[c].level(key, today)) is not None)
        earlier = [charts[c].level(key, today - timedelta(days=n)) for c in by_chart
                   for n in range(PEAK_TO, PEAK_FROM + 1)
                   if charts[c].edition(today - timedelta(days=n)) is not None]
        earlier = [v for v in earlier if v is not None]
        if earlier and max(earlier) >= level_now:
            s *= COMEBACK
        if s < threshold:
            continue
        c = Candidate(key, akey, artist, title, url, round(s, 2), best, charts=sorted(confirming or [gain_chart]))
        c.facts = [_fact(charts[src], key, charts[src].edition(today)) for src in sorted(by_chart)
                   if (by_chart[src].g1 or 0) > 0 or src in confirming]
        out.append(c)
    out.sort(key=lambda c: (-c.score, c.key))
    return out[:limit]
