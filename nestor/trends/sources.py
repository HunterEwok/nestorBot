"""The charts and how to read them. Each adapter turns one answer into a list of Entry.

Formats written from the services' public documentation or, for the unofficial Yandex and
Shazam endpoints, from what they returned when this was written: check them on the Pi with
`python -m nestor.trends --check -v` after an update, a changed format only breaks its own source.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from ..news.http import Http

NEW_DAYS = 30           # a song released this recently counts as new in a chart without its own mark


@dataclass(frozen=True)
class Entry:
    position: int
    artist: str
    title: str
    shift: int | None = None    # places gained since the chart's previous edition, when it says so
    is_new: bool = False        # a new entry, or a fresh release
    url: str | None = None


@dataclass(frozen=True)
class Chart:
    key: str
    name: str                   # for people: /music status, the model's facts
    kind: str                   # adapter
    needs_key: bool = False     # read only with LASTFM_API_KEY


CHARTS = (
    Chart("yandex_ru", "Яндекс Музыка, чарт России", "yandex"),
    Chart("apple_ru", "Apple Music, топ России", "apple"),
    Chart("shazam_ru", "Shazam, топ России", "shazam"),
    Chart("lastfm_ru", "Last.fm, топ России", "lastfm", needs_key=True),
)

_SPLIT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|x|×|&|vs\.?)\s+|\s*[,;]\s*|\s+/\s+", re.I)


def main_artist(artist: str) -> str:
    """«Artist feat. Other», «A, B», «A & B», «A x B» -> «Artist»."""
    return _SPLIT.split(artist.strip(), maxsplit=1)[0].strip() or artist.strip()


def artist_key(artist: str) -> str:
    text = unicodedata.normalize("NFKC", main_artist(artist)).casefold().replace("ё", "е")
    return re.sub(r"[^\w]+", " ", text).strip()


def _text(value: Any, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _fresh(released: Any, today: date) -> bool:
    try:
        day = date.fromisoformat(str(released)[:10])
    except ValueError:
        return False
    return today - timedelta(days=NEW_DAYS) <= day <= today + timedelta(days=1)


def _entry(position: Any, artist: Any, title: Any, **kw) -> Entry | None:
    artist, title = _text(artist), _text(title)
    if not isinstance(position, int) or position < 1 or not artist or not title:
        return None
    return Entry(position, artist, title, **kw)


async def apple(http: Http, now: datetime, key: str = "") -> list[Entry]:
    """Apple's marketing feed of the most played songs in the Russian storefront: no shifts, release dates."""
    data = (await http.get("https://rss.marketingtools.apple.com/api/v2/ru/music/most-played/100/songs.json")).json()
    results = (data.get("feed") or {}).get("results")
    if not isinstance(results, list):
        raise ValueError("no feed.results")
    out = [_entry(i, r.get("artistName"), r.get("name"), is_new=_fresh(r.get("releaseDate"), now.date()),
                  url=r.get("url") if str(r.get("url", "")).startswith("https://music.apple.com/") else None)
           for i, r in enumerate(results, 1) if isinstance(r, dict)]
    return [e for e in out if e]


async def yandex(http: Http, now: datetime, key: str = "") -> list[Entry]:
    """The chart of Russia from Yandex Music's own app API (unofficial, no token): position, the move since
    the previous chart (progress up/down/same/new and shift) and the track id."""
    data = (await http.get("https://api.music.yandex.net/landing3/chart/russia",
                           headers={"Accept": "application/json"})).json()
    tracks = (((data.get("result") or {}).get("chart") or {}).get("tracks"))
    if not isinstance(tracks, list):
        raise ValueError("no result.chart.tracks")
    out = []
    for t in tracks:
        track, chart = (t.get("track") or {}), (t.get("chart") or {})
        artists = [a.get("name") for a in track.get("artists") or [] if isinstance(a, dict) and a.get("name")]
        progress, shift = chart.get("progress"), chart.get("shift")
        moved = abs(shift) if isinstance(shift, int) else None
        signed = {"up": moved, "down": -moved if moved is not None else None, "same": 0}.get(progress)
        tid = track.get("id")
        url = f"https://music.yandex.ru/track/{tid}" if str(tid or "").isdigit() else None
        e = _entry(chart.get("position"), ", ".join(artists), track.get("title"), shift=signed,
                   is_new=progress == "new", url=url)
        if e:
            out.append(e)
    return out


async def shazam(http: Http, now: datetime, key: str = "") -> list[Entry]:
    """Shazam's top 200 of Russia as the CSV its chart page offers for download: rank, artist, title.
    A few lines of title come before the table."""
    text = (await http.get("https://www.shazam.com/services/charts/csv/top-200/russia")).text()
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 3 and row[0].strip().isdigit():
            e = _entry(int(row[0]), row[1], row[2])
            if e:
                out.append(e)
    if not out:
        raise ValueError("no chart rows in the CSV")
    return out


LASTFM = "https://ws.audioscrobbler.com/2.0/"


async def lastfm(http: Http, now: datetime, key: str = "") -> list[Entry]:
    """Last.fm's weekly top tracks of Russia (geo.getTopTracks), with an API key."""
    if not key:
        raise ValueError("LASTFM_API_KEY is not set")
    data = (await http.get(LASTFM, params={"method": "geo.gettoptracks", "country": "Russian Federation",
                                           "limit": 100, "api_key": key, "format": "json"})).json()
    tracks = ((data.get("tracks") or {}).get("track"))
    if not isinstance(tracks, list):
        raise ValueError(f"no tracks.track: {_text(data.get('message'), 80)}")
    out = []
    for i, t in enumerate(tracks, 1):
        artist = t.get("artist")
        e = _entry(i, artist.get("name") if isinstance(artist, dict) else artist, t.get("name"),
                   url=t.get("url") if str(t.get("url", "")).startswith("https://www.last.fm/") else None)
        if e:
            out.append(e)
    return out


async def lastfm_listeners(http: Http, key: str, artist: str) -> int | None:
    """An artist's listeners on Last.fm (artist.getInfo), None if it does not know them."""
    data = (await http.get(LASTFM, params={"method": "artist.getinfo", "artist": artist, "autocorrect": 1,
                                           "api_key": key, "format": "json"})).json()
    value = ((data.get("artist") or {}).get("stats") or {}).get("listeners")
    return int(value) if str(value or "").isdigit() else None


ADAPTERS: dict[str, Callable[[Http, datetime, str], Awaitable[list[Entry]]]] = {
    "apple": apple, "yandex": yandex, "shazam": shazam, "lastfm": lastfm,
}
