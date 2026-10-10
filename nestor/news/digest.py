"""The digest: what the smart model gets, what it may answer, and the message the code builds
from it. The model rates the candidates' significance and writes the strongest one or two;
the code keeps those at or above the owner's threshold. The model refers to candidates by
number; every link comes from stored URLs."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from ..bot import prompts
from ..bot.daily import day_name, is_public_url
from ..bot.llm import extract_json
from .filters import clean_text
from .model import SECTION_TITLES, Source
from .rank import Candidate

VISIBLE_LIMIT = 4000        # Telegram counts 4096 characters after parsing the HTML
MAX_LINKS = 3
MAX_TEXT = 600
_CHECKED = ("research", "trials")   # primary sources: a paper or a registry entry needs no second outlet


@dataclass(frozen=True)
class Topic:
    ids: tuple[int, ...]       # candidate numbers
    score: int                 # significance, 1-10, as the model rated it
    text: str


@dataclass(frozen=True)
class Plan:
    topics: tuple[Topic, ...]  # the strongest first; empty: nothing significant enough today


def _markers(c: Candidate) -> list[str]:
    out = []
    meta = c.items[0]["meta"] or {}
    if meta.get("pubtypes"):
        out.append(", ".join(str(p) for p in meta["pubtypes"][:3]))
    if meta.get("nct"):
        trial = [meta.get("status"), ", ".join(meta.get("phases") or []),
                 {"new": "новое", "results": "есть результаты", "status": "сменился статус"}.get(meta.get("change"))]
        out.append("испытание: " + "; ".join(t for t in trial if t))
    if meta.get("server") or meta.get("preprint"):
        out.append(f"препринт {meta['server']}, без рецензии" if meta.get("server") else "препринт, без рецензии")
    if any((r["meta"] or {}).get("paid") for r in c.items):
        out.append(prompts.NEWS_PAID)
    return out


def build_prompt(candidates: list[Candidate], sources: Mapping[str, Source], day: date,
                 max_topics: int, min_score: int) -> list[dict]:
    blocks = []
    for c in candidates:
        names = _names(c, sources)
        best = c.items[0]
        lines = [f"[{c.n}] Раздел: {SECTION_TITLES[c.section]}. Источники: {', '.join(names)}."]
        marks = _markers(c)
        if marks:
            lines.append("Пометки: " + "; ".join(marks) + ".")
        if best["gist"]:
            lines.append(f"Суть: {best['gist']}")
        lines.append(f"Заголовок: {best['title']}")
        detail = (best["summary"] or "").strip()
        if detail:
            lines.append(f"Подробности: {detail[:400]}")
        for other in c.items[1:3]:
            if other["gist"]:
                lines.append(f"Ещё источник: {other['gist']}")
        blocks.append("\n".join(lines))
    user = f"Сводка за {day_name(day)} {day.year}. Кандидаты:\n\n" + "\n\n".join(blocks)
    return [{"role": "system", "content": prompts.NEWS_SYSTEM.format(max_topics=max_topics, min_score=min_score)},
            {"role": "user", "content": user}]


def _ids(value: Any, known: set[int]) -> tuple[int, ...]:
    if isinstance(value, int) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        return ()
    out: list[int] = []
    for v in value:
        if isinstance(v, int) and not isinstance(v, bool) and v in known and v not in out:
            out.append(v)
    return tuple(out)


def parse(answer: str, candidates: list[Candidate], max_topics: int, min_score: int) -> Plan | None:
    """The topics at or above min_score, strongest first, at most max_topics. None: no usable answer;
    an empty Plan: the model answered, and nothing was significant enough."""
    data = extract_json(answer)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    known = {c.n for c in candidates}
    used: set[int] = set()
    topics: list[Topic] = []
    rated = False
    for entry in data["items"]:
        if not isinstance(entry, dict):
            continue
        ids = tuple(i for i in _ids(entry.get("ids"), known) if i not in used)
        text = clean_text(entry.get("text")) if isinstance(entry.get("text"), str) else ""
        score = entry.get("score")
        if not ids or not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 10:
            continue
        rated = True
        used.update(ids)            # a candidate's first rating holds, a repeat cannot raise it
        if score < min_score or len(text) < 15:
            continue
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT].rsplit(" ", 1)[0] + "…"
        topics.append(Topic(ids, score, text))
    if not rated and data["items"]:
        return None
    topics.sort(key=lambda t: t.score, reverse=True)       # stable: the model's order among equals
    return Plan(tuple(topics[:max_topics]))


def _names(c: Candidate, sources: Mapping[str, Source]) -> list[str]:
    names: list[str] = []
    for r in c.items:
        name = sources[r["source"]].name if r["source"] in sources else r["source"]
        if name not in names:
            names.append(name)
    return names


def _links(cands: list[Candidate], sources: Mapping[str, Source]) -> list[str]:
    links, seen = [], set()
    for c in cands:
        for r in c.items:
            name = sources[r["source"]].name if r["source"] in sources else r["source"]
            if name in seen or not is_public_url(r["url"]):
                continue
            seen.add(name)
            links.append(f'<a href="{html.escape(r["url"], quote=True)}">{html.escape(name)}</a>')
    return links[:MAX_LINKS]


def _line(topic: Topic, by_n: Mapping[int, Candidate], sources: Mapping[str, Source]) -> str:
    cands = [by_n[i] for i in topic.ids]
    tail = ", ".join(_links(cands, sources))
    notes = []
    kinds = {sources[r["source"]].kind if r["source"] in sources else "" for c in cands for r in c.items}
    if kinds <= {"arctic", "tme"}:
        notes.append(prompts.NEWS_COMMUNITY)
    elif cands[0].section not in _CHECKED and len({n for c in cands for n in _names(c, sources)}) == 1:
        notes.append(prompts.NEWS_ONE_SOURCE)
    if any((r["meta"] or {}).get("paid") for c in cands for r in c.items):
        notes.append(prompts.NEWS_PAID)
    line = f"• {html.escape(topic.text)}"
    if tail:
        line += f" — {tail}"
    if notes:
        line += " · " + " · ".join(notes)
    return line


def visible_len(text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def render(plan: Plan, candidates: list[Candidate], sources: Mapping[str, Source],
           day: date) -> tuple[str, list[Topic]]:
    """HTML for Telegram and the topics in it: topics are dropped from the end until it fits
    one message."""
    by_n = {c.n: c for c in candidates}
    topics = list(plan.topics)
    while True:
        parts = [prompts.NEWS_HEADER.format(date=day_name(day))]
        parts += [_line(t, by_n, sources) for t in topics]
        text = "\n\n".join(parts)
        if visible_len(text) <= VISIBLE_LIMIT or len(topics) <= 1:
            return text, topics
        topics.pop()


def used_item_ids(topics: list[Topic], candidates: list[Candidate]) -> list[int]:
    """Every item of the stories that made it in: none of them comes back tomorrow."""
    by_n = {c.n: c for c in candidates}
    return [r["id"] for t in topics for i in t.ids for r in by_n[i].items]
