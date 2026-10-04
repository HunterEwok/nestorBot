"""Plain text out of what the sources send: HTML entities decoded, invisible characters dropped,
whitespace collapsed. Shared by every adapter, so a title comes out the same whichever site
escaped it how many times.

Sources escape once (Reddit's API), twice (the t.me preview, some WordPress feeds) or three
times (a Reggaeville title seen live: "&amp;amp;#39;"). A leftover "&#39;" or "&#8364;" is not
cosmetic: it splits story titles in store.title_key and hides "€" and "@" from the trade and
blocklist patterns of filters.py.
"""
from __future__ import annotations

import html
import re
from html.entities import html5

# Complete entities only, and only exact HTML5 names: html.unescape alone decodes "&sect" in a URL's
# "&section=" to "§", "&copy" in "&copy2026", and the known head of an unknown name ("&regex;" -> "®ex;").
ENTITY = re.compile(r"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#\d{1,7}|#[xX][0-9A-Fa-f]{1,6});")
# Every decoded entity shortens the text, so the loop ends by itself; the cap bounds the work on hostile input.
ROUNDS = 4
# NUL breaks Postgres TEXT; soft hyphens and zero-width spaces (Reddit's "&#x200B;" lines) break the keyword filters.
INVISIBLE = dict.fromkeys([*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F, 0xAD, 0x200B, 0x2060, 0xFEFF])


def _entity(m: re.Match[str]) -> str:
    s = m.group()
    return html.unescape(s) if s[1] == "#" else html5.get(s[1:], s)


def unescape(text: str, rounds: int = ROUNDS) -> str:
    """Complete entities decoded, level after level, at most `rounds` levels. Links take rounds=1:
    the one level their source escaped, so a link's identity (dedupe, filters) never changes."""
    for _ in range(rounds):
        if "&" not in text:
            break
        decoded = ENTITY.sub(_entity, text)
        if decoded == text:
            break
        text = decoded
    return text


def squash(text: str) -> str:
    """The last step of every text field: entities decoded, invisible characters dropped, whitespace collapsed."""
    return " ".join(unescape(text).translate(INVISIBLE).split())
