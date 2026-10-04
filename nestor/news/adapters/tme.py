"""Public Telegram channels through their web preview https://t.me/s/<channel>: no account,
no API key. Telegram's terms frown on scraping, so this stays polite: one page per channel
per run (hourly at most), a link to t.me/<channel>/<id>, a short excerpt, no media.

What the preview page looks like:
- Up to ~20 newest posts, oldest first, each a div.tgme_widget_message[data-post="<channel>/<id>"]
  inside div.tgme_widget_message_wrap, all inside section.tgme_channel_history. ?after=<id>
  returns the posts after that id (verified), the cursor keeps the highest id seen.
- Text in div.tgme_widget_message_text with <br> line breaks; posts with media only have none
  and are skipped. A reply quotes its parent in a.tgme_widget_message_reply, never taken as text.
- The time is a.tgme_widget_message_date > time[datetime], ISO 8601 in UTC (the visible text
  is only HH:MM).
- Service posts ("Channel photo updated", the inactive-owner notice) carry class service_message.
- Forwards show div.tgme_widget_message_forwarded_from with a link to the original post.
  meta forwarded_from (that link) and links (hrefs of the post, Telegram ones past the cap
  too) let filters.py catch reposts of and links to blocked channels; they are never fetched.
- Since April 2024 the preview double-escapes "&" in hrefs (RSSHub), maybe in text too: hrefs
  are unescaped once more, the text and the forward label to the end.
- A channel with the preview disabled (copyright, porn, politics), a user, a bot or a missing
  name all answer 200 with the "tgme_page" contact/info card instead of posts: "no_preview".
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from ..http import Http
from ..model import FetchResult, Item, Source
from ..sources import BLOCKED_CHANNELS
from ..text import INVISIBLE, squash, unescape

BASE = "https://t.me"
TITLE_MAX = 120
SUMMARY_MAX = 1500
TEXT_MAX = 10_000           # full_text for the filters; a post holds up to 4096 chars
URL_MAX = 2000
LINKS_MAX = 10
# A preview page holds ~20 posts. Posts past the cap are not counted in the cursor: the next run gets them.
MESSAGES_MAX = 50

_CHANNEL = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_POST = re.compile(r"([A-Za-z][A-Za-z0-9_]{3,31})/(\d{1,12})")
_TELEGRAM = re.compile(r"https?://(?:www\.)?(?:t\.me|telegram\.(?:me|dog))/", re.I)
_LINE_TAGS = ("blockquote", "pre", "div", "p", "li")


class Page(NamedTuple):
    items: list[Item]
    max_id: int | None          # highest post id on the page, skipped posts included


async def fetch(http: Http, source: Source, cursor: dict, now: datetime) -> FetchResult:
    """The first preview page, or the posts after cursor["last_id"]."""
    channel = source.params.get("channel")
    if not isinstance(channel, str) or not _CHANNEL.fullmatch(channel):
        raise ValueError(f"tme: bad channel name {str(channel)[:40]!r}")
    if channel.lower() in BLOCKED_CHANNELS:
        raise ValueError(f"tme: channel @{channel} is blocked")
    last = _last_id(cursor)
    resp = await http.get(f"{BASE}/s/{channel}", params={"after": last} if last else None)
    page = parse_page(resp.text(), channel)
    newest = max((i for i in (last, page.max_id) if i), default=None)
    return FetchResult(page.items, {"last_id": newest} if newest else None)


def parse_page(body: bytes | str, channel: str) -> Page:
    """Items for the text posts of the page, in page order (oldest first)."""
    soup = BeautifulSoup(body, "html.parser")
    messages = soup.find_all("div", class_="tgme_widget_message")
    if not messages:
        if soup.find(class_=["tgme_channel_history", "tgme_widget_message_wrap"]):
            return Page([], None)  # nothing after the cursor
        if soup.find(class_="tgme_page"):
            raise ValueError("no_preview")
        raise ValueError("tme: not a channel preview page")
    messages = messages[:MESSAGES_MAX]
    if any(m.find_parent("div", class_="tgme_widget_message") is not None for m in messages):
        raise ValueError("tme: posts nested in posts")   # unclosed divs: every post would carry the next ones
    items: list[Item] = []
    max_id: int | None = None
    seen: set[str] = set()
    for msg in messages:
        m = _POST.fullmatch(str(msg.get("data-post") or "").strip())
        if not m or m.group(1).lower() != channel.lower():
            continue
        post_id = int(m.group(2))
        max_id = post_id if max_id is None else max(max_id, post_id)
        if _is_service(msg) or str(post_id) in seen:
            continue
        item = _item(msg, channel, post_id)
        if item is not None:
            seen.add(item.ext_id)
            items.append(item)
    return Page(items, max_id)


def _item(msg: Tag, channel: str, post_id: int) -> Item | None:
    # the post's own text, not the quote of the post it replies to
    node = next((t for t in msg.find_all("div", class_="tgme_widget_message_text")
                 if t.find_parent(class_="tgme_widget_message_reply") is None), None)
    lines = _text_lines(node) if node is not None else []
    if not lines:
        return None  # media only
    meta: dict[str, Any] = {"channel": channel, "forwarded": False}
    forward = msg.find(class_="tgme_widget_message_forwarded_from")
    if isinstance(forward, Tag):
        # where it came from, so filters.py can catch reposts of blocked channels
        name = forward.find(class_="tgme_widget_message_forwarded_from_name")
        label = squash((name or forward).get_text(" ")).removeprefix("Forwarded from").strip()
        meta["forwarded"] = True
        meta["forwarded_from"] = (_href(name.get("href")) if isinstance(name, Tag) else None) or label or None
    links: list[str] = []
    anchors = [*node.find_all("a", href=True), *msg.find_all("a", class_="tgme_widget_message_link_preview")]
    for href in (_href(a.get("href")) for a in anchors):
        # kept for the filters, never fetched; Telegram links past the cap too, for the blocklist
        if href and href not in links and (len(links) < LINKS_MAX or _TELEGRAM.match(href)):
            links.append(href)
    if links:
        meta["links"] = links
    text = " ".join(lines)
    return Item(ext_id=str(post_id), url=f"{BASE}/{channel}/{post_id}", title=_clip(lines[0], TITLE_MAX),
                summary=_clip(text, SUMMARY_MAX), published_at=_time(msg), lang="", meta=meta,
                full_text=text[:TEXT_MAX])


def _is_service(msg: Tag) -> bool:
    return "service_message" in (msg.get("class") or ()) or msg.find(class_="service_message") is not None


def _href(value: Any) -> str | None:
    """An absolute http(s) link; "&amp;" left in hrefs by the preview's double escaping is undone."""
    if not isinstance(value, str):
        return None
    value = unescape(value, rounds=1).strip()
    if not value or len(value) > URL_MAX or any(c.isspace() or ord(c) < 0x20 for c in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme.lower() in ("http", "https") and parts.hostname else None


def _text_lines(node: Tag) -> list[str]:
    """The text as non-empty lines, whitespace collapsed in each; <br> and block tags end a line.
    One pass over the nodes, the tree left as it is (replace_with per <br> is quadratic)."""
    parts: list[str] = []
    stack: list[Any] = list(reversed(node.contents))
    while stack:
        el = stack.pop()
        if el is None or (isinstance(el, Tag) and el.name == "br"):
            parts.append("\n")              # None: the end of a block tag
        elif isinstance(el, Tag):
            if el.name in _LINE_TAGS:
                parts.append("\n")
                stack.append(None)
            stack.extend(reversed(el.contents))
        elif type(el) is NavigableString:   # not comments
            parts.append(str(el))
    text = unescape("".join(parts))
    lines = (" ".join(line.translate(INVISIBLE).split()) for line in text.split("\n"))
    return [line for line in lines if line]


def _time(msg: Tag) -> datetime | None:
    link = msg.find("a", class_="tgme_widget_message_date")
    node = link.find("time", attrs={"datetime": True}) if isinstance(link, Tag) else None
    value: Any = node.get("datetime") if isinstance(node, Tag) else None
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):   # "0001-01-01T00:00+01:00" overflows in UTC
        return None


def _last_id(cursor: Any) -> int | None:
    value = cursor.get("last_id") if isinstance(cursor, dict) else None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10**12 else None


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:.") + "…"
