"""Model answers in Telegram formatting.

Models write a small Markdown subset (prompts.FORMAT asks for it); this module turns it into
the HTML Telegram accepts with parse_mode=HTML: **bold**, *italic*, ~~strike~~, ||spoiler||,
`code`, ``` blocks, «- » lists, «> » quotes and [text](https://…) links. Anything else stays
literal text, escaped. Every tag comes from one token as a matched pair, so the output is
always well-formed.

References to archive messages ([«quote»](#37285), a bare #37285, or a t.me/c link into the
same chat) become links to the message. The address is built by code from the message id,
never taken from the model; ids the archive does not show (deleted, author used /forget_me)
keep their text without a link. A link to any other site shows its real domain next to it,
so a label cannot hide where it leads.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

TELEGRAM_LIMIT = 4096


@dataclass(frozen=True)
class Ref:
    """An archive message the answer refers to."""
    url: str | None         # None: no t.me link exists (basic groups)
    quote: str              # a short piece of its text, for a bare #id


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """SC-6: split on paragraph, then line, then word boundaries."""
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        cut = max(rest.rfind("\n\n", 0, limit), rest.rfind("\n", 0, limit), rest.rfind(" ", 0, limit))
        if cut <= limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


# ---- references to archive messages ----

_TME = r"https?://t\.me/c/(?P<{0}>\d{{1,15}})/(?:\d{{1,12}}/)?(?P<{1}>\d{{1,12}})"
_TME_LINK = re.compile(_TME.format("internal", "message"))
_BARE_REF = re.compile(r"(?<![\w#&/])#(\d{1,12})(?!\w)")
# «#37285 (https://t.me/c/…/37285)»: the same message twice, keep the id only.
_REF_AND_LINK = re.compile(r"#(?P<id>\d{1,12})[ \t]*[(\[]?[ \t]*" + _TME.format("internal", "message")
                           + r"[ \t]*[)\]]?")
MAX_REFS = 50


def _internal(chat_id: int | None) -> str | None:
    """The chat's id inside t.me/c links: only supergroups have them."""
    if chat_id is None or chat_id > -1000000000000:
        return None
    return str(-chat_id - 1000000000000)


def ref_ids(text: str, chat_id: int | None = None) -> list[int]:
    """Message ids an answer refers to, first seen first (at most MAX_REFS)."""
    internal = _internal(chat_id)
    found = [(m.start(), int(m.group(1))) for m in _BARE_REF.finditer(text or "")]
    if internal is not None:
        found += [(m.start(), int(m.group("message"))) for m in _TME_LINK.finditer(text or "")
                  if m.group("internal") == internal]
    ids: list[int] = []
    for _, value in sorted(found):
        if value not in ids:
            ids.append(value)
    return ids[:MAX_REFS]


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
    r"|(?<![\w#&/])#(?P<ref>\d{1,12})(?!\w)"
    r"|(?<!\w)\*\*\*(?P<bolditalic>[^\s*](?:[^\n]*?[^\s*])?)\*\*\*(?!\w)"
    r"|(?<!\w)\*\*(?P<bold>[^\s*](?:[^\n]*?[^\s*])?)\*\*(?!\w)"
    r"|~~(?P<strike>[^\s~](?:[^\n]*?[^\s~])?)~~"
    r"|\|\|(?P<spoiler>[^\s|](?:[^\n]*?[^\s|])?)\|\|"
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
        host = (urlsplit(target).hostname or "").removeprefix("www.")
        link = _a(target, self.inline(label, links=False))
        if not host or host.lower() in label.lower():
            return link
        return f"{link} ({_esc(host)})"

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
                out.append(self._ref(int(value), None) if links else _esc(m.group(0)))
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
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")


def _pre(lines: list[str]) -> str:
    body = "\n".join(lines).strip("\n")
    return f"<pre>{_esc(body)}</pre>" if body.strip() else ""


def to_html(text: str, refs: dict[int, Ref] | None = None, chat_id: int | None = None) -> str:
    """Markdown from the model -> Telegram HTML."""
    r = _Renderer(refs, chat_id)
    text = _REF_AND_LINK.sub(lambda m: f"#{m.group('id')}" if m.group("id") == m.group("message")
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
            rest = line.strip()[3:]
            if rest.endswith("```") and len(rest) > 3:      # ```one line```
                out.append(_pre([rest[:-3].strip()]))
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
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append(r.inline(" — ".join(c for c in cells if c)))
            continue
        m = _HEADING.match(line)
        if m:
            title = m.group(1).replace("**", "").strip()
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


def render(text: str, refs: dict[int, Ref] | None = None, chat_id: int | None = None,
           limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Telegram HTML parts of a Markdown answer, each within Telegram's length limit."""
    parts: list[str] = []
    pending = split_text(text, limit)
    while pending:
        part = pending.pop(0)
        out = to_html(part, refs, chat_id)
        if visible_len(out) > limit and len(part) > 1:
            pending[:0] = split_text(part, max(len(part) // 2, 1))
            continue
        if out:
            parts.append(out)
    return parts
