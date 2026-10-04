"""Reddit through the Arctic Shift archive (arctic-shift.photon-reddit.com), a keyless
third-party copy of Reddit's API: Reddit's own .json answers 403 since May 2026, old.reddit
needs a login and the RSS ends on 13 Nov 2026. Arctic Shift itself ingests through Reddit's
API, so expect it to die around Q1 2027.

Quirks:
- /api/posts/search answers {"data": [<full Reddit post objects>]}, errors as
  {"data": null, "error": "..."}. A fields= list with permalink or removed_by_category is
  HTTP 400: fields= is never sent, the full objects come back.
- Score and num_comments stay 0-1 until the archive's second look (below), so the run asks
  once a day for the posts 36-60 h old; ranking is client side (the API cannot sort by score). The
  cursor keeps where the last window ended: a late run, or one after an outage, starts there,
  so no slice of posts falls between two windows. Up to CATCH_UP_H earlier than usual: the
  posts are then 96 h old, as far as service.MAX_AGE keeps anything new, and a host refusing
  for a day (15 + 30 min of retries, then REFUSED_BACKOFF) is caught up whole. A longer
  outage starts CATCH_UP_H early and loses only what lies before that.
- At most 100 posts per page: a full page is followed by another with before=<oldest
  created_utc>, up to MAX_PAGES. Pages may overlap, posts are deduped by id.
- removed_by_category is ABSENT on posts live at the first look and "reddit", "moderator",
  "deleted"... on removed ones. Reddit hides a removed body as "[removed]", "[deleted]" or a
  longer notice ("[ Removed by Reddit ]"): any of them is a body the bot never saw.
- The archive looks at a post twice: seconds after posting and again about 36 h later
  (_meta.retrieved_2nd_on; once 48 h in the records of 2026-10-04). The second look updates
  score, num_comments and the text. In those records, posts Reddit's filter held back at first
  and the moderators approved later kept removed_by_category "reddit" with their real text
  (score up to 91) and _meta {"was_initially_deleted": true, "removal_type": "reddit"}; posts
  removed at both looks had no such flag. So was_initially_deleted, a second look and a body
  mean live. Inferred, not seen live: removal_type without was_initially_deleted, or
  was_deleted_later, is a removal between the two looks (removed_by_category may then be
  absent) and counts as removed. Whether the second look ever writes a category is not known.
  A post the archive has not looked at again has the first look's score and removal state: in
  the usual window it is marked unsettled (filters.py drops it unstored) and the cursor stays
  at the oldest such post, so the next run asks for it again; once older than the usual window
  it is taken as it is. A second look later than about 60 h after posting is not waited for:
  the post is judged on its first look, which drops rather than passes it. meta removed_by
  keeps the raw category, so `python -m nestor.news --check -v` shows what the archive said.
- Titles, selftext and url are HTML-escaped (&amp; &lt; &gt;); selftext is Markdown. Titles
  pasted from elsewhere carry entities of their own ("It&amp;#39;s"): text is decoded to the end,
  links only the API's one level.
- Personal posts in r/Psychonaut and r/microdosing are dosing reports by nature:
  meta is_self and link let filters.py keep link posts only (params links_only).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from ..http import Http
from ..model import FetchResult, Item, Source
from ..text import squash, unescape

PAGE_SIZE = 100
MAX_PAGES = 5
WINDOW_START_H = 60     # after=now-60h
WINDOW_END_H = 36       # before=now-36h: about when the archive looks at a post again (fetch: unsettled)
CATCH_UP_H = 36         # a late run or an outage: start where the last window ended, at most this much earlier
TITLE_MAX = 300
SUMMARY_MAX = 1500
SELFTEXT_MAX = 10_000       # of the up to 40 000 chars of Markdown, enough for a 1500-char summary
URL_MAX = 2000
LINKS_MAX = 10
REDDIT = "https://www.reddit.com"

_SUB = re.compile(r"[A-Za-z0-9_]{2,21}")
_ID = re.compile(r"[0-9a-z]{1,16}")
_PERMALINK = re.compile(r"/r/[A-Za-z0-9_]{2,21}/comments/([0-9a-z]{1,16})/[A-Za-z0-9_%.~-]*/?")
# Reddit shows typed markup literally; only real HTML tags go, "<3" or "<series 3>" stay.
_TAG = re.compile(r"</?(?:a|abbr|b|blockquote|br|code|del|div|em|font|h[1-6]|hr|i|iframe|img|li|ol|p|pre|s|"
                  r"script|small|span|strike|strong|style|sub|sup|table|td|th|tr|u|ul)(?:\s[^<>]*)?/?>|<!--.{0,1000}?-->",
                  re.I | re.S)
# Selftext is untrusted and up to 40 000 chars: every lazy match is bounded, or "[[[[..." and "_a _a _a ..."
# take seconds of CPU on the event loop. Link targets may hold one level of parentheses:
# [text](https://en.wikipedia.org/wiki/Psilocybin_(drug)).
_MD_LINK = re.compile(r"!?\[([^\[\]\n]*)\]\(((?:[^()\s]|\([^()\s]*\))*)(?:\s+\"[^\"]*\")?\)")
# Telegram links, bare ones too ("t.me/name"): filters.py checks them against the blocklist.
_TG_LINK = re.compile(r"(?:\bhttps?://)?\b((?:www\.)?(?:t\.me|telegram\.(?:me|dog))/[^\s()\[\]<>\"']+)", re.I)
_MD_SPOILER = re.compile(r">!([^\n]{1,500}?)!<")
_MD_EMPHASIS = re.compile(r"(\*\*|__|~~|>!|!<)")
_MD_ITALIC = re.compile(r"(?<![\w*\\])([*_])(?=\S)([^\n]{1,300}?)(?<=\S)\1(?![\w*])")
_MD_LINE = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]+|(?:>[ \t]?)+|[*+-][ \t]+)")
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>~|^])")
# "[removed]", "[deleted]", "[ Removed by Reddit on account of violating the content policy. ]"
_GONE = re.compile(r"\[\s*(?:removed|deleted)\b.{0,200}", re.I | re.S)


class Page(NamedTuple):
    items: list[Item]
    posts: int                  # posts on the page, including the ones skipped
    oldest: int | None          # min created_utc on the page, the next page's before=


async def fetch(http: Http, source: Source, cursor: dict, now: datetime) -> FetchResult:
    """The posts created 36-60 h ago, newest first. A late run starts where the last window ended
    instead, up to CATCH_UP_H earlier; an early one keeps the usual start and the store dedupes
    the overlap by post id. The cursor keeps the window's end, or its start when MAX_PAGES did
    not reach the oldest posts, or the oldest unsettled post when that is earlier."""
    sub = source.params.get("subreddit")
    if not isinstance(sub, str) or not _SUB.fullmatch(sub):
        raise ValueError(f"arctic: bad subreddit {str(sub)[:30]!r}")
    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    start = int((now - timedelta(hours=WINDOW_START_H)).timestamp())
    after = start
    before = int((now - timedelta(hours=WINDOW_END_H)).timestamp())
    end = before
    unsettled: int | None = None    # the oldest post of the usual window the archive has not looked at again
    until = cursor.get("until") if isinstance(cursor, dict) else None
    if isinstance(until, int) and not isinstance(until, bool) and until < after:
        after = max(until, after - CATCH_UP_H * 3600) - 1   # the bounds may be exclusive; the overlap is deduped
    items: list[Item] = []
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        resp = await http.get(source.url, params={"subreddit": sub, "after": after, "before": before,
                                                   "limit": PAGE_SIZE, "sort": "desc"})
        page = parse_posts(resp.body, sub, source.lang)
        for item in page.items:
            if item.ext_id not in seen:
                seen.add(item.ext_id)
                items.append(item)
                created = int(item.published_at.timestamp()) if item.published_at else None
                if not item.meta["rechecked"] and created is not None and created >= start:
                    item.meta["unsettled"] = True
                    unsettled = created if unsettled is None else min(unsettled, created)
        if page.posts < PAGE_SIZE or page.oldest is None or not after < page.oldest < before:
            break
        before = page.oldest
    else:
        end = after   # the page cap cut the window short: its oldest part is unread, the next run starts there
    if unsettled is not None:
        end = min(end, unsettled)   # asked for again next run, and taken as it is once older than the usual window
    return FetchResult(items, cursor={"until": end})


def parse_posts(body: bytes, subreddit: str, lang: str = "en") -> Page:
    """One response of /api/posts/search; posts of another subreddit or without id or title are skipped."""
    try:
        data = json.loads(body)
    except ValueError:
        raise ValueError("arctic: not JSON") from None
    if not isinstance(data, dict):
        raise ValueError("arctic: unexpected response")
    if data.get("error"):
        raise ValueError(f"arctic: {' '.join(str(data['error']).split())[:60]}")
    posts = data.get("data")
    if not isinstance(posts, list):
        raise ValueError("arctic: no data list")
    items: list[Item] = []
    for p in posts:
        try:
            item = _post(p, subreddit, lang)
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
            continue  # one odd post does not cost the page
        if item is not None:
            items.append(item)
    created = [c for c in (_epoch(p.get("created_utc")) for p in posts if isinstance(p, dict)) if c is not None]
    return Page(items, len(posts), min(created) if created else None)


def _post(p: Any, subreddit: str, lang: str) -> Item | None:
    if not isinstance(p, dict):
        return None
    post_id = p.get("id")
    title = _clip(_plain(p.get("title")), TITLE_MAX)
    if not isinstance(post_id, str) or not _ID.fullmatch(post_id) or not title:
        return None
    sub = p.get("subreddit")
    if isinstance(sub, str) and sub.lower() != subreddit.lower():
        return None  # the archive mixed in another sub
    sub = sub if isinstance(sub, str) and _SUB.fullmatch(sub) else subreddit

    permalink = p.get("permalink")
    m = _PERMALINK.fullmatch(permalink) if isinstance(permalink, str) else None
    url = REDDIT + permalink if m and m.group(1) == post_id else f"{REDDIT}/r/{sub}/comments/{post_id}/"

    selftext = p.get("selftext") if isinstance(p.get("selftext"), str) else ""
    is_self = p.get("is_self") is True
    link = None if is_self else _link(p.get("url"))
    domain = p.get("domain")
    if not isinstance(domain, str) or not domain.strip() or len(domain) > 100:
        domain = f"self.{sub}" if is_self else (urlsplit(link).hostname or "") if link else ""
    rbc = p.get("removed_by_category")
    created = _epoch(p.get("created_utc"))
    gone = _GONE.fullmatch(selftext.strip()) is not None
    second = p.get("_meta") if isinstance(p.get("_meta"), dict) else {}
    rechecked = _epoch(second.get("retrieved_2nd_on")) is not None
    removal = second.get("removal_type")
    approved = second.get("was_initially_deleted") is True     # removed at the first look, live at the second
    later = second.get("was_deleted_later") is True or removal not in (None, "") and not approved
    restored = rechecked and approved and not later and not gone
    meta: dict[str, Any] = {
        "subreddit": sub,
        "score": _int(p.get("score")),
        "num_comments": _int(p.get("num_comments")),
        "is_self": is_self,
        "link": link,
        "domain": domain.strip(),
        "over_18": p.get("over_18") is True,
        "stickied": p.get("stickied") is True,
        # a body the bot never saw cannot be checked for trade or doses
        "removed": gone or later or rbc not in (None, "") and not restored,
        "flair": _plain(p.get("link_flair_text")) or None,
        "rechecked": rechecked,
    }
    category = next((c.strip()[:40] for c in (rbc, removal) if isinstance(c, str) and c.strip()), None)
    if category:
        meta["removed_by"] = category
    if later:
        meta["removed_later"] = True
    elif restored:
        meta["restored"] = True
    links = [] if gone else _links(selftext)
    if links:
        meta["links"] = links
    text = "" if gone else _markdown(selftext)
    return Item(ext_id=post_id, url=url, title=title, summary=_clip(text, SUMMARY_MAX),
                published_at=datetime.fromtimestamp(created, timezone.utc) if created is not None else None,
                lang=lang or "en", meta=meta, full_text=text)


def _link(value: Any) -> str | None:
    """The external URL of a link post (unescaped), http(s) only."""
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


def _links(selftext: str) -> list[str]:
    """Where the post links to, for filters.py (the summary keeps only link texts): Markdown
    link targets up to LINKS_MAX, Telegram links all of them. Never fetched."""
    text = unescape(selftext[:SELFTEXT_MAX], rounds=1)
    found = [_MD_ESCAPE.sub(r"\1", m.group(2)) for m in _MD_LINK.finditer(text)]
    found += ("https://" + m.group(1) for m in _TG_LINK.finditer(_MD_ESCAPE.sub(r"\1", text)))
    links: list[str] = []
    for link in filter(None, map(_link, found)):
        if link not in links and (len(links) < LINKS_MAX or _TG_LINK.match(link)):
            links.append(link)
    return links


def _epoch(value: Any) -> int | None:
    if isinstance(value, str) and len(value) < 30 and value.strip().replace(".", "", 1).isdigit():
        value = float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if 0 < value < 10_000_000_000 else None


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, float) and value == value and abs(value) < 1e12:
        return int(value)
    if isinstance(value, str) and len(value) < 16 and value.strip().lstrip("-").isdigit():
        return int(value)
    return value if isinstance(value, int) else 0


def _plain(value: Any) -> str:
    """Entities unescaped, tags stripped, whitespace collapsed."""
    if not isinstance(value, str):
        return ""
    return squash(_TAG.sub(" ", unescape(value)))


def _markdown(value: str) -> str:
    """Reddit Markdown as plain text: link texts kept, emphasis, headers, quotes and escapes dropped.
    The API escapes &, < and >; the Markdown source has entities of its own ("&#x200B;" lines):
    the Markdown is read after the API's level, the text decoded to the end."""
    text = unescape(value[:SELFTEXT_MAX], rounds=1)
    text = _MD_SPOILER.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_LINE.sub("", text)
    text = _MD_EMPHASIS.sub("", text)
    text = _MD_ITALIC.sub(r"\2", text)
    text = unescape(_MD_ESCAPE.sub(r"\1", text))
    return squash(_TAG.sub(" ", text))


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:.") + "…"
