"""The cheap model's pass over new items: a one-line Russian gist, importance, a section for
broad sources, and a second opinion on what the code filters missed. Pure functions; the
service makes the calls."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from ..bot import prompts
from ..bot.llm import extract_json
from .filters import clean_text
from .model import SECTIONS, Source

BATCH = 15
TEXT_CHARS = 600
HEAD_CHARS, TAIL_CHARS = 400, 200     # of a long community post: trade and contacts sit at its end
REASONS = ("offtopic", "trade", "dosing", "crime", "ad")


@dataclass(frozen=True)
class Verdict:
    item_id: int
    keep: bool
    section: str | None = None
    gist: str | None = None
    importance: int | None = None
    reason: str | None = None


def build_prompt(rows: list[Mapping[str, Any]], sources: Mapping[str, Source]) -> list[dict]:
    lines = []
    for n, row in enumerate(rows, 1):
        src = sources.get(row["source"])
        text = row["summary"] or ""
        if src is not None and src.mode == "community" and len(text) > TEXT_CHARS:
            text = text[:HEAD_CHARS] + " … " + text[-TAIL_CHARS:]
        else:
            text = text[:TEXT_CHARS]
        entry = {"n": n, "source": src.name if src else row["source"], "section": row["section"] or "?",
                 "title": row["title"] or "", "text": text}
        if row["lang"]:
            entry["lang"] = row["lang"]
        lines.append(json.dumps(entry, ensure_ascii=False))
    return [{"role": "system", "content": prompts.NEWS_TRIAGE_SYSTEM},
            {"role": "user", "content": "Записи:\n" + "\n".join(lines)}]


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse(answer: str, rows: list[Mapping[str, Any]]) -> list[Verdict]:
    """Verdicts for the items the model answered about; anything malformed is left out and
    the item stays in the queue."""
    data = extract_json(answer)
    items = data.get("items") if isinstance(data, dict) else None
    verdicts: dict[int, Verdict] = {}
    for entry in items if isinstance(items, list) else []:
        if not isinstance(entry, dict):
            continue
        n = _int(entry.get("n"))
        if n is None or not 1 <= n <= len(rows) or n in verdicts:
            continue
        row = rows[n - 1]
        keep = entry.get("keep")
        if keep is False:
            reason = entry.get("reason")
            verdicts[n] = Verdict(row["id"], False, reason=reason if reason in REASONS else "offtopic")
            continue
        if keep is not True:
            continue
        gist = clean_text(entry.get("gist")) if isinstance(entry.get("gist"), str) else ""
        if len(gist) > 300:
            gist = gist[:300].rsplit(" ", 1)[0] + "…"
        importance = _int(entry.get("importance"))
        section = None
        if row["section"] is None:
            section = entry.get("section") if entry.get("section") in SECTIONS else "policy"
        verdicts[n] = Verdict(row["id"], True, section=section, gist=gist or None,
                              importance=min(max(importance, 1), 5) if importance is not None else 2)
    return list(verdicts.values())
