"""Reactions to emotional messages, the owner's choice of 2026-10-04: a hybrid.

Code finds the candidates: laughter, joy, success, admiration, surprise, sadness, strong
emoji. About a minute after the first one, the chat's candidates go to the profile without
tools in one call, numbered and without names; it answers with a reaction per message or
none. Without that profile, past the daily number of calls, or when the day's tokens run low,
the code's own guess is used, and only where it is sure (laughter, emoji, congratulations).

Telegram lets a bot put one reaction on a message, from the standard set below, and only
those the chat allows. Bots cannot install reaction packs: custom emoji reactions exist only
if the group's admins turn them on with boosts, and the bot does not use them.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import ReactionTypeEmoji

from . import prompts
from .llm import LLM, Completion, extract_json
from .triggers import speaks_to

log = logging.getLogger("nestor.reactions")

# Bot API, ReactionTypeEmoji: the reactions any bot may set (without U+FE0F).
STANDARD = frozenset({
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊",
    "🤡", "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓",
    "🍾", "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗",
    "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾",
    "🤷‍♂", "🤷", "🤷‍♀", "😡",
})
# What Nestor puts: nothing rude or mocking, nothing that judges a person (👎, 🤡, 💩, 🥱),
# nothing flirty, and no 💊 in chats that talk about psychedelics.
ALLOWED = (
    "👍", "❤", "🔥", "🥰", "👏", "😁", "🤣", "🤔", "🤯", "😱", "😢", "😭", "🎉", "🤩", "🙏", "👌", "🕊", "😍",
    "❤‍🔥", "💯", "⚡", "🏆", "💔", "🤨", "🍾", "🤓", "👀", "😇", "😨", "🤝", "🤗", "🫡", "🤪", "🗿", "🆒",
    "😎", "🙈", "🦄", "🤷", "✍", "🎄", "🎅", "☃",
)


def normalize(emoji: str) -> str:
    """Models and clients add U+FE0F (❤️); Telegram's list has none."""
    return (emoji or "").replace("️", "").strip()


# (reaction, pattern, sure): a sure guess may be put without the model.
_RULES: tuple[tuple[str | None, re.Pattern, bool], ...] = tuple((emoji, re.compile(pattern, re.I), sure) for
                                                                 emoji, pattern, sure in (
    # Laughter is a whole word of laughter letters: «ахаха», «хехе», «ха-ха», not «страхах» or «хахаль».
    ("🤣", r"\b(?=[ах]{4,}\b)[ах]*?(?:хах|аха)|\b(?=[хе]{4,}\b)[хе]*?(?:хех|ехе)|\b(?=[хи]{4,}\b)[хи]*?(?:хих|ихи)|"
           r"\b(?=[ha]{4,}\b)[ha]*?(?:hah|aha)|\bх[аеи](?:[\s-]+х[аеи])+\b|"
           r"\bлол\b|\blol\b|\bржу\b|\blmao\b|😂|🤣|😆|😹|\bxd\b", True),
    ("😁", r"\){3,}|😄|😃|😀|😁", True),
    # «не поздравляйте меня, отец в больнице», «с др. стороны»: only the plain forms are sure.
    ("🎉", r"\bура+\b|(?<!не )\bпоздравля(?:ю|ем)\b|\bс\s+дн[её]м\s+рождени|🎉|🥳|🎊", True),
    ("🏆", r"\bпобедил|\bвыиграл|\bчемпион", False),
    ("👏", r"\bполучилось\b|\bсдал[аи]?\b|\bнаконец-то\b|\bгоржусь|\bмолод(?:ец|цы)\b", False),
    ("❤", r"❤|😍|🥰|💕|💖", True),
    ("🔥", r"\bогонь\b|\bофигенн|\bшикарн|\bпушка\b|\bкайф|🔥", False),
    ("😢", r"\bгрустно\b|\bпечальн|\bпечаль\b|\bжаль\b|\bобидно\b|😢|😭|😞|😔|\({3,}", False),
    ("🙏", r"\bсоболезн|\bумер(?:ла|ли)?\b|\bскончал|\bпохорон|царствие\s+небесное|покойся\s+с\s+миром", False),
    ("😱", r"\bофигеть\b|\bохренеть\b|ничего\s+себе|ни\s*фига\s+себе|\bвау\b|\bwow\b|\bомг\b|\bomg\b|\bкапец\b|"
           r"\bжесть\b|😱|😨|🤯|😮", False),
    ("🤔", r"🤔|\bхм+\b", False),
    # Words that only the model can read right: «ору» is laughter or shouting, «люблю» may be «не люблю».
    ("🤣", r"\bору\b|\bугар(?:но)?\b", False),
    ("❤", r"\bлюблю\b|\bобожаю\b", False),
    ("🎉", r"поздравля|\bс\s+др\b", False),
    (None, r"!{3,}|[?!]{4,}", False),       # strong, but which feeling: only the model can tell
))


@dataclass(frozen=True)
class Guess:
    emoji: str | None
    sure: bool


def guess(text: str | None) -> Guess | None:
    """Whether a message looks emotional, and the code's own pick; None if it does not."""
    for emoji, pattern, sure in _RULES:
        if pattern.search(text or ""):
            return Guess(emoji, sure and emoji is not None)
    return None


def eligible(msg: dict) -> bool:
    """A person's own words: no bots, channels, anonymous admins, forwards or commands."""
    sender = msg.get("from") or {}
    text = msg.get("text") or msg.get("caption") or ""
    return bool(text.strip()) and not sender.get("is_bot") and "sender_chat" not in msg \
        and "forward_origin" not in msg and "via_bot" not in msg and not text.lstrip().startswith("/")


# ---- «спасибо» to the bot: a reaction instead of an answer ----

_THANK = r"спасиб\w*|спс|пасиб\w*|благодар(?:ю|ствую|ен|на|ны)|сенкс|мерси|thx|thanks|thank\s+you"
_THANKS = (rf"(?:тебе\s+|вам\s+)?(?:большое\s+|огромное\s+)?(?:{_THANK}|того\s+же|взаимно)"
           r"(?:\s+(?:большое|огромное|тебе|вам|бро|друг|дружище|нестор|бот))*")
_PRAISE = r"(?:ты\s+)?(?:лучший|лучшая|молодец|красавчик|умница)"      # «ты лучший», «спасибо, ты лучший»
_ACK = r"(?:ок|окей|ok|okay|понял|поняла|ясно|понятно|принято|угу|ага|точно|хорошо|ладно)"
# A thanks or a praise, maybe after an «ок» or two («понял, спасибо», «ок спс»); an «ок» alone («ясно, понял»).
_THANKS_SAID = re.compile(rf"(?:{_ACK}\s+)*(?:{_THANKS}(?:\s+{_PRAISE})?|{_PRAISE}(?:\s+{_THANKS})?)")
_ACK_SAID = re.compile(rf"{_ACK}(?:\s+{_ACK})*")
_GOING_ON = re.compile(r"^(?:(?:а|и|ну)\s+)+")     # «а, понятно», «ну ок», «и тебе спасибо»
# A thanks among other words (thanks()): a word of thanks, the words of thanks around it, and the words after
# which more is said: «спасибо, а на завтра», «спасибо, но это не так».
_THANK_WORD = re.compile(rf"(?<!\w)(?:{_THANK})(?!\w)")
_THANKS_IN = re.compile(rf"(?<!\w)(?:{_THANKS})(?:\s+{_PRAISE})?(?!\w)")
_MORE = re.compile(r"(?<!\w)(?:а|и|но)(?!\w)")


# Friendly emoji outside the reactions list: answered with 👌.
_FRIENDLY = ("🙂", "😊", "☺", "😉", "👋", "✅", "✔", "💪", "🫶", "😀", "😃", "😄", "😆")


def acknowledgement(text: str | None) -> str | None:
    """The reaction for a bare «спасибо», «ок» (also «а, понятно», «и тебе спасибо», «а понял спасибо»,
    «ты лучший») or a friendly emoji sent to the bot; None for anything else (a question, «❓», «👎»):
    that goes to the model."""
    raw = normalize(text or "")
    if raw and not re.search(r"[\w?¿‽？❓❔⁉]", raw):
        if re.fullmatch(r"[\s)]+", raw):       # «)))»: a smile
            return "😁"
        mirrored = next((e for e in ALLOWED if raw.startswith(e)), None)
        if mirrored is not None:
            return mirrored
        return "👌" if raw.startswith(_FRIENDLY) else None
    words = _GOING_ON.sub("", re.sub(r"\s+", " ", re.sub(r"[^\w\s?-]", " ", raw)).strip().lower())
    if _THANKS_SAID.fullmatch(words):
        return "❤"
    if _ACK_SAID.fullmatch(words):
        return "👌"
    return None


def thanks(text: str | None) -> bool:
    """A thanks among other words that ask nothing more: «о, спасибо», «супер, спасибо!», «спасибо, выручил»,
    «спасибочки», «огромное тебе спасибо, ты лучший»; not «спасибо, а на завтра», «спасибо, но это не так»,
    «спасибо, расскажи ещё» or «спасибо, ты уверен?». For a message to the bot in own words
    (nestor.bot.addressing): nothing to answer. A call by name gets a reaction only for the bare ones
    (acknowledgement): with more words it may still ask something."""
    words = _GOING_ON.sub("", " ".join(re.sub(r"[^\w\s?-]", " ", normalize(text or "").lower()).split()))
    rest = _THANKS_IN.sub(" ", words)
    return _THANK_WORD.search(words) is not None and not speaks_to(rest) and not _MORE.search(rest)


# ---- the model's answer ----

def parse(answer: str | None, count: int, allowed: frozenset[str]) -> dict[int, str]:
    """{"reactions": [{"n": 1, "emoji": "😁"}]} -> {1: "😁"}; anything else is dropped."""
    data = extract_json(answer)
    items = data.get("reactions") if isinstance(data, dict) else None
    picks: dict[int, str] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        n, emoji = item.get("n"), item.get("emoji")
        if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= count and isinstance(emoji, str):
            emoji = normalize(emoji)
            if emoji in allowed and n not in picks:
                picks[n] = emoji
    return picks


@dataclass(frozen=True)
class ReactionConfig:
    delay_s: float = 60            # a batch waits this long for more candidates
    batch: int = 10                # messages per model call, the newest kept
    per_hour: int = 6              # reactions per chat per hour
    calls_per_day: int = 30        # model calls per chat per day
    max_age_s: float = 900         # older messages are left alone
    cache_s: float = 6 * 3600      # how long a chat's allowed reactions are remembered


@dataclass
class _Candidate:
    message_id: int
    guess: Guess
    sent_at: int                   # unix time


Wanted = Callable[[int], Awaitable[bool]]
CallsToday = Callable[[int], Awaitable[int]]
MaySpend = Callable[[], Awaitable[bool]]
LogCall = Callable[[int, Completion, float, str], Awaitable[None]]


class Reactor:
    """Collects candidates per chat and puts the reactions."""

    def __init__(self, bot: Bot, pool: asyncpg.Pool, llm: Callable[[], LLM | None], *, wanted: Wanted,
                 calls_today: CallsToday, may_spend: MaySpend, log_call: LogCall,
                 cfg: ReactionConfig = ReactionConfig()):
        self.bot, self.pool, self.llm, self.cfg = bot, pool, llm, cfg
        self.wanted, self.calls_today, self.may_spend, self.log_call = wanted, calls_today, may_spend, log_call
        self._pending: dict[int, list[_Candidate]] = {}
        self._timers: dict[int, asyncio.Task] = {}
        self._done: dict[int, deque[float]] = {}                       # monotonic times of reactions
        self._available: dict[int, tuple[float, frozenset[str]]] = {}

    def consider(self, msg: dict) -> bool:
        """A new stored group message: queue it if it looks emotional."""
        if not eligible(msg):
            return False
        found = guess(msg.get("text") or msg.get("caption"))
        if found is None:
            return False
        chat_id = msg["chat"]["id"]
        queue = self._pending.setdefault(chat_id, [])
        queue.append(_Candidate(msg["message_id"], found, int(msg.get("date") or time.time())))
        del queue[:-self.cfg.batch]
        if chat_id not in self._timers or self._timers[chat_id].done():
            self._timers[chat_id] = asyncio.create_task(self._flush_later(chat_id))
        return True

    async def drain(self) -> None:
        """Wait for the batches already queued (tests, shutdown)."""
        while timers := [t for t in self._timers.values() if not t.done()]:
            await asyncio.gather(*timers, return_exceptions=True)

    async def _flush_later(self, chat_id: int) -> None:
        await asyncio.sleep(self.cfg.delay_s)
        try:
            await self.flush(chat_id)
        except Exception:  # noqa: BLE001 - a reaction is never worth a crash
            log.exception("reactions failed", extra={"chat_id": chat_id})
        finally:
            # Candidates that came while this batch was being decided wait for the next one.
            if self._pending.get(chat_id):
                self._timers[chat_id] = asyncio.create_task(self._flush_later(chat_id))

    async def flush(self, chat_id: int) -> int:
        """Decide and put this chat's reactions; returns how many were put."""
        queue = [c for c in self._pending.pop(chat_id, []) if time.time() - c.sent_at <= self.cfg.max_age_s]
        room = self._room(chat_id)
        if not queue or room <= 0 or not await self.wanted(chat_id):
            return 0
        texts = await self._texts(chat_id, [c.message_id for c in queue])
        queue = [c for c in queue if c.message_id in texts]
        allowed = await self.allowed(chat_id)
        if not queue or not allowed:
            return 0
        picks = await self._ask(chat_id, queue, texts, allowed, room)
        if picks is None:   # no model now: only the sure guesses
            picks = {i: c.guess.emoji for i, c in enumerate(queue, 1)
                     if c.guess.sure and c.guess.emoji in allowed}
        if not picks:
            return 0
        # The model may take a while: a /pause, /reactions off or an author's /forget_me
        # meanwhile still counts.
        if not await self.wanted(chat_id):
            return 0
        still = await self._texts(chat_id, [queue[i - 1].message_id for i in picks])
        put = 0
        for i, emoji in sorted(picks.items())[:room]:
            if queue[i - 1].message_id in still and await self._put(chat_id, queue[i - 1].message_id, emoji):
                put += 1
        return put

    def _room(self, chat_id: int) -> int:
        done = self._done.setdefault(chat_id, deque())
        while done and time.monotonic() - done[0] > 3600:
            done.popleft()
        return self.cfg.per_hour - len(done)

    async def _texts(self, chat_id: int, ids: list[int]) -> dict[int, str]:
        """Read back from the archive: deleted messages and authors who used /forget_me drop out."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT m.message_id, m.text FROM messages m LEFT JOIN users u ON u.user_id = m.user_id
                   WHERE m.chat_id = $1 AND m.message_id = ANY($2::bigint[]) AND coalesce(m.text, '') <> ''
                     AND (u.user_id IS NULL OR NOT u.opted_out)""", chat_id, ids)
        return {r["message_id"]: r["text"] for r in rows}

    async def allowed(self, chat_id: int) -> frozenset[str]:
        """ALLOWED as far as the chat permits (ChatFullInfo.available_reactions; absent = all)."""
        cached = self._available.get(chat_id)
        if cached and time.monotonic() - cached[0] < self.cfg.cache_s:
            return cached[1]
        everything = frozenset(ALLOWED)
        try:
            chat = await self.bot.get_chat(chat_id)
        except TelegramAPIError as e:
            log.warning("chat reactions unknown", extra={"chat_id": chat_id, "error": type(e).__name__})
            return everything
        available = getattr(chat, "available_reactions", None)
        if available is None:
            allowed = everything
        else:
            allowed = everything & {normalize(r.emoji) for r in available if isinstance(r, ReactionTypeEmoji)}
        self._available[chat_id] = (time.monotonic(), allowed)
        return allowed

    async def _ask(self, chat_id: int, queue: list[_Candidate], texts: dict[int, str], allowed: frozenset[str],
                   room: int) -> dict[int, str] | None:
        """The model's picks, or None when it is not to be asked (then the code decides)."""
        llm = self.llm()
        if llm is None or await self.calls_today(chat_id) >= self.cfg.calls_per_day or not await self.may_spend():
            return None
        listed = "\n".join(f"{i}. {texts[c.message_id][:300].replace(chr(10), ' ⏎ ')}"
                           for i, c in enumerate(queue, 1))
        system = prompts.REACT_SYSTEM.format(limit=room, allowed=" ".join(e for e in ALLOWED if e in allowed))
        started = time.monotonic()
        try:
            completion = await asyncio.wait_for(llm.complete([{"role": "system", "content": system},
                                                              {"role": "user", "content": listed}]), 60)
        except Exception as e:  # noqa: BLE001 - Hermes down: the code's own guesses
            log.warning("reaction model failed", extra={"chat_id": chat_id, "error": type(e).__name__})
            # A failed call counts toward the day's calls too: a broken profile is not asked every minute.
            await self.log_call(chat_id, Completion("", None, None, None), started,
                                "timeout" if isinstance(e, asyncio.TimeoutError) else "error")
            return None
        picks = parse(completion.text, len(queue), allowed)
        await self.log_call(chat_id, completion, started, "ok")
        return picks

    async def _put(self, chat_id: int, message_id: int, emoji: str) -> bool:
        try:
            await self.bot.set_message_reaction(chat_id, message_id, reaction=[ReactionTypeEmoji(emoji=emoji)])
        except TelegramBadRequest as e:
            if "REACTION_INVALID" in str(e):   # not allowed here after all: never try it again in this chat
                at, allowed = self._available.get(chat_id, (time.monotonic(), frozenset(ALLOWED)))
                self._available[chat_id] = (at, allowed - {emoji})
            log.warning("reaction refused", extra={"chat_id": chat_id, "error": type(e).__name__})
            return False
        except TelegramAPIError as e:
            log.warning("reaction failed", extra={"chat_id": chat_id, "error": type(e).__name__})
            return False
        self._done.setdefault(chat_id, deque()).append(time.monotonic())
        return True
