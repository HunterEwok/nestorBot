"""python -m nestor.news --check: the owner's remote probe of the live sources, here with fake
adapters (no network, no database)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nestor.news import __main__ as cli
from nestor.news.http import HttpError
from nestor.news.model import FetchResult, Item

from .news_helpers import FakeHttp

WHEN = datetime(2026, 10, 2, 15, 14, tzinfo=timezone.utc)


def _post(post_id: str, title: str, **meta) -> Item:
    return Item(ext_id=post_id, url=f"https://www.reddit.com/r/Psychonaut/comments/{post_id}/", title=title,
                published_at=WHEN, meta={"subreddit": "Psychonaut", "num_comments": 3, **meta})


ITEMS = {
    "arctic": [
        _post("a1", "Psilocybin trial results", score=60, is_self=False, link="https://www.nature.com/articles/x",
              domain="nature.com", removed=False),
        _post("a2", "Integration after a hard trip", score=12, is_self=True, link=None, domain="self.Psychonaut",
              removed=True, removed_by="reddit"),
        _post("a3", "A paper on ego dissolution", score=2, is_self=False, link="https://www.nature.com/articles/y",
              domain="nature.com", removed=False),
    ],
    "ctgov": [Item(ext_id="NCT05222222:2026-10-02", url="https://clinicaltrials.gov/study/NCT05222222",
                   title="MDMA-Assisted Therapy for PTSD", published_at=WHEN,
                   meta={"nct": "NCT05222222", "status": "ACTIVE_NOT_RECRUITING", "change": "update"})],
}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("NEWS_TELEGRAM_CHANNELS", "psychedelicsocietyvienna")
    monkeypatch.setattr(cli, "Http", lambda contact: FakeHttp(contact=contact))

    async def adapter(http, source, cursor, now):
        assert cursor == {}   # --check has no history
        if source.kind == "rss":
            raise HttpError(403, source.url)
        return FetchResult(list(ITEMS.get(source.kind, ())))
    monkeypatch.setattr(cli, "ADAPTERS", {k: adapter for k in cli.ADAPTERS})


async def test_default_output_is_unchanged(fake, capsys):
    assert await cli.check(["reddit_psychonaut", "hightimes"]) == 1
    out = capsys.readouterr().out.splitlines()
    newest = WHEN.astimezone().strftime("%Y-%m-%d %H:%M")
    assert out == ["✗ hightimes: HTTP 403",     # in registry order
                   f"✓ reddit_psychonaut: записей 3, проходят фильтр 1, самая новая {newest}; "
                   "отброшено: removed 1, low_score 1",
                   "    Psilocybin trial results",
                   "", "Источников: 2, с ошибкой: 1"]


async def test_verbose_shows_every_entry_with_its_verdict(fake, capsys):
    assert await cli.check(["reddit_psychonaut", "ctgov"], verbose=True) == 0
    out = capsys.readouterr().out.splitlines()
    when = WHEN.astimezone().strftime("%Y-%m-%d %H:%M")
    assert out[0].startswith("✓ ctgov: записей 1, проходят фильтр 0")
    assert out[1] == "    (здесь нет истории статусов: смена статуса показана как update)"
    assert out[2] == f"    minor_update {when}  MDMA-Assisted Therapy for PTSD  [update, ACTIVE_NOT_RECRUITING]"
    assert out[3].startswith("✓ reddit_psychonaut: записей 3, проходят фильтр 1")
    assert out[4:7] == [
        f"    ok           {when}  Psilocybin trial results  [score 60, comments 3; link; domain nature.com]",
        f"    removed      {when}  Integration after a hard trip  "
        "[score 12, comments 3; self; removed_by reddit; domain self.psychonaut]",
        f"    low_score    {when}  A paper on ego dissolution  [score 2, comments 3; link; domain nature.com]",
    ]


async def test_verbose_shows_the_archive_second_look(fake, capsys, monkeypatch):
    monkeypatch.setitem(ITEMS, "arctic", [
        _post("b1", "Integration after a hard trip", score=91, is_self=True, link=None, domain="self.Psychonaut",
              removed=False, removed_by="reddit", restored=True, rechecked=True),
        _post("b2", "Retreat spots left", score=12, is_self=True, link=None, domain="self.Psychonaut",
              removed=True, removed_by="moderator", removed_later=True, rechecked=True),
        _post("b3", "Psilocybin trial results", score=1, is_self=False, link="https://www.nature.com/articles/x",
              domain="nature.com", removed=False, rechecked=False, unsettled=True),
        _post("b4", "Text of the first look", score=30, is_self=True, link=None, domain="self.Psychonaut",
              removed=True, removed_later=True, rechecked=True),
    ])
    assert await cli.check(["reddit_psychonaut"], verbose=True) == 0
    out = capsys.readouterr().out.splitlines()
    when = WHEN.astimezone().strftime("%Y-%m-%d %H:%M")
    assert out[0].endswith("отброшено: removed 2, self_post 1, unsettled 1")
    assert out[1:5] == [
        f"    self_post    {when}  Integration after a hard trip  "
        "[score 91, comments 3; self; removed_by reddit, approved later; domain self.psychonaut]",
        f"    removed      {when}  Retreat spots left  "
        "[score 12, comments 3; self; removed_by moderator, removed later; domain self.psychonaut]",
        f"    unsettled    {when}  Psilocybin trial results  "
        "[score 1, comments 3; link; not rechecked; domain nature.com]",
        f"    removed      {when}  Text of the first look  [score 30, comments 3; self; removed later; "
        "domain self.psychonaut]",
    ]


async def test_unknown_source(fake, capsys):
    assert await cli.check(["nope"], verbose=True) == 2
    assert "Нет таких источников: nope" in capsys.readouterr().out


@pytest.mark.parametrize("argv, check, verbose, sources", [
    (["--check"], True, False, []),
    (["--check", "pubmed", "ctgov"], True, False, ["pubmed", "ctgov"]),
    (["--check", "-v", "reddit_psychonaut"], True, True, ["reddit_psychonaut"]),
    (["--check", "pubmed", "--verbose", "ctgov"], True, True, ["pubmed", "ctgov"]),
    ([], False, False, []),
])
def test_arguments(argv, check, verbose, sources, capsys):
    args = cli.parse_args(argv)
    assert (args.check, args.verbose, args.sources) == (check, verbose, sources)
    assert ("usage:" in capsys.readouterr().out) is not check   # without --check: the help
