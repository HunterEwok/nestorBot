"""Which Hermes profile does which task.

A profile is fixed by what it can reach and by its model (set in its config.yaml):
- group: the archive of the asking chat, a cheap model (Haiku);
- deep:  the same archive tools, a bigger model (Sonnet or Opus); optional;
- web:   internet search, no archive, a cheap model;
- smart: internet search, no archive, a bigger model; optional;
- news:  no tools at all, a cheap model, for sorting public news items; optional.

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
BIG = frozenset({"deep", "smart"})

# task -> (profiles it may use, default profile)
TASKS: dict[str, tuple[tuple[str, ...], str]] = {
    "answer": (ARCHIVE, "group"),        # search, summaries, «что я пропустил», other questions
    "theses": (ARCHIVE, "deep"),         # «оцени тезисы»
    "who_is_right": (ARCHIVE, "deep"),   # «кто прав»
    "think": (ARCHIVE, "deep"),          # «нестор, подумай …» about the chat
    "digest": (ARCHIVE, "group"),        # the morning digest of the chat
    "topics": (ARCHIVE, "group"),        # topics of the week in /stats, themes for the wisdom of the day
    "web": (WEB, "web"),                 # questions about the world
    "think_web": (WEB, "smart"),         # «нестор, подумай …» about the world
    "factcheck": (WEB, "web"),           # «нестор, проверь» by reply
    "music": (WEB, "smart"),
    "wisdom": (WEB, "smart"),
    "news_triage": (PUBLIC, "news"),     # one line per news item
    "news": (PUBLIC, "smart"),           # the evening news digest
}
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
        """The configured profile for a task, or its fallback; None if none of them is set up."""
        allowed, default = TASKS[task]
        wanted = self.routes.get(task, default)
        for name in (wanted, *FALLBACK.get(wanted, ())):
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
