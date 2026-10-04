"""Reggaeville (www.reggaeville.com), news and new releases: a server-rendered TYPO3 site with
no feed (/rss.xml and /feed/ are 404, ?type=9818 returns the HTML page), so the first page
of the list is parsed. "Show more" loads through an eID URL that robots.txt disallows:
only the first page is ever read.

Quirks:
- Entries are recognized by their href: /artist-details/<artist>/news/view/<slug>/ and
  /dates/festival-details/<event>/news/news/<slug>/ on the news page,
  /artist-details/<artist>/releases/release/<slug>/ on the releases page (the artist slug
  can be a placeholder such as "reggaeville"). Relative, root-relative (TYPO3 may set
  <base href>) or absolute on www.reggaeville.com.
- An entry links its article twice or more (image, then title): one item per canonical URL
  (https, www host, no query or fragment, single slashes, trailing slash) with the longest
  meaningful anchor text; img alt and title attributes only when no anchor has text.
- Dates are MM/DD/YYYY group headers before the entries; an entry gets the closest header
  preceding the link its title came from (or any other of its links) at 12:00 UTC, else None
  (an entry featured above the first header only). Dates inside texts are not headers. A
  date inside the link itself (a card with its own date) belongs to that entry, never to its title.
- The imprint forbids reusing texts and images: the summary stays empty, nothing but the
  list page is fetched.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, PageElement, Tag

from ..http import Http
from ..model import FetchResult, Item, Source

SITE = "https://www.reggaeville.com"
HOSTS = ("www.reggaeville.com", "reggaeville.com")
PATTERNS = {
    "news": re.compile(r"/news/(?:view|news)/[^/]+"),
    "releases": re.compile(r"/releases/release/[^/]+"),
}
TITLE_MAX = 300
URL_MAX = 2000

_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
# Only complete entities: html.unescape alone also decodes "&copy" in "&copy2026" to "©".
_ENTITY = re.compile(r"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#\d{1,7}|#[xX][0-9A-Fa-f]{1,6});")
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_DROP_TAGS = ("script", "style", "noscript", "template", "svg")
_GENERIC = frozenset({"more", "read more", "show more", "load more", "details", "continue", "continue reading",
                      "view", "view more", "click here", "here", "mehr", "weiterlesen", "listen", "watch"})
# NUL breaks Postgres TEXT; soft hyphens and zero-width spaces break the keyword filters.
_INVISIBLE = dict.fromkeys([*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F, 0xAD, 0x200B, 0x2060, 0xFEFF])


class _Seen(NamedTuple):
    text: str
    from_attr: bool             # img alt or a title attribute, not the anchor's text
    day: datetime | None


async def fetch(http: Http, source: Source, cursor: dict, now: datetime) -> FetchResult:
    """One GET of the list page; no cursor, the store recognizes the URLs it has seen."""
    page = source.params.get("page")
    if page not in PATTERNS:
        raise ValueError(f"reggaeville: unknown page {str(page)[:20]!r}")
    resp = await http.get(source.url)
    return FetchResult(parse_page(resp.text(), page, resp.url or source.url))


def parse_page(body: bytes | str, page: str, base_url: str = SITE + "/") -> list[Item]:
    """Every entry of the page in page order; ValueError("layout changed") when there is none."""
    pattern = PATTERNS[page]
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    base = soup.find("base", href=True)
    if isinstance(base, Tag):
        base_url = _on_site(base_url, str(base["href"])) or base_url

    found: dict[str, list[_Seen]] = {}
    day: datetime | None = None
    for node in soup.descendants:
        if type(node) is NavigableString:   # not comments, CDATA or the doctype
            day = _header_day(str(node)) or day
        elif isinstance(node, Tag) and node.name == "a" and node.get("href"):
            url = _canonical(str(node["href"]), base_url)
            if url is None or not pattern.search(urlsplit(url).path):
                continue
            # a card link with its own date inside: that date, not the header before the card
            when = _inner_day(node) or day
            seen = found.setdefault(url, [])
            seen.append(_Seen(_anchor_text(node), False, when))
            seen.extend(_Seen(t, True, when) for t in _attr_texts(node))
    if not found:
        raise ValueError("layout changed")

    items: list[Item] = []
    for url, seen in found.items():
        best = max((s for s in seen if _meaningful(s.text)), key=lambda s: (not s.from_attr, len(s.text)),
                   default=None)
        if best is None:
            continue
        day = best.day or next((s.day for s in seen if s.day), None)  # a featured copy above the headers
        items.append(Item(ext_id=url, url=url, title=_clip(best.text, TITLE_MAX), summary="", published_at=day,
                          lang="en", meta={"page": page}))
    return items


def _canonical(href: str, base_url: str) -> str | None:
    """https://www.reggaeville.com/<path>/ for a link on the site; None for anything else."""
    href = href.strip()
    if not href or len(href) > URL_MAX or any(ord(c) < 0x20 for c in href):
        return None
    try:
        parts = urlsplit(urljoin(base_url, href.replace(" ", "%20")))
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or (parts.hostname or "").lower() not in HOSTS:
        return None
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"
    return SITE + path


def _on_site(base_url: str, href: str) -> str | None:
    """The <base href> as an absolute URL when it stays on the site."""
    try:
        url = urljoin(base_url, href.strip())
        parts = urlsplit(url)
    except ValueError:
        return None
    return url if parts.scheme.lower() in ("http", "https") and (parts.hostname or "").lower() in HOSTS else None


def _header_day(text: str) -> datetime | None:
    m = _DATE.fullmatch(" ".join(text.split()))
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, 12, tzinfo=timezone.utc)
    except ValueError:
        return None


def _own(tag: Tag) -> list[PageElement]:
    """The nodes inside the tag, without the content of links nested in it: html.parser nests an
    unclosed <a> (a browser closes it), and walking every nested link again would be quadratic."""
    out: list[PageElement] = []
    stack: list[PageElement] = list(reversed(tag.contents))
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, Tag) and node.name != "a":
            stack.extend(reversed(node.contents))
    return out


def _strings(nodes: list[PageElement]) -> list[str]:
    return [str(n) for n in nodes if type(n) is NavigableString]   # not comments


def _inner_day(a: Tag) -> datetime | None:
    """A date inside the link (a card with its date) is the entry's, unless the link swallowed the
    links after it (an unclosed <a>): then that date is the header of the next group."""
    nodes = _own(a)
    if any(isinstance(n, Tag) and n.name == "a" for n in nodes):
        return None
    return next((d for s in _strings(nodes) if (d := _header_day(s))), None)


def _anchor_text(a: Tag) -> str:
    """The heading inside the anchor if it wraps a whole card, else all of its text but a date."""
    nodes = _own(a)
    heading = next((n for n in nodes if isinstance(n, Tag) and n.name in _HEADINGS), None)
    texts = _strings(_own(heading) if heading is not None else nodes)
    return _plain(" ".join(t for t in texts if _header_day(t) is None))


def _attr_texts(a: Tag) -> list[str]:
    imgs = [n for n in _own(a) if isinstance(n, Tag) and n.name == "img"]
    values = [a.get("title")] + [v for img in imgs for v in (img.get("alt"), img.get("title"))]
    return [_plain(v) for v in values if isinstance(v, str)]


def _meaningful(text: str) -> bool:
    key = " ".join(re.sub(r"[^\w\s]", " ", text).split()).casefold()
    return bool(key) and key not in _GENERIC and any(c.isalpha() for c in key)


def _plain(text: str) -> str:
    """Whitespace collapsed; entities the parser left (double-escaped markup) unescaped."""
    text = _ENTITY.sub(lambda m: html.unescape(m.group()), text)
    return " ".join(text.translate(_INVISIBLE).split())


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:.") + "…"
