"""The charts and how to read them. Each adapter turns one answer into a list of Entry.

Formats written from the services' public documentation or, for the unofficial Yandex and
Shazam endpoints, from what they returned when this was written: check them on the Pi with
`python -m nestor.trends --check -v` after an update, a changed format only breaks its own source.
"""
from __future__ import annotations

import csv
import html
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
class Options:
    lastfm_key: str = ""        # LASTFM_API_KEY
    yandex_proxy: str = ""      # MUSIC_YANDEX_PROXY: Yandex serves its chart only to Russian addresses


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
    Chart("shazam_msk", "Shazam, топ Москвы", "shazam_msk"),
    Chart("lastfm_ru", "Last.fm, топ России", "lastfm", needs_key=True),
)

_SPLIT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|x|×|&|vs\.?)\s+|\s*[,;]\s*|\s+/\s+", re.I)


def main_artist(artist: str) -> str:
    """«Artist feat. Other», «A, B», «A & B», «A x B» -> «Artist»."""
    return _SPLIT.split(artist.strip(), maxsplit=1)[0].strip() or artist.strip()


def artist_key(artist: str) -> str:
    text = unicodedata.normalize("NFKC", main_artist(artist)).casefold().replace("ё", "е")
    return re.sub(r"[^\w]+", " ", text).strip()


_BRACKETS = re.compile(r"\s*[(\[][^)\]]*[)\]]")
_FEAT_TAIL = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|prod\.?)\s.*$", re.I)


def song_key(artist: str, title: str) -> str:
    """One song across the charts: the main artist and the title without brackets, «feat.» and case.
    «Песня (feat. Б)» in one chart and «Песня» in another are the same song."""
    name = _FEAT_TAIL.sub("", _BRACKETS.sub("", title)).strip() or title
    text = unicodedata.normalize("NFKC", name).casefold().replace("ё", "е")
    return artist_key(artist) + "|" + re.sub(r"[^\w]+", " ", text).strip()


def _text(value: Any, limit: int = 200) -> str:
    """Apple's feed and Shazam's CSV send names HTML-escaped («Xcho &amp; МОТ»)."""
    return re.sub(r"\s+", " ", html.unescape(value)).strip()[:limit] if isinstance(value, str) else ""


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


APPLE_URL = "https://rss.marketingtools.apple.com/api/v2/ru/music/most-played/100/songs.json"
SHAZAM_URL = "https://www.shazam.com/services/charts/csv/top-200/russia"
SHAZAM_MSK_URL = "https://www.shazam.com/services/charts/csv/top-50/russia/moscow"


async def apple(http: Http, now: datetime, opts: Options = Options()) -> list[Entry]:
    """Apple's marketing feed of the most played songs in the Russian storefront: no shifts, release dates."""
    return parse_apple((await http.get(APPLE_URL)).json(), now.date())


def parse_apple(data: Any, today: date) -> list[Entry]:
    results = ((data if isinstance(data, dict) else {}).get("feed") or {}).get("results")
    if not isinstance(results, list):
        raise ValueError("no feed.results")
    out = [_entry(i, r.get("artistName"), r.get("name"), is_new=_fresh(r.get("releaseDate"), today),
                  url=r.get("url") if str(r.get("url", "")).startswith("https://music.apple.com/") else None)
           for i, r in enumerate(results, 1) if isinstance(r, dict)]
    return [e for e in out if e]


async def yandex(http: Http, now: datetime, opts: Options = Options()) -> list[Entry]:
    """The chart of Russia from Yandex Music's own app API (unofficial, no token): position, the move since
    the previous chart (progress up/down/same/new and shift) and the track id."""
    data = (await http.get("https://api.music.yandex.net/landing3/chart/russia",
                           headers={"Accept": "application/json"}, proxy=opts.yandex_proxy or None)).json()
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


async def shazam(http: Http, now: datetime, opts: Options = Options()) -> list[Entry]:
    """Shazam's top 200 of Russia as the CSV its chart page offers for download: rank, artist, title.
    A few lines of title come before the table."""
    return parse_shazam((await http.get(SHAZAM_URL)).text())


async def shazam_msk(http: Http, now: datetime, opts: Options = Options()) -> list[Entry]:
    """Shazam's top 50 of Moscow: a song often climbs in the city before it climbs in the country."""
    return parse_shazam((await http.get(SHAZAM_MSK_URL)).text())


def parse_shazam(text: str) -> list[Entry]:
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


async def lastfm(http: Http, now: datetime, opts: Options = Options()) -> list[Entry]:
    """Last.fm's weekly top tracks of Russia (geo.getTopTracks), with an API key."""
    if not opts.lastfm_key:
        raise ValueError("LASTFM_API_KEY is not set")
    data = (await http.get(LASTFM, params={"method": "geo.gettoptracks", "country": "Russian Federation",
                                           "limit": 100, "api_key": opts.lastfm_key, "format": "json"})).json()
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


ADAPTERS: dict[str, Callable[[Http, datetime, Options], Awaitable[list[Entry]]]] = {
    "apple": apple, "yandex": yandex, "shazam": shazam, "shazam_msk": shazam_msk, "lastfm": lastfm,
}
