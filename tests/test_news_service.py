"""The news service when things go wrong: text the database refuses, a save that fails, a stage
of the loop that breaks, a triage model that answers with prose."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import asyncpg

from nestor.news import store
from nestor.news.model import FetchResult

from .test_news_pipeline import NOW, FakeAdapter, ScriptedLLM, item, make_service


async def test_nul_and_lone_surrogates_are_cleaned_before_saving(pool, conn):
    adapter = FakeAdapter()
    service = make_service(pool, adapter)
    # what a cut JSON escape or a stray NUL in an abstract looks like after parsing
    adapter.results = {"pubmed": FetchResult([item(
        "1\x00", "Psilocybin\x00 for depression: a randomized trial \ud83d", 1, url="https://example.org/1\x00",
        summary="Abstract\x00 text \udc00", doi="10.1/x\x00", lang="en\x00", categories=("Trial\x00",),
        meta={"authors": ["Smith\x00", {"name": "\ud83dDoe 😀"}], "pubtypes\x00": ["Clinical Trial"]})])}
    assert (await service.collect(NOW))["pubmed"] == "ok"
    row = await conn.fetchrow("SELECT ext_id, url, title, summary, doi, lang, categories, meta, status FROM news_items")
    assert dict(row) == {
        "ext_id": "1", "url": "https://example.org/1", "title": "Psilocybin for depression: a randomized trial �",
        "summary": "Abstract text �", "doi": "10.1/x", "lang": "en", "categories": ["Trial"],
        "meta": {"authors": ["Smith", {"name": "�Doe 😀"}], "pubtypes": ["Clinical Trial"]}, "status": "new"}


async def test_model_text_the_database_refuses_is_cleaned(pool, conn):
    def keep_broken(messages):   # a cut escape and a NUL escape in the JSON, and a NUL after it
        n = messages[-1]["content"].count('{"n": ')
        items = ", ".join(f'{{"n": {i}, "keep": true, "gist": "суть \\ud83d\\u0000{i}"}}' for i in range(1, n + 1))
        return '{"items": [' + items + "]}\x00"

    adapter = FakeAdapter()
    service = make_service(pool, adapter, ScriptedLLM(keep_broken))
    await service.collect(NOW - timedelta(hours=7))   # seeds; every source is due again at NOW
    adapter.results = {
        "mm": FetchResult([item("a", "Senate approves cannabis rescheduling", 1),
                           item("b", "Governor signs psychedelic therapy law", 1)]),
        "pubmed": FetchResult([item("1", "Psilocybin for depression: a randomized trial", 1)]),
    }
    await service.collect(datetime.now(timezone.utc))
    assert await service.triage(NOW, force=True) == 3
    assert {r["gist"] for r in await conn.fetch("SELECT gist FROM news_items")} == {"суть �1", "суть �2", "суть �3"}
    plan = ('{"items": [{"ids": [1], "section": "main", "text": "Сенат \\ud83d\\u0000одобрил перенос каннабиса."},'
            ' {"ids": [2, 3], "section": "research", "text": "Две темы вместе."}]}')
    text = await service.digest(date(2026, 10, 4), ScriptedLLM(plan, profile="smart"), max_topics=10, timeout_s=5)
    assert "Сенат �одобрил перенос каннабиса." in text and "\x00" not in text
    # the triage answer is logged too, so its tokens count toward the daily limit
    logged = [tuple(r) for r in await conn.fetch("SELECT scenario, status FROM bot_requests ORDER BY id")]
    assert logged == [("news_triage", "ok"), ("news", "ok")]


async def test_a_failure_quoting_a_broken_server_message_is_still_saved(pool, conn):
    adapter = FakeAdapter()
    service = make_service(pool, adapter)
    # the adapters quote errMsg and the like from the server's JSON in their ValueError
    adapter.results = {"mm": ValueError("europepmc: bad\x00 query \ud800"),
                       "norml": FetchResult([item("b", "Governor signs psychedelic therapy law", 1)])}
    result = await service.collect(NOW)
    assert result["norml"] == "ok"
    st = await store.states(conn)
    assert st["mm"]["status"] == "error: europepmc: bad query �" and st["mm"]["failures"] == 1
    assert st["mm"]["next_run_at"] == NOW + timedelta(minutes=15)
    assert "mm" not in await service.collect(NOW + timedelta(minutes=5))   # no refetch every tick


async def test_a_failed_save_backs_off_that_source_and_spares_the_others(pool, conn, monkeypatch):
    adapter = FakeAdapter()
    service = make_service(pool, adapter)
    adapter.results = {
        "mm": FetchResult([item("a", "Senate votes on cannabis rescheduling bill", 1)], cursor={"page": 2}),
        "norml": FetchResult([item("b", "Governor signs psychedelic therapy law", 1)]),
        "pubmed": FetchResult([item("1", "Psilocybin for depression: a randomized trial", 1)]),
    }
    save_item = store.save_item

    async def refused(conn, source, it, status, reason=None):
        if source.key == "mm":
            raise asyncpg.exceptions.CharacterNotInRepertoireError("invalid byte sequence for encoding UTF8")
        return await save_item(conn, source, it, status, reason)

    monkeypatch.setattr(store, "save_item", refused)
    result = await service.collect(NOW)
    assert result["mm"] == "error: CharacterNotInRepertoireError" and result["norml"] == result["pubmed"] == "ok"
    st = await store.states(conn)
    assert st["mm"]["failures"] == 1 and st["mm"]["next_run_at"] == NOW + timedelta(minutes=15)
    assert not st["mm"]["seeded"] and st["mm"]["cursor"] == {}   # the retry starts from the same place
    assert {r["ext_id"] for r in await conn.fetch("SELECT ext_id FROM news_items")} == {"b", "1"}
    assert "mm" not in await service.collect(NOW + timedelta(minutes=5))   # no refetch every tick


async def test_a_failing_stage_does_not_skip_the_others():
    service = make_service(None, FakeAdapter(), tick_s=0.01)
    ran, done = [], asyncio.Event()

    async def collect(now):
        ran.append("collect")
        raise RuntimeError("a bug")

    async def triage(now):
        ran.append("triage")
        raise ConnectionRefusedError()   # the database is down

    async def cleanup(now):
        ran.append("cleanup")
        done.set()

    service.collect, service.triage, service.cleanup = collect, triage, cleanup
    task = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(done.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)   # the loop stops on cancel, it does not swallow it
    assert ran[:3] == ["collect", "triage", "cleanup"]


async def test_a_prose_triage_answer_waits_for_the_next_pass(pool, conn):
    adapter = FakeAdapter()
    llm = ScriptedLLM("Извините, я не могу помочь с этим запросом.")
    service = make_service(pool, adapter, llm)
    await service.collect(NOW)   # seed
    adapter.results = {"mm": FetchResult([item(f"x{i}", f"Cannabis policy story number {i:02d} today", 1)
                                          for i in range(20)])}
    await service.collect(NOW + timedelta(hours=1))

    async def counts():
        return {tuple(r) for r in await conn.fetch(
            "SELECT status, drop_reason, triage_attempts, count(*) FROM news_items GROUP BY 1, 2, 3")}

    assert await service.triage(NOW + timedelta(hours=1)) == 0
    assert len(llm.calls) == 1 and await counts() == {("new", None, 1, 15), ("new", None, 0, 5)}
    await service.triage(NOW + timedelta(hours=1))
    await service.triage(NOW + timedelta(hours=1))
    assert len(llm.calls) == 3 and await counts() == {("rejected", "triage_failed", 3, 15), ("new", None, 0, 5)}
