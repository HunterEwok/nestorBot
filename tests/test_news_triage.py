"""The triage prompt: what the cheap model sees of each new item. No database, no network."""
import json

from nestor.news import triage
from nestor.news.model import Source
from nestor.news.sources import telegram_source

MM = Source("mm", "Marijuana Moment", "rss", "https://mm.example/feed/", "policy", every_h=1)
TG = telegram_source("psychedelicsocietyvienna")
SOURCES = {s.key: s for s in (MM, TG)}

HEAD = "Integration circle notes from Vienna, thank you all for coming. " * 20
TAIL = "Bestellung per PN, Versand nach ganz Österreich, 30 € pro Packung"


def row(source: str, summary: str, n: int = 1) -> dict:
    return {"id": 100 + n, "source": source, "url": "https://x/", "title": f"t{n}", "summary": summary, "lang": "",
            "section": "community", "meta": {}}


def texts(rows: list[dict]) -> list[str]:
    messages = triage.build_prompt(rows, SOURCES)
    lines = messages[1]["content"].removeprefix("Записи:\n").splitlines()
    return [json.loads(line)["text"] for line in lines]


def test_long_community_posts_show_their_tail():
    summary = HEAD + TAIL
    text, = texts([row(TG.key, summary)])
    assert TAIL in text and text.startswith(summary[:400]) and text.endswith(summary[-200:])
    assert " … " in text and len(text) <= triage.TEXT_CHARS + 3


def test_other_posts_show_their_head():
    summary = HEAD + TAIL
    curated, short, unknown, empty = texts([row(MM.key, summary, 1), row(TG.key, "Lecture on Thursday", 2),
                                            row("gone", summary, 3), {**row(TG.key, "", 4), "summary": None}])
    assert curated == unknown == summary[:triage.TEXT_CHARS] and TAIL not in curated
    assert short == "Lecture on Thursday" and empty == ""
