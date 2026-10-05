"""Is a message addressed to the bot (FR-10)? Pure functions over Bot API dicts."""
from __future__ import annotations

import re
from dataclasses import dataclass

# /status (the watchdog's view, nestor.bot.answerer._status) is for the admins of the alert group only:
# it is in neither HELP nor the BotFather list.
COMMANDS = {"help", "privacy", "forget_me", "summary", "stats", "pause", "resume", "bot_word", "start",
            "catchup", "export", "reminders", "unremind", "digest", "wisdom", "news", "reactions", "address",
            "chime", "status"}


@dataclass(frozen=True)
class BotIdentity:
    user_id: int
    username: str            # without @
    keywords: tuple[str, ...]  # e.g. ("нестор", "бот"); the generic "бот" can be disabled per chat


@dataclass(frozen=True)
class Invocation:
    kind: str                # mention | reply | keyword | command | address (nestor.bot.addressing)
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


# Words that speak to someone: «ты» and its forms, requests (the owner's list, and what /help asks
# the bot to do), «как думаешь». With /address on they tell an address from talk about the bot.
_SPEAKS_TO = re.compile(
    r"(?<!\w)(?:ты|тебя|тебе|тобой|тобою|твой|твоя|тво[её]|твои|твоего|твоей|твоему|твоим|твоих|твоими|твою|"
    r"скажи|подскажи|расскажи|объясни|помоги|напомни|найди|глянь|посмотри|как\s+думаешь|знаешь|можешь|слышишь|"
    r"слушай|оцени|проверь|включи|подумай|перескажи)(?!\w)", re.IGNORECASE)
# A mark right after the name: «нестор, ...», «нестор: ...», «нестор — ...», «нестор?».
MARK = re.compile(r"[,:;!?.…—–-]")
# A question by its first word, without its «?»: «нестор что я пропустил», «нестор кто прав», «нестор как дела».
# Not «что-то», «когда-то», nor «как всегда», «как и все»: «нестор что-то тупит» is about the bot.
_ASKS = re.compile(r"(?:что|чего|кто|кого|кому|где|куда|откуда|когда|сколько|почему|зачем|"
                   r"как(?:ой|ая|ое|ие|ую|ого|ому|им|их)|как(?![\w-])(?!\s+(?:всегда|обычно|будто|и|раз|бы|правило)"
                   r"(?!\w)))(?![\w-])", re.IGNORECASE)
# A web address, without its trailing punctuation: a «?si=» inside it is not a question.
URL = re.compile(r"(?:https?://|www\.|(?<![\w.])[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}/)\S+?(?=[.,;:!?)»\"']*(?:\s|$))")


def speaks_to(text: str) -> bool:
    """Whether the text speaks to someone: a question, «ты», a request. Links do not count."""
    text = URL.sub("", text)
    return "?" in text or _SPEAKS_TO.search(text) is not None


# «помолчи», «замолчи уже», «хватит», «не встревай», «ну помолчи, пожалуйста», «да помолчи ты», «помолчи
# чуть-чуть»: a quiet word, and nothing but these soft words around it (nestor.bot.chime). Anything else is
# a request like any other.
_SOFT = r"(?:а|ну|да|же|уже|вс[её]|ты|ладно|давай|пожалуйста|плиз|плз|пж|пока|сегодня|немного|чуть|чуточку|до|завтра)"
_QUIET = re.compile(rf"(?:{_SOFT} )*(помолчи|замолчи|хватит|не встревай)((?: {_SOFT})*)")


def quiet(text: str) -> bool:
    """Whether the text, the bot's name taken out, asks it to keep quiet. Only «хватит?» with nothing after
    the word is a question (is it enough?); «помолчи?», «замолчи?», «хватит уже, а?» ask the bot all the same."""
    m = _QUIET.fullmatch(" ".join(re.sub(r"[^\w\s]", " ", text.lower()).split()))
    return m is not None and not (m.group(1) == "хватит" and not m.group(2) and "?" in text)


def name_forms(keywords: tuple[str, ...]) -> str:
    """A regex of the bot's names in any case form: нестор, нестора, нестору, нестором, несторе,
    несторушка, несторчик; бот, бота, боту, ботом, боте. A name that ends in a vowel stays as it is."""
    forms = []
    for k in keywords:
        word = re.escape(k)
        if re.search(r"[бвгджзклмнпрстфхцчшщ]$", k, re.IGNORECASE):
            word += r"(?:а|у|ом|е|ушк(?:а|и|е|у|ой|о)|чик(?:а|у|ом|е)?)?"
        forms.append(word)
    return "|".join(forms)


def name_regex(keywords: tuple[str, ...]) -> str:
    """A regex of the bot's names (name_forms) as words of their own: not the end of «чат-бот» or
    «телеграм-бота». «нестор-то», «Нестор-летописец» are the name: the model tells them apart."""
    return rf"(?<![\w-])(?:{name_forms(keywords)})(?!\w)"


def detect(msg: dict, me: BotIdentity, *, generic_keyword_enabled: bool = True,
           addressing: bool = False) -> Invocation | None:
    """An explicit call: a command, an @mention, a reply to the bot, or the name at the start.
    addressing (/address on): the name at the start counts only when a comma or another mark comes
    after it, the rest speaks to someone (a «?», «ты», a request), asks something by its first word
    («нестор кто прав») or asks it to keep quiet, or there is no rest («нестор», «нестор 👋»);
    «нестор вчера тупил» and «нестор-то вчера тупил» are left to nestor.bot.addressing, the model decides."""
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
            rest, mark = text[km.end():], km.group()
            # keyword_pattern takes «,:;!.—-» after the name with it; «?», «…» and «–» stay in the rest.
            # A hyphen joined to the next word is part of it, not a mark: «нестор-то», «бот-помощник».
            if re.search(r"\w-$", mark) and re.match(r"\w", rest):
                mark = mark[:-1]
            # The bare name, maybe with an emoji or a bracket after it, calls the bot as it always did.
            if not addressing or MARK.search(mark) or MARK.match(rest) or speaks_to(rest) or _ASKS.match(rest) \
                    or quiet(rest) or not re.search(r"\w", rest):
                return Invocation("keyword", rest.strip())
    return None
