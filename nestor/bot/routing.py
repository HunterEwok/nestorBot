"""Which Hermes profile does which task.

A profile is fixed by what it can reach and by its model (set in its config.yaml):
- group: the archive of the asking chat, the main model (Sonnet);
- deep:  the same archive tools, a bigger model (Opus); optional;
- web:   internet search, no archive, the main model (Sonnet);
- smart: internet search, no archive, a bigger model (Opus); optional;
- news:  no tools at all, the main model (Sonnet), for sorting public news items, for picking
         reactions to chat messages, for telling whether a message is addressed to the bot
         and for the bot's own rare remarks (nothing to search with, nothing to read); optional.

Tasks that read the archive may only go to group/deep, tasks that search the internet only
to web/smart: that split is what keeps chat text out of search queries, so a route that
breaks it is refused at start. The owner can move a task between the profiles it is allowed
on with MODEL_ROUTES, e.g. "theses:group,music:web". A missing optional profile falls back
to its cheap sibling.
"""
from __future__ import annotations

import re

from .llm import LLM

ARCHIVE = ("group", "deep")
WEB = ("web", "smart")
PUBLIC = ("news", "web", "smart")   # public news items only: any profile without the archive
NO_WEB = ("news", "group")          # chat text without the internet: reactions, address checks, remarks
BIG = frozenset({"deep", "smart"})

# task -> (profiles it may use, default profile)
TASKS: dict[str, tuple[tuple[str, ...], str]] = {
    "answer": (ARCHIVE, "group"),        # search, summaries, «что я пропустил», other questions
    "theses": (ARCHIVE, "deep"),         # «оцени тезисы»
    "who_is_right": (ARCHIVE, "deep"),   # «кто прав»
    "think": (ARCHIVE, "deep"),          # «нестор, подумай …» about the chat
    "digest": (ARCHIVE, "group"),        # the digest of the chat, and its notes on parts of a long period
    "topics": (ARCHIVE, "group"),        # topics of the week in /stats, themes for the wisdom of the day
    "web": (WEB, "web"),                 # questions about the world
    "think_web": (WEB, "smart"),         # «нестор, подумай …» about the world
    "factcheck": (WEB, "web"),           # «нестор, проверь» by reply
    "music": (WEB, "smart"),
    "wisdom": (WEB, "smart"),
    "news_triage": (PUBLIC, "news"),     # one line per news item
    "news": (PUBLIC, "smart"),           # the evening news digest
    "music_check": (PUBLIC, "news"),     # unused since the viral song of the day; kept so old MODEL_ROUTES still load
    "music_trends": (WEB, "smart"),      # the viral song of the day: why it spread, where to listen (/music)
    "react": (NO_WEB, "news"),           # reactions to emotional messages; no `news` profile: the code decides
    "address": (NO_WEB, "news"),         # is a doubtful message addressed to the bot; no `news` profile: not asked
    "chime": (NO_WEB, "news"),           # the bot's own remarks (/chime on); no `news` profile: none
}
# Tasks the code can do without (or decide itself): they never move to another profile.
NO_FALLBACK = frozenset({"react", "address", "chime"})
FALLBACK = {"deep": ("group",), "smart": ("web",), "news": ("web", "smart"), "group": (), "web": ()}


def parse_routes(value: str) -> dict[str, str]:
    """MODEL_ROUTES: "task:profile" pairs separated by commas."""
    routes: dict[str, str] = {}
    for pair in value.replace(" ", "").split(","):
        if not pair:
            continue
        task, _, profile = pair.partition(":")
        if task not in TASKS:
            raise ValueError(f"unknown task {task!r}; tasks: {', '.join(TASKS)}")
        allowed = TASKS[task][0]
        if profile not in allowed:
            raise ValueError(f"task {task!r} may only use the profiles {', '.join(allowed)}, not {profile!r}")
        routes[task] = profile
    return routes


class Router:
    def __init__(self, clients: dict[str, LLM | None], routes: dict[str, str] | None = None):
        self.clients = {name: c for name, c in clients.items() if c is not None}
        self.routes = routes or {}

    def profile(self, task: str) -> str | None:
        """The configured profile for a task, or its fallback; None if none of them is set up.
        Reactions, address checks and remarks never fall back: without their profile the code picks
        reactions for free, a message that is not clearly addressed to the bot stays unanswered, and
        the bot makes no remarks."""
        allowed, default = TASKS[task]
        wanted = self.routes.get(task, default)
        for name in (wanted, *(FALLBACK.get(wanted, ()) if task not in NO_FALLBACK else ())):
            if name in allowed and name in self.clients:
                return name
        return None

    def llm(self, task: str) -> LLM | None:
        name = self.profile(task)
        return self.clients[name] if name else None

    def is_big(self, task: str) -> bool:
        return self.profile(task) in BIG


_THINK = re.compile(r"^\s*(?:подумай\s+(?:хорошенько|как\s+следует|получше)|подумайте|подумай|поразмысли)(?!\w)"
                    r"[\s,:;.!—-]*", re.IGNORECASE)


def split_think(text: str) -> tuple[bool, str]:
    """«нестор, подумай, кто прав» -> (True, "кто прав"): the asker wants the bigger model.
    A bare «подумай хорошенько» (a reply to an answer) keeps its words: they are the question."""
    m = _THINK.match(text or "")
    if not m:
        return False, text
    return True, text[m.end():].strip() or text.strip()
