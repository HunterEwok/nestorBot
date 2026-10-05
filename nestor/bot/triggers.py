"""Is a message addressed to the bot (FR-10)? Pure functions over Bot API dicts."""
from __future__ import annotations

import re
from dataclasses import dataclass

# /status (the watchdog's view, nestor.bot.answerer._status) is for the admins of the alert group only:
# it is in neither HELP nor the BotFather list.
COMMANDS = {"help", "privacy", "forget_me", "summary", "stats", "pause", "resume", "bot_word", "start",
            "catchup", "export", "reminders", "unremind", "digest", "wisdom", "news", "reactions", "status"}


@dataclass(frozen=True)
class BotIdentity:
    user_id: int
    username: str            # without @
    keywords: tuple[str, ...]  # e.g. ("нестор", "бот"); the generic "бот" can be disabled per chat


@dataclass(frozen=True)
class Invocation:
    kind: str                # mention | reply | keyword | command
    text: str                # the request with the trigger stripped
    command: str | None = None
    args: str = ""
    replied_to_bot: int | None = None  # message_id of the bot's message being replied to


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Telegram entity offsets count UTF-16 code units."""
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le")


def _strip_span(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    rest = (raw[:offset * 2] + raw[(offset + length) * 2:]).decode("utf-16-le")
    return re.sub(r"\s+", " ", rest).strip(" ,:;—-")


def keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern:
    words = "|".join(re.escape(k) for k in keywords)
    return re.compile(rf"^\s*(?:{words})(?![\w])[\s,:;!.—-]*", re.IGNORECASE)


def detect(msg: dict, me: BotIdentity, *, generic_keyword_enabled: bool = True) -> Invocation | None:
    text = msg.get("text") or msg.get("caption") or ""
    entities = msg.get("entities") or msg.get("caption_entities") or []

    # Commands: /help or /help@this_bot; a command addressed to another bot is not ours.
    m = re.match(r"^/([a-z_]+)(?:@(\w+))?(?:\s+(.*))?$", text.strip(), re.DOTALL | re.IGNORECASE)
    if m and m.group(1).lower() in COMMANDS:
        target = m.group(2)
        if target is None or target.lower() == me.username.lower():
            return Invocation("command", (m.group(3) or "").strip(), m.group(1).lower(), (m.group(3) or "").strip())
        return None

    for e in entities:
        if e.get("type") == "mention":
            if _utf16_slice(text, e["offset"], e["length"]).lstrip("@").lower() == me.username.lower():
                return Invocation("mention", _strip_span(text, e["offset"], e["length"]))
        elif e.get("type") == "text_mention" and (e.get("user") or {}).get("id") == me.user_id:
            return Invocation("mention", _strip_span(text, e["offset"], e["length"]))

    reply = msg.get("reply_to_message") or {}
    topic_root = msg.get("is_topic_message") and reply.get("message_id") == msg.get("message_thread_id")
    if (reply.get("from") or {}).get("id") == me.user_id and not topic_root:
        return Invocation("reply", text.strip(), replied_to_bot=reply["message_id"])

    keywords = me.keywords if generic_keyword_enabled else tuple(k for k in me.keywords if k != "бот")
    if keywords:
        km = keyword_pattern(keywords).match(text)
        if km:
            return Invocation("keyword", text[km.end():].strip())
    return None
