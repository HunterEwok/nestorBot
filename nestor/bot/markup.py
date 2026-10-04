"""Model answers in Telegram formatting.

Models write a small Markdown subset (prompts.FORMAT asks for it); this module turns it into
the HTML Telegram accepts with parse_mode=HTML: **bold**, *italic*, ~~strike~~, ||spoiler||,
`code`, ``` blocks, «- » lists, «> » quotes and [text](https://…) links. Anything else stays
literal text, escaped. Every tag comes from one token as a matched pair, so the output is
always well-formed.

References to archive messages ([«quote»](#37285), a bare #37285, or a t.me/c link into the
same chat) become links to the message. The address is built by code from the message id,
never taken from the model; ids the archive does not show (deleted, author used /forget_me)
keep their text without a link. A bare number needs three digits or more: «проблема #1» is
a count, not a message. A link to any other site shows its real domain next to it, unless
the label is that very domain, so a label cannot hide where it leads.
"""
from __future__ import annotations

import html
import re
from collections.abc import Collection
from dataclasses import dataclass
from urllib.parse import urlsplit

TELEGRAM_LIMIT = 4096


@dataclass(frozen=True)
class Ref:
    """An archive message the answer refers to."""
    url: str | None         # None: no t.me link exists (basic groups)
    quote: str              # a short piece of its text, for a bare #id
    user_id: int | None = None   # its author: /forget_me also removes answers that quote them
    bare: bool = True       # a bare #id may mean it; False: the model was not shown it («issue #1234»)


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """SC-6: split on paragraph, then line, then word boundaries, never inside a link, a
    quote-link or a bold span. Only the separator goes at the cut, so an indented line (code,
    a nested list item) keeps its indentation in the next part."""
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        cut = _cut_point(rest, limit)
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n" if rest[cut] == "\n" else " ")
    if rest:
        parts.append(rest)
    return parts


def chunk_lines(lines: list[str], limit: int = TELEGRAM_LIMIT) -> list[list[str]]:
    """Whole lines of HTML grouped into messages within the limit (a long list, one item a line)."""
    chunks: list[list[str]] = [[]]
    for line in lines:
        if chunks[-1] and visible_len("\n".join(chunks[-1] + [line])) > limit:
            chunks.append([])
        chunks[-1].append(line)
    return chunks


def _cut_point(text: str, limit: int) -> int:
    for sep in ("\n\n", "\n"):
        at = text.rfind(sep, 0, limit)
        if at > limit // 2:
            return at
    spans = [m.span() for m in _INLINE.finditer(text) if m.start() < limit]
    at = text.rfind(" ", 0, limit)
    while at > limit // 2 and any(start < at < end for start, end in spans):
        at = text.rfind(" ", 0, at)
    return at if at > limit // 2 else limit


# ---- references to archive messages ----

_TME = r"https?://t\.me/c/(?P<{0}>\d{{1,15}})/(?:\d{{1,12}}/)?(?P<{1}>\d{{1,12}})"
_TME_LINK = re.compile(_TME.format("internal", "message"))
_BARE_REF = re.compile(r"(?<![\w#&/])#([1-9]\d{2,11})(?!\w)")    # three digits or more
_LINK_REF = re.compile(r"\]\(\s*#(\d{1,12})\s*\)")                  # [«quote»](#5): any number
# «#37285 (https://t.me/c/…/37285)»: the same message twice, keep the id only.
_REF_AND_LINK = re.compile(r"#(?P<id>\d{1,12})[ \t]*[(\[]?[ \t]*" + _TME.format("internal", "message")
                           + r"[ \t]*[)\]]?")
MAX_REFS = 50


def _internal(chat_id: int | None) -> str | None:
    """The chat's id inside t.me/c links: only supergroups have them."""
    if chat_id is None or chat_id > -1000000000000:
        return None
    return str(-chat_id - 1000000000000)


def ref_ids(text: str, chat_id: int | None = None, known: Collection[int] | None = None) -> list[int]:
    """Message ids an answer refers to, first seen first (at most MAX_REFS). known: the ids the
    model was shown; a bare #id outside them is a number («issue #1234»), not a reference."""
    internal = _internal(chat_id)
    found = [(m.start(), int(m.group(1))) for m in _BARE_REF.finditer(text or "")
             if known is None or int(m.group(1)) in known]
    found += [(m.start(), int(m.group(1))) for m in _LINK_REF.finditer(text or "")]
    if internal is not None:
        found += [(m.start(), int(m.group("message"))) for m in _TME_LINK.finditer(text or "")
                  if m.group("internal") == internal]
    ids: list[int] = []
    for _, value in sorted(found):
        if value not in ids:
            ids.append(value)
    return ids[:MAX_REFS]


def linked_ids(text: str, chat_id: int | None) -> set[int]:
    """Ids given as t.me/c links into this chat: they name the supergroup itself, never the
    basic group it was migrated from."""
    internal = _internal(chat_id)
    if internal is None:
        return set()
    return {int(m.group("message")) for m in _TME_LINK.finditer(text or "") if m.group("internal") == internal}


def quote(text: str | None, limit: int = 60) -> str:
    """«The first words of a message…» for a bare reference."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return "сообщение"
    if len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        text = text[:cut if cut > limit // 2 else limit].rstrip(" ,.;:—-") + "…"
    return f"«{text}»"


# ---- inline markup ----

_INLINE = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\[(?P<label>[^\[\]\n]{1,300})\]\(\s*(?P<target>#\d{1,12}|https?://[^\s()<>]+(?:\([^\s()<>]*\)[^\s()<>]*)*)\s*\)"
    r"|\[(?P<badlabel>[^\[\]\n]{1,300})\]\((?!\s*(?:#|https?://))[^()\s]*(?:\([^()\s]*\)[^()\s]*)*\)"
    r"|(?P<url>https?://[^\s<>]+)"
    r"|(?<![\w#&/])#(?P<ref>[1-9]\d{2,11})(?!\w)"
    r"|(?<!\w)\*\*\*(?P<bolditalic>[^\s*](?:[^\n]*?[^\s*])??)\*\*\*(?!\w)"
    r"|(?<!\w)\*\*(?P<bold>[^\s*](?:[^\n]*?[^\s*])??)\*\*(?!\w)"
    r"|~~(?P<strike>[^\s~](?:[^\n]*?[^\s~])??)~~"
    r"|\|\|(?P<spoiler>[^\s|](?:[^\n]*?[^\s|])??)\|\|"
    r"|(?<![\w*])\*(?P<italic>[^\s*](?:[^*\n]*?[^\s*])?)\*(?![\w*])"
)

_KINDS = ("code", "label", "badlabel", "url", "ref", "bolditalic", "bold", "strike", "spoiler", "italic")
_TRAIL = ".,;:!?)»\"'"
_STRAY = re.compile(r"(?<!\w)\*\*(?!\s|\*|$)|(?<=\S)(?<!\*)\*\*(?!\w)")


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _plain(text: str) -> str:
    """Text between markup: escaped, without the stray ** of an unclosed bold."""
    return _esc(_STRAY.sub("", text))


def _a(url: str, label_html: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{label_html}</a>'


_MIXED = re.compile(r"(?=.*[a-z])(?=.*[^\x00-\x7f])")     # Latin and other letters in one label
_BIDI = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")      # direction overrides reverse what follows


def _host(url: str) -> str | None:
    """The domain a browser opens, or None when it is unclear: userinfo («a.com@b.com»),
    backslashes and percent signs are read differently by browsers and by Python. A label
    that mixes scripts («sbеrbank.ru» with a Cyrillic е) is shown the way the address bar
    shows it, in its xn-- form; «сбер.рф» stays readable."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:          # «https://rbc.ru[1]» and other broken addresses
        return None
    if not host or re.search(r"[@\\%]", parts.netloc) or "\\" in url:
        return None
    host = host.lower().removeprefix("www.")
    if host.isascii():
        return host
    try:
        ascii_host = host.encode("idna").decode()
    except UnicodeError:
        return None
    return ascii_host if any(_MIXED.match(label) for label in host.split(".")) else host


def _names_host(label: str, host: str) -> bool:
    """The label is the link's own domain (or its address): no need to repeat it."""
    shown = re.sub(r"^https?://", "", label.strip().lower()).removeprefix("www.")
    return re.split(r"[/?#:]", shown, maxsplit=1)[0] == host


class _Renderer:
    def __init__(self, refs: dict[int, Ref] | None, chat_id: int | None):
        self.refs = refs or {}
        self.internal = _internal(chat_id)

    def _own(self, url: str) -> int | None:
        """The message id if the URL is a t.me/c link into this chat."""
        m = _TME_LINK.fullmatch(url)
        if m and self.internal is not None and m.group("internal") == self.internal:
            return int(m.group("message"))
        return None

    def _ref(self, message_id: int, label: str | None) -> str:
        ref = self.refs.get(message_id)
        if ref is None:
            return _esc(label) if label is not None else f"#{message_id}"
        shown = label if label is not None and not re.fullmatch(r"#?\d+|сообщение|ссылка", label.strip(), re.I) \
            else ref.quote
        return _a(ref.url, _esc(shown)) if ref.url else _esc(shown)

    def _link(self, label: str, target: str) -> str:
        if target.startswith("#"):
            return self._ref(int(target[1:]), label)
        own = self._own(target)
        if own is not None:
            return self._ref(own, label)
        if self.internal is not None and _TME_LINK.fullmatch(target):
            return _esc(label)      # a link into another chat: the label alone
        host = _host(target)
        if host is None:
            return self.inline(label, links=False)     # an address that could mislead: the label alone
        shown = self.inline(label, links=False)
        link = _a(target, shown)
        return link if _names_host(to_plain(shown), host) else f"{link} ({_esc(host)})"

    def inline(self, text: str, *, links: bool = True) -> str:
        out: list[str] = []
        pos = 0
        for m in _INLINE.finditer(text):
            out.append(_plain(text[pos:m.start()]))
            pos = m.end()
            kind = next(k for k in _KINDS if m.group(k) is not None)
            value = m.group(kind)
            if kind == "code":
                out.append(f"<code>{_esc(value)}</code>")
            elif kind == "label":
                out.append(self._link(value, m.group("target")) if links else _esc(value))
            elif kind == "badlabel":     # javascript:, tg:// and the like: the label alone
                out.append(_esc(value))
            elif kind == "url":
                bare = value.rstrip(_TRAIL)
                own = self._own(bare) if links else None
                if own is not None and own in self.refs:
                    out.append(self._ref(own, None) + _esc(value[len(bare):]))
                else:
                    out.append(_esc(value))
            elif kind == "ref":
                ref = self.refs.get(int(value))
                out.append(self._ref(int(value), None) if links and ref is not None and ref.bare else _esc(m.group(0)))
            elif kind == "spoiler" and not links:     # inside a link label: nothing may be hidden
                out.append(self.inline(value, links=False))
            elif kind == "bolditalic":
                out.append(f"<b><i>{self.inline(value, links=links)}</i></b>")
            else:
                tag = {"bold": "b", "strike": "s", "spoiler": "tg-spoiler", "italic": "i"}[kind]
                out.append(f"<{tag}>{self.inline(value, links=links)}</{tag}>")
        out.append(_plain(text[pos:]))
        return "".join(out)


# ---- blocks ----

_FENCE = re.compile(r"^\s*```")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(\S.*)$")      # closing #s are stripped in code: no backtracking
_BULLET = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")


def _one_line_block(line: str) -> bool:
    """```ls -la```: a block that opens and closes on one line."""
    rest = line.strip()[3:]
    return rest.endswith("```") and len(rest) > 3


def _cells(line: str) -> list[str]:
    """Table cells: a «|» inside ||spoilers||, `code` or a link does not split."""
    keep = [m.span() for m in _INLINE.finditer(line)]
    cells, start = [], 0
    for i, ch in enumerate(line):
        if ch == "|" and not any(s <= i < e for s, e in keep):
            cells.append(line[start:i])
            start = i + 1
    cells.append(line[start:])
    return [c.strip() for c in cells]


def _ends_in_code(text: str) -> bool:
    inside = False
    for line in text.split("\n"):
        if _FENCE.match(line) and (inside or not _one_line_block(line)):
            inside = not inside
    return inside


def _pre(lines: list[str]) -> str:
    body = "\n".join(lines).strip("\n")
    return f"<pre>{_esc(body)}</pre>" if body.strip() else ""


def to_html(text: str, refs: dict[int, Ref] | None = None, chat_id: int | None = None) -> str:
    """Markdown from the model -> Telegram HTML."""
    r = _Renderer(refs, chat_id)
    text = _BIDI.sub("", text or "")
    text = _REF_AND_LINK.sub(lambda m: f"[#{m.group('id')}](#{m.group('id')})" if m.group("id") == m.group("message")
                             and m.group("internal") == r.internal else m.group(0), text or "")
    out: list[str] = []
    quote_lines: list[str] = []
    code: list[str] | None = None

    def flush_quote() -> None:
        if quote_lines:
            tag = "blockquote expandable" if len(quote_lines) > 4 else "blockquote"
            out.append(f"<{tag}>" + "\n".join(quote_lines) + "</blockquote>")
            quote_lines.clear()

    for line in text.split("\n"):
        if code is not None:
            if _FENCE.match(line):
                out.append(_pre(code))
                code = None
            else:
                code.append(line)
            continue
        if _FENCE.match(line):
            flush_quote()
            if _one_line_block(line):
                out.append(_pre([line.strip()[3:-3].strip()]))
            else:
                code = []
            continue
        m = _QUOTE.match(line)
        if m:
            quote_lines.append(r.inline(m.group(1)))
            continue
        flush_quote()
        if _RULE.match(line) or _TABLE_SEPARATOR.match(line) and "|" in line:
            out.append("")
            continue
        if _TABLE_ROW.match(line):
            out.append(r.inline(" — ".join(c for c in _cells(line) if c)))
            continue
        m = _HEADING.match(line)
        if m:
            title = m.group(1).rstrip()
            bare = title.rstrip("#")
            if bare != title and (not bare or bare[-1].isspace()):     # «## Итог ##», but not «C#»
                title = bare
            title = title.replace("**", "").strip()
            out.append(f"<b>{r.inline(title)}</b>")
            continue
        m = _BULLET.match(line)
        if m:
            depth = min(len(m.group(1).expandtabs(4)) // 2, 3)
            out.append("  " * depth + ("•" if depth == 0 else "◦") + " " + r.inline(m.group(2)))
            continue
        out.append(r.inline(line))
    if code is not None:
        out.append(_pre(code))
    flush_quote()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def to_plain(html_text: str) -> str:
    """What the reader sees of a Telegram HTML text: tags dropped, entities decoded."""
    return html.unescape(re.sub(r"<[^>]*>", "", html_text))


def visible_len(html_text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units, after entity parsing)."""
    return len(to_plain(html_text).encode("utf-16-le")) // 2


def _split_markdown(text: str, limit: int) -> list[str]:
    """split_text that keeps code blocks whole in each part: a block cut in two is closed
    at the end of one part and opened again at the start of the next."""
    parts = split_text(text, limit)
    for i in range(len(parts) - 1):
        if _ends_in_code(parts[i]):
            parts[i] += "\n```"
            parts[i + 1] = "```\n" + parts[i + 1]
    return parts


def render(text: str, refs: dict[int, Ref] | None = None, chat_id: int | None = None,
           limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Telegram HTML parts of a Markdown answer, each within Telegram's length limit."""
    parts: list[str] = []
    pending = _split_markdown(text, limit)
    while pending:
        part = pending.pop(0)
        out = to_html(part, refs, chat_id)
        if visible_len(out) > limit and len(part) > 1:
            pending[:0] = _split_markdown(part, max(len(part) // 2, 1))
            continue
        if out:
            parts.append(out)
    return parts
