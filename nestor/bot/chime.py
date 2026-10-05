"""The bot's own remarks, the owner's «И2» of 2026-10-05.

Where an admin turned on /chime, once an hour between CHIME_HOURS the last hour of the group goes
to the profile without tools, numbered and without names (nestor.bot.addressing.render). It
mostly stays silent; now and then it answers one person's message with a short remark: a question
nobody answered, a dispute about a checkable fact, a light joke, a useful fact. The code picks the
hours, counts the remarks and checks what the model chose (Answerer.run_chimes): a number it was
shown, a person's own message of the last hour (not a forward) that the model read whole (text, not
cut, nor a reply to a cut one), that is neither a request to the bot nor about it, not of the person
the previous remark answered, a short text without links. Anything else is silence.
«нестор, помолчи» silences the remarks until the end of the day; the bot's answers are not affected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dtime

from .. import forget
from .llm import sole_json
from .triggers import BotIdentity, name_regex, quiet

MAX_CHARS = 300         # a remark is one or two sentences
MIN_CUT = 20            # an overlong remark cut shorter than this is not worth saying


@dataclass(frozen=True)
class ChimeConfig:
    hours: tuple[dtime, dtime] = (dtime(10), dtime(23))   # CHIME_HOURS, local time: from the first, before the second
    min_messages: int = 10      # CHIME_MIN_MESSAGES: people's messages in the last hour, at least
    daily: int = 3              # CHIME_DAILY: remarks per chat per day
    window: int = 40            # CHIME_WINDOW: the last messages of the hour the model reads
    every_min: int = 60         # one look per chat this often, at this many minutes back
    timeout_s: float = 60       # a look that takes longer is silence


def in_hours(at: dtime, hours: tuple[dtime, dtime]) -> bool:
    """Whether a local time is within CHIME_HOURS: 10:00 is, 23:00 is not."""
    return hours[0] <= at < hours[1]


def asks_quiet(text: str | None, me: BotIdentity) -> bool:
    """Whether a request to the bot asks it to keep quiet («нестор, помолчи», «замолчи уже», «хватит»,
    «не встревай», with or without the name): then no remarks until the end of the day."""
    return quiet(re.sub(name_regex(me.keywords), " ", (text or "").lower()))


def names_bot(text: str | None, me: BotIdentity) -> bool:
    """Whether a message names the bot anywhere, in any case form or by its @username: a call, which
    gets an answer anyway, or talk about it, which the bot leaves alone. A remark answers neither."""
    return re.search(rf"{name_regex(me.keywords)}|(?<!\w)@{re.escape(me.username)}(?!\w)", text or "",
                     re.IGNORECASE) is not None


# A web address in any form: https://…, www.…, t.me/…, example.com, сайт.рф.
LINK = re.compile(r"https?://|www\.|(?<![\w.])[\w-]+(?:\.[\w-]+)*\.(?:[a-z]{2,}|рф)(?!\w)", re.IGNORECASE)


def trim(text: str, limit: int = MAX_CHARS) -> str | None:
    """The remark in one line, at most `limit` characters: whole, or up to the end of its last sentence
    that fits. None when it is empty or no sensible cut fits."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text or None
    ends = [m.end() for m in re.finditer(r"[.!?…]+(?=\s)", text[:limit + 1])]
    return text[:ends[-1]] if ends and ends[-1] >= MIN_CUT else None


@dataclass(frozen=True)
class Pick:
    n: int                  # the number of the message to answer, as the model was shown it
    text: str


# What the model reads besides the words (nestor.bot.addressing.render) means nothing in the chat: the labels
# instead of names («Аноним 2»), of the forum's topics («[раздел 2]»), the numbers of the messages («3. …», «(ответ
# на 3)»). The model is told never to mention them; a remark that does anyway, in any form, is not said: the
# labels' stems and «сообщени…», or a number marked as one («#3», «№3», «ответ на 3»). Now and then a fine
# remark goes too («по сообщениям ТАСС»): silence is the default anyway.
_SHOWN = re.compile(r"аноним|раздел|сообщени|[#№]\s*\d|ответ\s+на\s+\d", re.IGNORECASE)
# «Нестор: …», as the model reads its own.
_SIGNED = re.compile(r"^\s*нестор\s*(?::|—|–|-(?!\w))\s*", re.IGNORECASE)


def parse(answer: str | None, count: int) -> Pick | None:
    """{"reply_to": N, "text": "…"} -> Pick. {"reply_to": null}, a number that was not shown, an empty
    text, one too long to cut, a link, a label («Аноним 2 прав») or a message's number, an answer
    with words around its object or with two objects (an object in it may be quoted from the chat),
    anything else: None, the bot stays silent. A «Нестор:» before the text is dropped."""
    data = sole_json(answer)
    if not isinstance(data, dict):
        return None
    n, text = data.get("reply_to"), data.get("text")
    if type(n) is not int or not 1 <= n <= count or not isinstance(text, str):
        return None
    text = trim(_SIGNED.sub("", text))
    if text is None or LINK.search(text) or _SHOWN.search(text):
        return None
    return Pick(n, text)


def targetable(row, *, bot_id: int, previous: list[int] | None, now: datetime, max_age: timedelta) -> bool:
    """Whether a remark may answer this archive row (context.messages_by_id), read back after the model
    chose it (a deleted message, or one whose author used /forget_me meanwhile, is not found at all): a
    person's own words, not a bot's, a channel's post, an admin's as the group or a forward (a remark «на
    ты» would take someone else's words for theirs), at most max_age old, and not of the person the chat's
    previous remark answered."""
    return (row is not None and row["user_id"] is not None and row["user_id"] != bot_id and not row["is_bot"]
            and row["sender_chat_id"] is None and not row["forwarded"] and row["user_id"] not in forget.SHARED_IDS
            and row["user_id"] not in (previous or ()) and now - row["sent_at"] <= max_age)


def read_whole(row, shown: list, cut: int) -> bool:
    """Whether the model read all of the message and of the shown ones it replies to, one to another: text,
    at most `cut` characters of each (a longer one is cut, «…[обрезано]»). A voice message, a sticker, a
    photo are only their kind to it; and «ого, вот это поворот» under a long story reacts to how it ends."""
    by_id, seen = {r["message_id"]: r for r in shown}, set()
    while row is not None and row["message_id"] not in seen:
        if row["kind"] != "text" or not (row["text"] or "").strip() or len(row["text"]) > cut:
            return False
        seen.add(row["message_id"])
        row = by_id.get(row["reply_to_id"])
    return True
