"""The news pipeline without the network: fake adapters feed the service, which stores,
filters, glues stories, triages with a fake model and writes the digest."""
import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from nestor.bot.llm import Completion
from nestor.news import digest, rank, store, triage
from nestor.news.http import HttpError
from nestor.news.model import FetchResult, Item, Source
from nestor.news.service import NewsService, NotEnoughNews

from .news_helpers import FakeHttp

TZ = ZoneInfo("Europe/Tallinn")
NOW = datetime.now(timezone.utc).replace(microsecond=0)    # the database stamps fetched_at with its own now()

MM = Source("mm", "Marijuana Moment", "rss", "https://mm.example/feed/", "policy", every_h=1)
NORML = Source("norml", "NORML", "rss", "https://norml.example/feed/", "policy", every_h=1)
PUB = Source("pubmed", "PubMed", "pubmed", "https://eutils.example/", "research", every_h=6, mode="science")
EPMC = Source("epmc", "Europe PMC", "europepmc", "https://epmc.example/", "research", every_h=6, mode="science")
RU = Source("nplus1", "N+1", "rss", "https://nplus1.example/rss", None, every_h=1, topic_required=True, lang="ru")
TG = Source("tg_x", "Telegram @x", "tme", "https://t.me/s/x", "community", every_h=1, mode="community")
SOURCES = (MM, NORML, PUB, EPMC, RU, TG)


def item(ext_id, title, hours_ago=1.0, **kw):
    kw.setdefault("url", f"https://example.org/{ext_id}")
    return Item(ext_id=ext_id, title=title, published_at=NOW - timedelta(hours=hours_ago), **kw)


class FakeAdapter:
    def __init__(self):
        self.results: dict[str, object] = {}
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, http, source, cursor, now):
        self.calls.append((source.key, dict(cursor)))
        result = self.results.get(source.key, FetchResult([]))
        if isinstance(result, Exception):
            raise result
        return result


class ScriptedLLM:
    """Answers with the next text of a list (or a function of the prompt)."""

    def __init__(self, *answers, profile="news"):
        self.answers, self.calls, self.profile = list(answers), [], profile

    async def complete(self, messages):
        self.calls.append(messages)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            answer = answer(messages)
        if isinstance(answer, Exception):
            raise answer
        return Completion(answer, "claude-test", 500, 100, self.profile)


def keep_all(messages):
    """Triage answer: keep every item, gist = «суть N»."""
    n = messages[-1]["content"].count('{"n": ')
    return json.dumps({"items": [{"n": i, "keep": True, "gist": f"суть {i}", "importance": 3, "section": "research"}
                                 for i in range(1, n + 1)]})


def make_service(pool, adapter, llm=None, **kw):
    async def wanted():
        return True
    return NewsService(pool, SOURCES, triage_llm=llm, is_wanted=wanted, tz=TZ, daily_token_limit=kw.pop("limit", 10**6),
                       http_factory=lambda contact: FakeHttp(contact=contact),
                       adapters={k: adapter for k in ("rss", "pubmed", "europepmc", "tme")}, **kw)


async def statuses(conn):
    return {(r["source"], r["ext_id"]): (r["status"], r["drop_reason"])
            for r in await conn.fetch("SELECT source, ext_id, status, drop_reason FROM news_items")}


async def test_first_run_seeds_then_only_new_items_wait_for_triage(pool, conn):
    adapter = FakeAdapter()
    service = make_service(pool, adapter)
    adapter.results = {
        "mm": FetchResult([item("a", "Senate votes on cannabis rescheduling bill", 2),
                           item("b", "Older cannabis policy story from last week", 72)]),
        "pubmed": FetchResult([item("1", "Psilocybin for depression: a randomized trial", 1, doi="10.1/x")],
                              cursor={"k": 1}),
        "tg_x": FetchResult([item("7", "Psychedelic meetup, Eintritt 5 €", 1, summary="Tickets 5 € at the door")]),
        "nplus1": FetchResult([item("n1", "Новый телескоп увидел далёкую галактику", 1, lang="ru")]),
    }
    assert await service.collect(NOW) == {k: "ok" for k in ("mm", "norml", "pubmed", "epmc", "nplus1", "tg_x")}
    assert await statuses(conn) == {
        ("mm", "a"): ("new", None),
        ("mm", "b"): ("seen", None),           # there before the first fetch
        ("pubmed", "1"): ("new", None),
        ("tg_x", "7"): ("dropped", "trade"),   # kept with its reason for tuning the filter
    }                                          # off-topic N+1 news is not even stored
    st = await store.states(conn)
    assert st["pubmed"]["cursor"] == {"k": 1} and st["pubmed"]["seeded"] and st["pubmed"]["status"] == "ok"
    assert st["mm"]["newest_item_at"] == NOW - timedelta(hours=2)

    # Two hours later: hourly sources are due again, PubMed (every 6 h) is not.
    later = NOW + timedelta(hours=2)
    adapter.calls.clear()
    adapter.results["mm"] = FetchResult([item("a", "Senate votes on cannabis rescheduling bill", 4),
                                         item("c", "Governor signs psychedelic therapy law", -1),
                                         item("d", "Ancient cannabis story resurfaces", 24 * 10)])
    await service.collect(later)
    assert {k for k, _ in adapter.calls} == {"mm", "norml", "nplus1", "tg_x"}
    s = await statuses(conn)
    assert s[("mm", "c")] == ("new", None) and s[("mm", "d")] == ("seen", None)
    assert await conn.fetchval("SELECT count(*) FROM news_items WHERE source = 'mm' AND ext_id = 'a'") == 1


async def test_one_story_from_several_sources(pool, conn):
    adapter = FakeAdapter()
    service = make_service(pool, adapter)
    await service.collect(NOW)   # seed
    adapter.results = {
        "mm": FetchResult([item("a", "Texas lawmakers approve medical cannabis expansion bill", 1)]),
        "norml": FetchResult([item("z", "Texas Lawmakers Approve Medical Cannabis Expansion Bill!", 1)]),
        "pubmed": FetchResult([item("1", "MDMA-assisted therapy for PTSD", 1, doi="10.5/abc"),
                               item("99", "Cannabidiol in social anxiety: trial results", 1, doi="10.9/cbd"),
                               item("100", "Ayahuasca in prolonged grief disorder", 1, doi="10.9/aya")]),
        "epmc": FetchResult([item("PPR1", "MDMA assisted therapy in PTSD (preprint)", 1, doi="10.5/abc"),
                             # preprints that name their journal version, by PMID or by DOI
                             item("PPR2", "CBD for anxiety, a preprint", 1, doi="10.1101/p2",
                                  meta={"published_pmid": "99"}),
                             item("PPR3", "Grief and ayahuasca, a preprint", 1, doi="10.1101/p3",
                                  meta={"published_doi": "10.9/aya"})]),
    }
    await service.collect(NOW + timedelta(hours=7))
    rows = {r["ext_id"]: r["story_id"] for r in await conn.fetch("SELECT ext_id, story_id FROM news_items")}
    assert rows["a"] == rows["z"] and rows["1"] == rows["PPR1"] and rows["a"] != rows["1"]
    assert rows["99"] == rows["PPR2"] and rows["100"] == rows["PPR3"] and len({rows["1"], rows["99"], rows["100"]}) == 3


async def test_errors_back_off_and_respect_retry_after(pool, conn):
    adapter = FakeAdapter()
    service = make_service(pool, adapter)
    adapter.results = {"mm": HttpError(429, "https://mm.example/feed/", retry_after=7200),
                       "norml": ValueError("layout changed")}
    result = await service.collect(NOW)
    assert result["mm"] == "http_429" and result["norml"] == "error: layout changed"
    st = await store.states(conn)
    assert st["mm"]["next_run_at"] == NOW + timedelta(hours=2) and st["mm"]["failures"] == 1
    assert not st["mm"]["seeded"] and st["mm"]["last_ok_at"] is None
    assert "mm" not in await service.collect(NOW + timedelta(hours=1))
    adapter.results["mm"] = FetchResult([item("a", "Cannabis bill passes the House", 1)])
    assert (await service.collect(NOW + timedelta(hours=2, minutes=1)))["mm"] == "ok"
    st = await store.states(conn)
    assert st["mm"]["failures"] == 0 and st["mm"]["next_run_at"] is None and st["mm"]["seeded"]


async def test_a_refusing_host_is_asked_once_a_day(pool, conn):
    adapter = FakeAdapter()
    service = make_service(pool, adapter)
    adapter.results = {"mm": HttpError(403, "https://mm.example/feed/"), "norml": HttpError(503, "https://norml.example/")}
    t = NOW
    # 15 and 30 minutes as for any error; after the third refusal a day, while an overload keeps every_h (1 h)
    for n, (mm_wait, norml_wait) in enumerate(((15, 15), (30, 30), (24 * 60, 60)), start=1):
        assert await service.collect(t, only=("mm", "norml")) == {"mm": "http_403", "norml": "http_503"}
        st = await store.states(conn)
        assert st["mm"]["failures"] == st["norml"]["failures"] == n
        assert st["mm"]["next_run_at"] == t + timedelta(minutes=mm_wait)
        assert st["norml"]["next_run_at"] == t + timedelta(minutes=norml_wait)
        t += timedelta(minutes=mm_wait)
    assert {"mm", "norml"} <= set(await service.stale(conn, t))
    adapter.results["mm"] = FetchResult([item("a", "Cannabis bill passes the House", 1)])
    assert (await service.collect(t, only=("mm",)))["mm"] == "ok"
    assert (await store.states(conn))["mm"]["failures"] == 0


async def test_one_refusal_among_other_errors_keeps_the_usual_retries(pool, conn):
    # A WAF that answers timeouts and 503s, then one 403: an hour as for any error; only a second 403 in a row waits a day
    adapter = FakeAdapter()
    service = make_service(pool, adapter)
    url = "https://mm.example/feed/"
    t = NOW
    for n, (error, wait) in enumerate(((TimeoutError(), 15), (HttpError(503, url), 30), (HttpError(403, url), 60),
                                       (HttpError(403, url), 24 * 60)), start=1):
        adapter.results = {"mm": error}
        await service.collect(t, only=("mm",))
        st = (await store.states(conn))["mm"]
        assert st["failures"] == n and st["next_run_at"] == t + timedelta(minutes=wait)
        t += timedelta(minutes=wait)


def _arctic_service(pool, http):
    from nestor.news.adapters import arctic
    from nestor.news.sources import SOURCES as REGISTRY
    therapy = next(s for s in REGISTRY if s.key == "reddit_psychedelictherapy")

    async def wanted():
        return True
    return therapy, NewsService(pool, (therapy,), triage_llm=None, is_wanted=wanted, tz=TZ, daily_token_limit=10**6,
                                http_factory=lambda contact: http, adapters={"arctic": arctic.fetch})


def _looks(post_id: str, created: int) -> tuple[dict, dict]:
    """The archive's record of an approved post before and after its second look. In the records of
    2026-10-04 the second look had the text, score 91 and _meta.was_initially_deleted; the first look is
    taken to have had "[removed]" and score 1. The second look came at 36 h, once at 48 h."""
    post = {"id": post_id, "title": "Looking for an integration therapist in Berlin",
            "subreddit": "PsychedelicTherapy", "is_self": True, "created_utc": created, "removed_by_category": "reddit"}
    first = {**post, "selftext": "[removed]", "score": 1, "num_comments": 0}
    second = {**post, "selftext": "Been working through a hard night and want support.", "score": 91,
              "num_comments": 32, "_meta": {"is_edited": True, "removal_type": "reddit",
                                            "retrieved_2nd_on": created + 48 * 3600, "was_initially_deleted": True}}
    return first, second


async def test_a_reddit_post_the_archive_has_not_rechecked_waits_a_day(pool, conn):
    """A post fetched before the archive's second look is not stored; the next day's run asks for it again."""
    from nestor.news.sources import ARCTIC_SHIFT
    day1 = NOW - timedelta(hours=24)
    created = int((day1 - timedelta(hours=40)).timestamp())     # its second look comes 8 h after the day1 run
    first, second = _looks("1wwtest", created)
    answers = [{"data": []}, {"data": [first]}, {"data": [second]}]
    http = FakeHttp([("GET", "^" + re.escape(ARCTIC_SHIFT), lambda call: json.dumps(answers.pop(0)).encode())])
    therapy, service = _arctic_service(pool, http)
    await service.collect(day1 - timedelta(hours=24))        # the first run only seeds
    assert await service.collect(day1) == {therapy.key: "ok"}
    assert await statuses(conn) == {}                         # dropped as unsettled, not stored
    assert (await store.states(conn))[therapy.key]["cursor"] == {"until": created}
    assert await service.collect(NOW) == {therapy.key: "ok"}
    assert http.calls[-1]["params"]["after"] == created - 1
    assert await statuses(conn) == {(therapy.key, "1wwtest"): ("new", None)}


async def test_an_unsettled_reddit_post_of_the_first_run_stays_history(pool, conn):
    from nestor.news.sources import ARCTIC_SHIFT
    created = int((NOW - timedelta(hours=24 + 40)).timestamp())
    first, second = _looks("1wwseed", created)
    answers = [{"data": [first]}, {"data": [second]}]
    http = FakeHttp([("GET", "^" + re.escape(ARCTIC_SHIFT), lambda call: json.dumps(answers.pop(0)).encode())])
    therapy, service = _arctic_service(pool, http)
    await service.collect(NOW - timedelta(hours=24))          # the first run: everything it sees is history
    assert await statuses(conn) == {(therapy.key, "1wwseed"): ("seen", None)}
    await service.collect(NOW)                                # asked for again, settled now, and deduped
    assert http.calls[-1]["params"]["after"] == created - 1
    assert await statuses(conn) == {(therapy.key, "1wwseed"): ("seen", None)}


async def test_stale_sources_are_reported(pool, conn):
    adapter = FakeAdapter()
    service = make_service(pool, adapter)
    adapter.results = {"mm": FetchResult([item("a", "Cannabis bill passes the House", 24 * 20)])}
    await service.collect(NOW)
    assert await service.stale(conn, NOW) == ["mm"]   # max_age_days 14 by default



async def test_a_sparse_feed_is_not_stale_after_two_quiet_months(pool, conn):
    # ScienceDaily's psychedelics topic gets one or two stories a month; 56 days without one was seen live.
    from nestor.news.sources import SOURCES as REGISTRY
    sparse = next(s for s in REGISTRY if s.key == "sciencedaily_psychedelics")
    adapter = FakeAdapter()

    async def wanted():
        return True
    service = NewsService(pool, (sparse,), triage_llm=None, is_wanted=wanted, tz=TZ, daily_token_limit=10**6,
                          http_factory=lambda contact: FakeHttp(contact=contact), adapters={"rss": adapter})
    adapter.results = {sparse.key: FetchResult([item("m", "Magic mushrooms may reshape the brain", 24 * 70)])}
    await service.collect(NOW)
    assert await service.stale(conn, NOW) == []
    assert await service.stale(conn, NOW + timedelta(days=55)) == [sparse.key]


# ---- triage ----

def rows_for(*sections):
    return [{"id": 100 + i, "source": "mm", "url": "https://x/", "title": f"t{i}", "summary": "s", "lang": "en",
             "section": sec, "meta": {}} for i, sec in enumerate(sections)]


def test_triage_parse_validates_everything():
    rows = rows_for("policy", None, "research", None, "policy")
    answer = "Вот ответ:\n```json\n" + json.dumps({"items": [
        {"n": 1, "keep": True, "gist": "Сенат одобрил закон, см. https://evil.example 25 мг", "importance": 9,
         "section": "reggae"},
        {"n": 2, "keep": True, "gist": "Найдено", "importance": "4", "section": "trials"},
        {"n": 3, "keep": False, "reason": "dosing"},
        {"n": 4, "keep": False, "reason": "whatever"},
        {"n": 3, "keep": True, "gist": "дубль"},
        {"n": 9, "keep": True, "gist": "чужой номер"},
        {"n": 5, "keep": "yes"},
    ]}, ensure_ascii=False) + "\n```"
    verdicts = {v.item_id: v for v in triage.parse(answer, rows)}
    assert set(verdicts) == {100, 101, 102, 103}             # item 5 stays in the queue
    first = verdicts[100]
    assert first.keep and first.importance == 5 and first.section is None   # the source decides the section
    assert "http" not in first.gist and "25" not in first.gist and first.gist.startswith("Сенат одобрил закон")
    assert verdicts[101].section == "trials" and verdicts[101].importance == 4
    assert (verdicts[102].keep, verdicts[102].reason) == (False, "dosing")
    assert verdicts[103].reason == "offtopic"
    assert triage.parse("не JSON", rows) == []


async def test_triage_waits_for_a_batch_and_survives_a_dead_model(pool, conn):
    adapter = FakeAdapter()
    llm = ScriptedLLM(keep_all)
    service = make_service(pool, adapter, llm)
    await service.collect(NOW)
    adapter.results = {"mm": FetchResult([item("a", "Senate votes on cannabis rescheduling bill", 1)])}
    await service.collect(NOW + timedelta(hours=1))
    assert await service.triage(NOW + timedelta(hours=1)) == 0 and llm.calls == []   # one item waits for company
    assert await service.triage(NOW + timedelta(hours=5)) == 1                       # ... but not forever
    row = await conn.fetchrow("SELECT status, gist, importance, section FROM news_items WHERE ext_id = 'a'")
    assert tuple(row) == ("kept", "суть 1", 3, "policy")

    adapter.results = {"mm": FetchResult([item("b", "Governor signs psychedelic therapy law", 1)])}
    await service.collect(NOW + timedelta(hours=6))
    service.triage_llm = ScriptedLLM(RuntimeError("hermes down"))
    assert await service.triage(NOW + timedelta(hours=6), force=True) == 0
    assert tuple(await conn.fetchrow("SELECT status, triage_attempts FROM news_items WHERE ext_id = 'b'")) == ("new", 0)
    service.triage_llm = ScriptedLLM('{"items": []}')       # the model skips it three times
    for _ in range(3):
        await service.triage(NOW + timedelta(hours=6), force=True)
    assert tuple(await conn.fetchrow("SELECT status, drop_reason FROM news_items WHERE ext_id = 'b'")) == (
        "rejected", "triage_failed")
    logged = await conn.fetch("SELECT chat_id, scenario, profile, status FROM bot_requests ORDER BY id")
    assert [tuple(r) for r in logged] == [(None, "news_triage", "news", "ok")] + [(None, "news_triage", "news",
                                                                                 "error")] * 3


async def test_triage_stops_at_the_daily_token_limit(pool, conn):
    adapter = FakeAdapter()
    llm = ScriptedLLM(keep_all)
    service = make_service(pool, adapter, llm, limit=500)
    await service.collect(NOW)
    adapter.results = {"mm": FetchResult([item(f"x{i}", f"Cannabis policy story number {i:02d} today", 1)
                                          for i in range(20)])}
    await service.collect(NOW + timedelta(hours=1))
    assert await service.triage(NOW + timedelta(hours=1)) == 15      # the first batch spends the budget
    assert len(llm.calls) == 1
    assert await conn.fetchval("SELECT count(*) FROM news_items WHERE status = 'new'") == 5


# ---- ranking and the digest ----

def cand_row(i, source, section, story, importance=3, meta=None, url=None, hours=1, gist=None):
    return {"id": i, "source": source, "url": url or f"https://{source}.example/{i}", "title": f"Title {i}",
            "summary": "", "doi": None, "meta": meta or {}, "section": section, "gist": gist or f"суть {i}",
            "importance": importance, "published_at": NOW, "fetched_at": NOW - timedelta(hours=hours),
            "story_id": story}


def test_rank_caps_sections_and_rewards_several_outlets():
    by_key = {s.key: s for s in SOURCES}
    rows = [cand_row(i, "pubmed", "research", i, importance=4) for i in range(1, 10)]
    rows += [cand_row(20, "mm", "policy", 20, importance=2), cand_row(21, "norml", "policy", 20, importance=2),
             cand_row(22, "mm", "policy", 22, importance=2, hours=0)]
    rows.append(cand_row(30, "pubmed", "research", 30, importance=4, hours=3,
                         meta={"pubtypes": ["Randomized Controlled Trial"]}))
    picked = rank.pick(rows, by_key, NOW)
    research = [c for c in picked if c.section == "research"]
    assert len(research) == 6 and [c.n for c in picked] == list(range(1, len(picked) + 1))
    policy = [c for c in picked if c.section == "policy"]
    assert [c.story_id for c in policy] == [20, 22]           # two outlets beat a single higher-rated one
    assert {r["id"] for r in policy[0].items} == {20, 21}
    assert research[0].story_id == 30                         # an RCT beats other papers of the same importance


def candidates():
    by_key = {s.key: s for s in SOURCES}
    rows = [cand_row(1, "mm", "policy", 1, importance=5, url="https://mm.example/a?x=1&y=<2>"),
            cand_row(2, "norml", "policy", 1),
            cand_row(3, "pubmed", "research", 3, meta={"pubtypes": ["Randomized Controlled Trial"]}),
            cand_row(4, "tg_x", "community", 4),
            cand_row(5, "mm", "policy", 5, meta={"paid": True}, url="javascript:alert(1)")]
    return rank.pick(rows, by_key, NOW), by_key


def test_digest_parse_and_render():
    cands, by_key = candidates()
    n = {c.story_id: c.n for c in cands}
    answer = json.dumps({"items": [
        {"ids": [n[1]], "section": "main", "text": "Сенат <b>одобрил</b> закон. Подробнее https://evil.example"},
        {"ids": [n[3], 999], "section": "research", "text": "РКИ псилоцибина: 25 мг помогли при депрессии."},
        {"ids": [n[1]], "section": "policy", "text": "повтор той же темы не проходит"},
        {"ids": [n[4]], "section": "community", "text": "Встреча сообщества в Вене."},
        {"ids": [n[5]], "section": "nonsense", "text": "раздел выдуман моделью"},
        {"ids": [n[5]], "section": "policy", "text": "коротко"},
    ], "comment": "Спокойный день."}, ensure_ascii=False)
    plan = digest.parse(answer, cands, max_topics=10)
    assert [(t.ids, t.section) for t in plan.topics] == [((n[1],), "main"), ((n[3],), "research"),
                                                         ((n[4],), "community")]
    text, topics = digest.render(plan, cands, by_key, date(2026, 10, 4))
    assert topics == list(plan.topics)
    assert text.startswith("🗞 <b>Новости дня, 4 октября</b>\n\n<b>Главное за день</b>\n• Сенат &lt;b&gt;одобрил")
    assert "evil.example" not in text and "25 мг" not in text
    assert '<a href="https://mm.example/a?x=1&amp;y=&lt;2&gt;">Marijuana Moment</a>, ' \
           '<a href="https://norml.example/2">NORML</a>' in text
    assert "<b>Исследования</b>\n• РКИ псилоцибина: помогли при депрессии. — " \
           '<a href="https://pubmed.example/3">PubMed</a>' in text
    assert "Telegram @x</a> · пост сообщества" in text
    assert text.endswith("💬 <i>Спокойный день.</i>")
    assert digest.used_item_ids(topics, cands) == [1, 2, 3, 4]
    assert digest.parse('{"items": [{"ids": [1], "section": "main", "text": "x"}]}', cands, 10) is None


def test_digest_marks_single_outlets_and_paid_items_and_fits_one_message():
    cands, by_key = candidates()
    n = {c.story_id: c.n for c in cands}
    plan = digest.Plan((digest.Topic((n[5],), "policy", "Платная аналитика рынка психоделиков."),), None)
    text, _ = digest.render(plan, cands, by_key, date(2026, 10, 4))
    assert "javascript" not in text and text.endswith("Платная аналитика рынка психоделиков. · один источник · платно")
    long = digest.Plan(tuple(digest.Topic((c.n,), c.section, "Очень длинный текст темы. " * 50) for c in cands), None)
    text, topics = digest.render(long, cands, by_key, date(2026, 10, 4))
    assert digest.visible_len(text) <= digest.VISIBLE_LIMIT and 1 <= len(topics) < len(cands)


def test_digest_marks_preprints_with_or_without_the_server_name():
    def marks(meta):
        return digest._markers(rank.Candidate(1, 1, "research", 10, [cand_row(1, "epmc", "research", 1, meta=meta)]))
    assert marks({"preprint": True, "server": "medRxiv"}) == ["препринт medRxiv, без рецензии"]
    assert marks({"preprint": True}) == ["препринт, без рецензии"]
    assert marks({}) == []


@pytest.fixture
async def digest_ready(pool, conn):
    adapter = FakeAdapter()
    service = make_service(pool, adapter, ScriptedLLM(keep_all))
    await service.collect(NOW - timedelta(hours=7))   # seeds; every source is due again at NOW
    adapter.results = {
        "mm": FetchResult([item("a", "Senate approves cannabis rescheduling", 1),
                           item("b", "Governor signs psychedelic therapy law", 1)]),
        "norml": FetchResult([item("z", "Senate Approves Cannabis Rescheduling", 1)]),
        "pubmed": FetchResult([item("1", "Psilocybin for depression: a randomized trial", 1, doi="10.1/x",
                                    meta={"pubtypes": ["Randomized Controlled Trial"]})]),
    }
    await service.collect(datetime.now(timezone.utc))
    return service


async def test_digest_is_written_once_a_day(digest_ready, conn):
    service = digest_ready
    ids = {r["ext_id"]: r["id"] for r in await conn.fetch("SELECT ext_id, id FROM news_items")}

    def plan(messages):
        user = messages[-1]["content"]
        assert "senate approves cannabis rescheduling" in user.lower()
        assert re.search(r"Источники: (Marijuana Moment, NORML|NORML, Marijuana Moment)\.", user)
        return json.dumps({"items": [{"ids": [1], "section": "main", "text": "Сенат одобрил перенос каннабиса."},
                                     {"ids": [2, 3], "section": "research", "text": "Две темы вместе."}],
                           "comment": "День законов."}, ensure_ascii=False)
    smart = ScriptedLLM(plan, profile="smart")
    day = date(2026, 10, 4)
    text = await service.digest(day, smart, max_topics=10, timeout_s=5)
    assert text.startswith("🗞 <b>Новости дня, 4 октября</b>") and "Сенат одобрил перенос каннабиса." in text
    assert await service.digest(day, smart, max_topics=10, timeout_s=5) == text
    assert len(smart.calls) == 1
    used = {r["ext_id"] for r in await conn.fetch("SELECT ext_id FROM news_items WHERE status = 'used'")}
    assert used == {"a", "z", "b", "1"} and set(ids) >= used
    assert await conn.fetchval("SELECT item_ids FROM news_digests WHERE day = $1", day)
    logged = [tuple(r) for r in await conn.fetch("SELECT chat_id, scenario, profile FROM bot_requests ORDER BY id")]
    assert logged == [(None, "news_triage", "news"), (None, "news", "smart")]
    # The next day nothing from these stories comes back.
    with pytest.raises(NotEnoughNews):
        await service.digest(date(2026, 10, 5), smart, max_topics=10, timeout_s=5)


async def test_digest_bad_answer_is_an_error_and_too_little_news_waits(digest_ready, conn):
    service = digest_ready
    from nestor.bot.llm import LLMError

    with pytest.raises(LLMError):
        await service.digest(date(2026, 10, 4), ScriptedLLM("не знаю"), max_topics=10, timeout_s=5)
    assert await conn.fetchval("SELECT count(*) FROM news_digests") == 0
    assert await conn.fetchval("SELECT count(*) FROM news_items WHERE status = 'used'") == 0
    await conn.execute("UPDATE news_items SET status = 'rejected' WHERE ext_id <> 'a'")
    with pytest.raises(NotEnoughNews):
        await service.digest(date(2026, 10, 4), ScriptedLLM("{}"), max_topics=10, timeout_s=5)


async def test_cleanup_keeps_only_identifiers(pool, conn):
    adapter = FakeAdapter()
    service = make_service(pool, adapter)
    adapter.results = {"mm": FetchResult([item("a", "Senate approves cannabis rescheduling", 1)])}
    await service.collect(NOW)
    await conn.execute("UPDATE news_items SET fetched_at = now() - interval '31 days'")
    await service.cleanup(datetime.now(timezone.utc))
    row = await conn.fetchrow("SELECT ext_id, title, url, status FROM news_items")
    assert tuple(row) == ("a", None, None, "seen")
    await conn.execute("UPDATE news_items SET fetched_at = now() - interval '401 days'")
    service._cleaned = None
    await service.cleanup(datetime.now(timezone.utc))
    assert await conn.fetchval("SELECT count(*) FROM news_items") == 0
