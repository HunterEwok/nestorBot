"""RSS 2.0, Atom and RDF (RSS 1.0) feeds: every Source with kind "rss".

What the feeds we read look like, and what this module does about it:
- WordPress (PsyPost, Marijuana Moment, NORML, Leafly, Psychedelic Alpha, Lucid News, MAPS,
  World A Reggae, Нож): guid isPermaLink="false" (usually ?p=<id>, Marijuana Moment uses the
  article URL), the full post in content:encoded, repeated <category>, titles with numeric entities.
  Some append utm_* to links and tag advertorials "Sponsored" (High Times did, before it refused
  bots; filters.py drops such posts by params drop_categories); NORML descriptions open with an
  <img>; NORML guids carry "&#038;".
- Squarespace (Daily Reggae): opaque guids, so params id_from_link keys items by the link.
- ScienceDaily: dates in EDT/EST; its CDN serves stale copies, so params cache_buster adds
  ?_=<unix hour>; its terms forbid keeping the text, so params keep_text=False empties the summary.
- N+1 and Meduza: +0300 dates and CDATA titles; N+1's description only repeats the title.
- Psychedelic Alpha: params paid_marker ("Pα+") in the title or a category marks paywalled posts.
- PsyPost: the last paragraph of content:encoded links the paper on doi.org, which gives the DOI
  for the cross-source dedupe.

Feed bodies are untrusted: documents declaring XML entities are refused before parsing, the body
goes to feedparser as a stream (a bytes argument may be taken for a file name), and no link found
in the content is ever fetched.

feedparser is not given the feed's URL as a base: it would resolve every guid without
isPermaLink="false" against it, so an opaque guid ("12345") on an item without <link> became a
made-up link under the feed's directory. Relative <link>s are resolved here instead, and a link
feedparser took from a guid must be absolute.
"""
from __future__ import annotations

import calendar
import io
import logging
import re
import warnings
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import feedparser
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

from ..http import Http
from ..model import FetchResult, Item, Source
from ..text import INVISIBLE, squash

log = logging.getLogger("nestor.news")
# Short titles such as "https://..." or "file.mp3" make bs4 warn that it got a URL, not markup.
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

TITLE_MAX = 300
SUMMARY_MAX = 1500
URL_MAX = 2000           # longer values do not fit the btree indexes on url and ext_id
HTML_MAX = 50_000        # markup read for the summary; the DOI search sees all of it
CATEGORIES_MAX = 50

_TRACKING = re.compile(r"utm_\w*|fbclid|gclid|mc_cid|mc_eid", re.I)
_MARKUP = re.compile(r"<[a-zA-Z/!?]|&#?\w+;")
# Markup left in text that is supposed to be plain: what a feed escaped twice (&amp;lt;p&amp;gt;) or put
# in a plain-text element, and the "R&D;" feedparser's loose parser makes of a bare "R&D". A "<" before
# anything but a common tag ("<LOQ in 3 samples and >LOQ") is text and stays; script-like tags count
# only with their closing tag ("Why <script> tags worry security teams" is a title, not markup).
_ESCAPED = re.compile(
    r"&#?\w+;"
    r"|</?(?:p|a|b|i|u|s|q|em|strong|small|mark|del|cite|code|pre|sup|sub|br|hr|span|div|img|picture|figure"
    r"|figcaption|ul|ol|li|dl|dt|dd|h[1-6]|blockquote|section|article|header|footer|aside|table|caption|thead"
    r"|tbody|tr|td|th)\b[^<>]*>"
    r"|<(script|style|iframe|noscript|template)\b[^<>]*>.*?</\1\s*>", re.I | re.S)
_DROP_TAGS = ("script", "style", "noscript", "template", "iframe", "figcaption")
_BLOCK_TAGS = ("address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figure", "footer",
               "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "ol", "p", "pre", "section", "table",
               "td", "th", "tr", "ul")
_APPEARED = re.compile(r"The post .{1,400} appeared first on .{1,200}")   # WordPress/Yoast footer
# doi.org links, publisher links like /doi/full/10.1177/..., and "doi: 10.1038/..." in the text.
# The suffix is printable ASCII except "#&'<>?, so closing quotes like ” and » are not taken in.
_DOI = re.compile(r"(?:doi\.org/|/doi/(?:abs/|full/|pdf/|epdf/)?|\bdoi:\s*)(10\.\d{4,9}(?:/|%2f)[!$%(-;=@-~]+)", re.I)
_DOI_VALID = re.compile(r"10\.\d{4,9}/\S+")
# Expat expands internal entities; refuse them in every encoding feedparser would detect.
_ENTITY_DECL = tuple("<!ENTITY".encode(e) for e in ("ascii", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"))
_ENTITY_EBCDIC = "<!ENTITY".encode("cp037")


async def fetch(http: Http, source: Source, cursor: dict, now: datetime) -> FetchResult:
    """One GET of the feed; no cursor, the store recognizes entries it has seen by ext_id."""
    url = source.url
    if source.params.get("cache_buster"):
        url += ("&" if "?" in url else "?") + f"_={int(now.timestamp()) // 3600}"
    resp = await http.get(url)
    return FetchResult(parse(resp.body, source))


def parse(body: bytes, source: Source) -> list[Item]:
    """Every entry with a title and an http(s) link, in feed order, without duplicate ext_ids."""
    if _declares_entity(body):
        raise ValueError("XML entity declarations are refused")
    # WordPress sites often send a blank line before <?xml; expat then ignores the declared encoding.
    stripped = body.lstrip()
    if stripped.startswith(b"<"):
        body = stripped
    try:
        # application/xml: the encoding comes from the XML declaration (text/xml would mean ASCII).
        # No content-location on purpose, see the module docstring.
        feed = feedparser.parse(io.BytesIO(body), response_headers={"content-type": "application/xml"})
    except Exception as e:  # feedparser recovers from almost everything; anything else is a bad body
        raise ValueError(f"feed parser failed: {type(e).__name__}") from e
    entries = feed.get("entries") or []
    if not entries:
        if feed.get("bozo"):
            raise ValueError(f"not a feed: {str(feed.get('bozo_exception') or 'malformed')[:80]}")
        if not feed.get("version"):
            raise ValueError("not an RSS, Atom or RDF feed")
        return []  # a valid feed with no entries (Leafly's comment feeds)
    items: list[Item] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            item = _item(entry, source)
        except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError):
            log.debug("skipped a malformed entry", extra={"source": source.key})
            continue
        if item is not None and item.ext_id not in seen:
            seen.add(item.ext_id)
            items.append(item)
    return items


def normalize_link(value: Any) -> str | None:
    """An http(s) URL without tracking parameters (utm_*, fbclid, gclid, mc_cid, mc_eid) and
    without the #fragment; the rest is kept as written. None for anything else."""
    if not isinstance(value, str):
        return None
    value = value.strip().replace(" ", "%20")
    if not value or len(value) > URL_MAX or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
    except ValueError:
        return None
    query = "&".join(p for p in parts.query.split("&")
                     if p and not _TRACKING.fullmatch(unquote(p.split("=", 1)[0])))
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, query, ""))


def plain_text(value: Any, markup: bool = True) -> str:
    """Tags stripped, entities decoded, whitespace collapsed. A second pass only runs on entities
    or common tags left by double escaping, so an escaped "<" in the text survives the first one;
    entities of deeper levels (a CDATA title "It&amp;amp;#39;s") are decoded at the end.
    markup=False (feedparser's text/plain): "<" is text unless such markup is in the value."""
    text = value if isinstance(value, str) else ""
    for pattern in (_MARKUP if markup else _ESCAPED, _ESCAPED):
        if not pattern.search(text):
            break
        # the space: html.parser holds back an "&..." at the very end of its input and drops it ("R&D" -> "RD")
        soup = BeautifulSoup(text + " ", "html.parser")
        for tag in soup.find_all(_DROP_TAGS):
            tag.decompose()
        for tag in soup.find_all(_BLOCK_TAGS):
            tag.insert_before(" ")
            tag.insert_after(" ")
        text = soup.get_text()
    return squash(text)


def _item(entry: Any, source: Source) -> Item | None:
    url = _link(entry, source)
    full_title = plain_text(entry.get("title"), _is_markup(entry.get("title_detail")))
    title = _clip(full_title, TITLE_MAX)
    if not url or not title:
        return None
    guid = entry.get("id")
    guid = guid.strip().translate(INVISIBLE) if isinstance(guid, str) else ""
    ext_id = url if source.params.get("id_from_link") or not guid or len(guid) > URL_MAX else guid

    contents = [c for c in entry.get("content") or () if hasattr(c, "get") and isinstance(c.get("value"), str)]
    html_parts = [c["value"] for c in contents]
    summary_html = entry.get("summary") if isinstance(entry.get("summary"), str) else ""
    summary = ""
    if source.params.get("keep_text", True):
        texts = [(c["value"], _is_markup(c)) for c in contents]
        for raw, markup in (*texts, (summary_html, _is_markup(entry.get("summary_detail")))):
            summary = _drop_footer(plain_text(raw[:HTML_MAX], markup))
            if summary:
                break
        if summary.casefold() == full_title.casefold():
            summary = ""  # N+1 repeats the title in the description
        summary = _clip(summary, SUMMARY_MAX)

    categories = _categories(entry.get("tags") or ())
    meta: dict[str, Any] = {}
    marker = source.params.get("paid_marker")
    if marker and (marker.lower() in title.lower() or marker.lower() in categories):
        meta["paid"] = True

    doi = _doi(entry, html_parts, summary_html)
    return Item(ext_id=ext_id, url=url, title=title, summary=summary, published_at=_published(entry), doi=doi,
                lang=source.lang or "en", categories=categories, meta=meta)


def _link(entry: Any, source: Source) -> str | None:
    raw = entry.get("link")
    if not isinstance(raw, str) or not raw.strip():
        return None
    # feedparser copies a guid without isPermaLink="false" into a missing <link>: only a full URL will do.
    if entry.get("guidislink") and raw == entry.get("id"):
        return normalize_link(raw)
    return normalize_link(urljoin(source.url, raw.strip()))


def _is_markup(detail: Any) -> bool:
    """False when feedparser says the element is plain text (RSS titles without markup, Atom type="text")."""
    return not (hasattr(detail, "get") and detail.get("type") == "text/plain")


def _drop_footer(text: str) -> str:
    i = text.rfind("The post ")
    return text[:i].rstrip() if i >= 0 and _APPEARED.fullmatch(text, i) else text


def _published(entry: Any) -> datetime | None:
    # dict.get: FeedParserDict would map a missing updated_parsed to published_parsed with a warning.
    parsed = dict.get(entry, "published_parsed") or dict.get(entry, "updated_parsed")
    if not parsed:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)  # feedparser gives UTC
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _categories(tags: Iterable[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for tag in tags:
        term = plain_text(tag.get("term") if hasattr(tag, "get") else None).lower()
        if term and len(term) <= 100 and term not in out:
            out.append(term)
    return tuple(out[:CATEGORIES_MAX])


def _doi(entry: Any, html_parts: list[str], summary_html: str) -> str | None:
    """The paper's DOI: explicit RDF fields first, then a doi.org link of the entry, then the last
    DOI in the content (PsyPost links the paper in the closing paragraph), then the teaser."""
    explicit = [entry.get("prism_doi"), entry.get("dc_identifier"), entry.get("link")]
    explicit += [link.get("href") for link in entry.get("links") or () if hasattr(link, "get")]
    for value in explicit:
        if isinstance(value, str):
            doi = _clean_doi(value.strip()) if value.strip().startswith("10.") else _last_doi(value)
            if doi:
                return doi
    for text in ("\n".join(html_parts), summary_html):
        doi = _last_doi(text)
        if doi:
            return doi
    return None


def _last_doi(text: str) -> str | None:
    found = None
    for m in _DOI.finditer(text):
        found = _clean_doi(m.group(1)) or found
    return found


def _clean_doi(raw: str) -> str | None:
    # Cut at a decoded space or non-ASCII character: href="https://doi.org/10.1/x%E2%80%9D" ends in a quote.
    doi = re.split(r"[^!-~]", unquote(raw).strip(), maxsplit=1)[0]
    while doi:
        if doi[-1] in ".,;:!?'\"":
            doi = doi[:-1]
        elif doi[-1] in ")]}" and doi.count(doi[-1]) > doi.count({")": "(", "]": "[", "}": "{"}[doi[-1]]):
            doi = doi[:-1]  # a bracket that closes the sentence, not one of the DOI's own
        else:
            break
    doi = doi.lower()
    return doi if _DOI_VALID.fullmatch(doi) else None


def _clip(text: str, limit: int) -> str:
    """At most limit characters, cut on a word boundary with an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    if not text[limit - 1].isspace():
        space = head.rfind(" ")
        if space > limit // 2:
            head = head[:space]
    return head.rstrip(" ,;:.-–—") + "…"


def _declares_entity(body: bytes) -> bool:
    upper = body.upper()
    return any(p in upper for p in _ENTITY_DECL) or _ENTITY_EBCDIC in body
