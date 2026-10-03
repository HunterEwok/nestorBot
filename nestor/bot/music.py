"""Songs on request: the model names the song and finds YouTube candidates; this module
checks them against public endpoints and builds the message.

No audio is downloaded or re-uploaded. The message carries a YouTube link whose preview
plays inside Telegram, and buttons to Apple Music, YouTube Music, Spotify and Yandex
Music: a direct track link where one could be verified, a search on that service
otherwise. Buttons are built here from validated ids on fixed hosts, never copied from
the model, so a prompt injection cannot turn the card into a phishing link.
"""
from __future__ import annotations

import asyncio
import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urlparse

import aiohttp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import prompts
from .llm import extract_json

_YT_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
APPLE_STOREFRONTS = ("ru", "us")    # the first one is also used for the search link

# «нестор, включи» / «включи эту песню» as a reply: the song is in the replied message.
_VERB = re.compile(r"^\s*(?:включи|вруби|поставь|сыграй|запусти|play|найди|скинь|пришли|дай)(?!\w)", re.I)
_FILLER = re.compile(r"(?<!\w)(?:мне|нам|пожалуйста|плиз|эту|этот|это|эти|её|ее|его|их|тут|здесь|песн\w*|трек\w*|"
                     r"музык\w*|клип\w*|композици\w*|отсюда|из\s+сообщения)(?!\w)", re.I)


def needs_reply_text(question: str) -> bool:
    """True when the request names no song by itself («включи эту песню»)."""
    rest = _VERB.sub("", question or "", count=1)
    rest = _FILLER.sub(" ", rest)
    return not re.sub(r"[\W_]+", "", rest)


def youtube_id(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https"):
        return None
    host = (u.hostname or "").lower()
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif host in _YT_HOSTS:
        parts = [p for p in u.path.split("/") if p]
        if u.path == "/watch":
            vid = (parse_qs(u.query).get("v") or [""])[0]
        elif len(parts) >= 2 and parts[0] in ("shorts", "embed", "live"):
            vid = parts[1]
        else:
            return None
    else:
        return None
    return vid if _YT_ID.fullmatch(vid) else None


def spotify_track_id(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    if u.scheme != "https" or (u.hostname or "").lower() != "open.spotify.com":
        return None
    parts = [p for p in u.path.split("/") if p]
    if parts and parts[0].startswith("intl-"):
        parts = parts[1:]
    if len(parts) >= 2 and parts[0] == "track" and _SPOTIFY_ID.fullmatch(parts[1]):
        return parts[1]
    return None


def apple_track_url(url: Any) -> str | None:
    """A music.apple.com album/song link from the iTunes Search API, without tracking parameters."""
    if not isinstance(url, str):
        return None
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    if u.scheme != "https" or (u.hostname or "").lower() != "music.apple.com":
        return None
    if not re.fullmatch(r"/[a-z]{2}/(?:album|song)/[^\s?#]+", u.path):
        return None
    track = (parse_qs(u.query).get("i") or [""])[0]
    return f"https://music.apple.com{u.path}" + (f"?i={track}" if track.isdigit() else "")


def _norm(text: Any) -> str:
    """Lowercase, ё→е, no accents, no brackets like «(Official Video)», words only."""
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKD", text.lower().replace("ё", "е"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[(\[].*?[)\]]", " ", text)
    return " ".join(re.findall(r"\w+", text))


def same_song(found: Any, wanted: str) -> bool:
    a, b = _norm(found), _norm(wanted)
    return bool(a and b) and (b in a or a in b)


@dataclass(frozen=True)
class Pick:
    artist: str
    title: str
    youtube_ids: tuple[str, ...]
    spotify_id: str | None

    @property
    def name(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title

    @property
    def query(self) -> str:
        return f"{self.artist} {self.title}".strip()


@dataclass(frozen=True)
class Say:
    """No song: the model asks which one is meant, or could not find it."""
    text: str


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().strip("\"«»")[:limit]


def parse(answer: str) -> Pick | Say | None:
    data = extract_json(answer)
    if data is None:
        return None
    question = _text(data.get("question"), 300)
    if question:
        return Say(question)
    title = _text(data.get("title"), 150)
    if not title:
        reason = _text(data.get("error"), 200)
        return Say(f"Не нашёл: {reason}" if reason else prompts.MUSIC_NOT_FOUND)
    raw = data.get("youtube")
    raw = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    ids: list[str] = []
    for url in raw[:6]:
        vid = youtube_id(url)
        if vid and vid not in ids:
            ids.append(vid)
    return Pick(_text(data.get("artist"), 100), title, tuple(ids[:3]), spotify_track_id(data.get("spotify")))


class Checker(Protocol):
    async def youtube(self, video_id: str) -> dict | None:
        """oEmbed data if the video exists and may be embedded, i.e. plays inside Telegram."""

    async def spotify(self, track_id: str) -> dict | None: ...

    async def apple(self, artist: str, title: str) -> str | None:
        """A verified Apple Music link for the song, or None."""


class HttpChecker:
    """Public endpoints that need no keys: YouTube and Spotify oEmbed, the iTunes Search API."""

    def __init__(self, timeout: float = 8):
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def _json(self, url: str, params: dict) -> Any:
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, headers={"User-Agent": "nestorBot/1.0"}) as s:
                async with s.get(url, params=params) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    async def youtube(self, video_id: str) -> dict | None:
        data = await self._json("https://www.youtube.com/oembed",
                                {"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"})
        return data if isinstance(data, dict) and data.get("title") else None

    async def spotify(self, track_id: str) -> dict | None:
        data = await self._json("https://open.spotify.com/oembed", {"url": f"https://open.spotify.com/track/{track_id}"})
        return data if isinstance(data, dict) and data.get("title") else None

    async def apple(self, artist: str, title: str) -> str | None:
        for country in APPLE_STOREFRONTS:
            data = await self._json("https://itunes.apple.com/search", {
                "term": f"{artist} {title}".strip(), "media": "music", "entity": "song", "limit": 10, "country": country})
            results = data.get("results") if isinstance(data, dict) else None
            for item in results if isinstance(results, list) else []:
                if not isinstance(item, dict):
                    continue
                if same_song(item.get("trackName"), title) and (not artist or same_song(item.get("artistName"), artist)):
                    url = apple_track_url(item.get("trackViewUrl"))
                    if url:
                        return url
        return None


@dataclass(frozen=True)
class Card:
    html: str                   # message text, parse_mode HTML
    plain: str                  # the same for the audit log
    preview_url: str | None     # YouTube video whose preview plays in Telegram
    keyboard: InlineKeyboardMarkup


def search_url(service: str, query: str) -> str:
    q = quote(query, safe="")
    return {
        "apple": f"https://music.apple.com/{APPLE_STOREFRONTS[0]}/search?term={q}",
        "youtube_music": f"https://music.youtube.com/search?q={q}",
        "spotify": f"https://open.spotify.com/search/{q}",
        "yandex": f"https://music.yandex.ru/search?text={q}",
    }[service]


async def _none() -> None:
    return None


async def build_card(pick: Pick, checker: Checker) -> Card:
    checks = await asyncio.gather(
        *(checker.youtube(vid) for vid in pick.youtube_ids),
        checker.spotify(pick.spotify_id) if pick.spotify_id else _none(),
        checker.apple(pick.artist, pick.title),
        return_exceptions=True)
    *video_checks, spotify_ok, apple = checks
    videos = [(vid, info) for vid, info in zip(pick.youtube_ids, video_checks, strict=True) if isinstance(info, dict)]
    matching = [v for v in videos if same_song(v[1].get("title"), pick.title)]
    video = (matching or videos or [(None, None)])[0][0]

    links = {
        "apple": (apple, True) if isinstance(apple, str) else (search_url("apple", pick.query), False),
        "youtube_music": ((f"https://music.youtube.com/watch?v={video}", True) if video
                          else (search_url("youtube_music", pick.query), False)),
        "spotify": ((f"https://open.spotify.com/track/{pick.spotify_id}", True) if isinstance(spotify_ok, dict)
                    else (search_url("spotify", pick.query), False)),
        "yandex": (search_url("yandex", pick.query), False),
    }
    labels = {"apple": "Apple Music", "youtube_music": "YouTube Music", "spotify": "Spotify", "yandex": "Яндекс Музыка"}

    def button(key: str) -> InlineKeyboardButton:
        url, direct = links[key]
        return InlineKeyboardButton(text=labels[key] if direct else f"🔎 {labels[key]}", url=url)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[button("apple"), button("youtube_music")],
                                                     [button("spotify"), button("yandex")]])
    name = html.escape(pick.name)
    preview = f"https://www.youtube.com/watch?v={video}" if video else None
    if preview:
        text = f'🎵 <b>{name}</b>\n<a href="{preview}">{prompts.MUSIC_PLAY}</a>'
    else:
        text = f"🎵 <b>{name}</b>\n{prompts.MUSIC_NO_PLAYER}"
    plain = "\n".join([pick.name, f"YouTube: {preview or '-'}"]
                      + [f"{labels[k]}: {u}" for k, (u, _) in links.items()])
    return Card(text, plain, preview, keyboard)
