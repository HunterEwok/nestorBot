"""Rising Russian artists (nestor.trends): the charts' formats, the growth score, the two model calls,
the service, and /music with the weekly post through the real dispatcher."""
import json
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from nestor.bot import music, prompts
from nestor.news.http import HttpError, Response
from nestor.trends import pick, score, sources, store
from nestor.trends.service import NOTHING, NotEnoughMusic, TrendsService
from nestor.trends.sources import Entry

from .factories import CHAT_ID, message, update
from .news_helpers import FakeHttp
from .test_answerer import FakeChecker, make_env_fixture  # noqa: F401 - fixture make_env
from .test_news_pipeline import ScriptedLLM

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)     # a Friday
TODAY = NOW.astimezone(TZ).date()
VIDEO = "dQw4w9WgXcQ"


# ---- the charts' formats ----

async def test_apple_feed_marks_fresh_releases_and_keeps_only_its_own_links():
    body = {"feed": {"results": [
        {"artistName": "Новый Артист", "name": "Песня", "releaseDate": "2026-10-01",
         "url": "https://music.apple.com/ru/album/x/1?i=2"},
        {"artistName": "Старый &amp; Друг", "name": "Хит", "releaseDate": "2019-05-01", "url": "https://evil.example/x"},
        {"artistName": "", "name": "Без артиста"},
    ]}}
    http = FakeHttp([("GET", "marketingtools", json.dumps(body).encode())])
    entries = await sources.apple(http, NOW)
    assert entries == [Entry(1, "Новый Артист", "Песня", is_new=True, url="https://music.apple.com/ru/album/x/1?i=2"),
                       Entry(2, "Старый & Друг", "Хит")]


async def test_yandex_chart_gives_the_move_and_new_entries():
    def track(pos, progress, shift, title, *artists, tid=7):
        return {"track": {"id": tid, "title": title, "artists": [{"name": a} for a in artists]},
                "chart": {"position": pos, "progress": progress, "shift": shift}}
    body = {"result": {"chart": {"tracks": [track(1, "same", 0, "Хит", "Звезда"),
                                            track(2, "up", 40, "Взлёт", "Новичок", "Гость", tid=99),
                                            track(3, "down", 2, "Спад", "Кто-то"),
                                            track(4, "new", 0, "Свежак", "Дебют")]}}}
    entries = await sources.yandex(FakeHttp([("GET", "landing3/chart/russia", json.dumps(body).encode())]), NOW)
    assert [(e.position, e.artist, e.shift, e.is_new) for e in entries] == [
        (1, "Звезда", 0, False), (2, "Новичок, Гость", 40, False), (3, "Кто-то", -2, False), (4, "Дебют", None, True)]
    assert entries[1].url == "https://music.yandex.ru/track/99"
    http = FakeHttp([("GET", "landing3", json.dumps(body).encode())])
    await sources.yandex(http, NOW, sources.Options(yandex_proxy="http://u:p@ru.example:3128"))
    assert http.calls[0]["proxy"] == "http://u:p@ru.example:3128"


async def test_shazam_csv_skips_the_lines_before_the_table():
    body = "Shazam Top 200 Russia\nDate,2026-10-09\n\nRank,Artist,Title\n1,Артист,Песня\n2,\"A, B\",Track\n" \
           "3,урал гайсин &amp; снялцепи,ты в моих мыслях\n"
    entries = await sources.shazam(FakeHttp([("GET", "csv/top-200/russia", body.encode())]), NOW)
    assert [(e.position, e.artist, e.title) for e in entries] == [
        (1, "Артист", "Песня"), (2, "A, B", "Track"), (3, "урал гайсин & снялцепи", "ты в моих мыслях")]


async def test_a_changed_format_is_an_error_of_that_chart():
    http = FakeHttp([("GET", ".", b'{"something": "else"}')])
    for adapter in (sources.apple, sources.yandex):
        try:
            await adapter(http, NOW)
        except ValueError:
            continue
        raise AssertionError(adapter)


async def test_lastfm_needs_its_key_and_reads_listeners():
    body = {"tracks": {"track": [{"name": "Песня", "artist": {"name": "Артист"}, "url": "https://www.last.fm/music/x"}]}}
    http = FakeHttp([("GET", "geo.gettoptracks", json.dumps(body).encode()),
                     ("GET", "artist.getinfo", b'{"artist": {"stats": {"listeners": "12345"}}}')])
    assert [e.title for e in await sources.lastfm(http, NOW, sources.Options(lastfm_key="KEY"))] == ["Песня"]
    assert await sources.lastfm_listeners(http, "KEY", "Артист") == 12345
    try:
        await sources.lastfm(http, NOW)
    except ValueError:
        pass
    else:
        raise AssertionError("no key, no request")


def test_main_artist_and_key():
    assert sources.main_artist("Артист feat. Гость") == "Артист"
    assert sources.main_artist("A, B & C") == "A"
    assert sources.main_artist("AC/DC") == "AC/DC"
    assert sources.artist_key("Ёлка") == sources.artist_key("елка") == "елка"
    assert sources.artist_key("  Mr. Big x Small ") == "mr big"


# ---- the score ----

def snap(day, source, position, artist, title="Песня", shift=None, is_new=False, url=None):
    return {"day": day, "source": source, "position": position, "artist": artist,
            "artist_key": sources.artist_key(artist), "title": title, "shift": shift, "is_new": is_new, "url": url}


def chart(day, source, names, size=100, **marks):
    """A chart with the named artists at their places and filler elsewhere."""
    rows = [snap(day, source, pos, name, **marks.get(name, {})) for name, pos in names.items()]
    taken = set(names.values())
    rows += [snap(day, source, p, f"filler{p}") for p in range(1, size + 1) if p not in taken]
    return rows


def test_first_run_uses_the_charts_own_marks():
    rows = chart(TODAY, "yandex_ru", {"Ракета": 10, "Стоячий": 5, "Дебют": 30},
                 Ракета={"shift": 60}, Стоячий={"shift": 0}, Дебют={"is_new": True})
    found = score.rising(rows, TODAY, {}, NOW)
    keys = [c.key for c in found]
    assert keys[:2] == ["ракета", "дебют"] and "стоячий" not in keys
    assert "поднялся на 60" in found[0].facts[0] and "новинка" in found[1].facts[0]


def test_a_week_of_history_compares_with_a_week_ago_and_old_hits_fade():
    rows = []
    for d in range(28):
        day = TODAY - timedelta(days=27 - d)
        names = {"Старый Хит": 50 if d < 21 else 20}      # in the chart every day, climbs lately
        if d >= 20:
            names["Новичок"] = 90 if d < 21 else 15         # entered a week ago, climbed fast
        rows += chart(day, "yandex_ru", names)
    found = {c.key: c for c in score.rising(rows, TODAY, {}, NOW)}
    assert "90 → 15 место за 7 дн." in found["новичок"].facts[0]
    assert found["новичок"].score > found["старый хит"].score * 2
    assert "в чарте 28 дн. из 28" in found["старый хит"].facts[0]


def test_several_charts_add_up_and_name_the_song_of_the_best_one():
    rows = chart(TODAY, "yandex_ru", {"Новичок": 40}, Новичок={"shift": 10}) \
        + chart(TODAY, "apple_ru", {"Новичок": 5}, Новичок={"is_new": True, "url": "https://music.apple.com/ru/x"})
    for r in rows:
        if r["artist"] == "Новичок" and r["source"] == "apple_ru":
            r["title"] = "Другая"
    [c] = [c for c in score.rising(rows, TODAY, {}, NOW) if c.key == "новичок"]
    assert c.charts == ["yandex_ru", "apple_ru"] and c.title == "Другая" and c.url == "https://music.apple.com/ru/x"
    assert c.score == 10 + (96 // 2 + 15) + score.EXTRA_CHART_BONUS


def test_known_recommended_big_and_foreign_artists_are_left_out():
    rows = chart(TODAY, "yandex_ru", {n: p for p, n in enumerate(["Звезда", "Был", "Большой", "Чужой", "Свежий"], 1)},
                 **{n: {"shift": 50} for n in ["Звезда", "Был", "Большой", "Чужой", "Свежий"]})

    def artist(**kw):
        return {"known": False, "known_reason": None, "recommended_at": None, "listeners": None, "russian": None,
                "checked_at": NOW - timedelta(days=1)} | kw
    artists = {"звезда": artist(known=True, known_reason="model"), "был": artist(recommended_at=NOW - timedelta(days=10)),
               "большой": artist(listeners=5_000_000), "чужой": artist(russian=False),
               "свежий": artist(recommended_at=NOW - timedelta(days=120), listeners=1000)}
    assert [c.key for c in score.rising(rows, TODAY, artists, NOW, max_listeners=300_000)] == ["свежий"]
    # The model's verdicts expire and are asked again; /music known holds for good.
    old = NOW - score.RECHECK - timedelta(days=1)
    artists |= {"звезда": artist(known=True, known_reason="model", checked_at=old),
                "чужой": artist(russian=False, checked_at=old),
                "свежий": artist(known=True, known_reason="command", checked_at=old)}
    assert [c.key for c in score.rising(rows, TODAY, artists, NOW, max_listeners=300_000)] == ["звезда", "чужой"]


def test_a_chart_not_read_lately_is_left_out():
    rows = chart(TODAY - timedelta(days=5), "yandex_ru", {"Ракета": 1}, Ракета={"shift": 90})
    assert score.rising(rows, TODAY, {}, NOW) == []


# ---- the model calls ----

def candidates(*names):
    return [score.Candidate(sources.artist_key(n), n, f"Песня {n}", f"https://music.yandex.ru/track/{i}", 50,
                            [f"Яндекс Музыка: 90 → {i} место за 7 дн."], ["yandex_ru"])
            for i, n in enumerate(names, 1)]


def test_check_answers_are_kept_only_when_complete():
    answer = json.dumps({"items": [{"n": 1, "russian": True, "known": False}, {"n": 2, "russian": "да"},
                                   {"n": 9, "russian": True, "known": False}, {"n": 3, "russian": False, "known": True}]})
    assert pick.parse_check(answer, 3) == {1: pick.Verdict(True, False), 3: pick.Verdict(False, True)}
    assert pick.parse_check("не знаю", 3) == {}
    prompt = pick.check_prompt(candidates("А <b>", "Б"))
    assert '"artist": "А <b>"' in prompt[-1]["content"]


def test_write_plan_takes_valid_items_and_the_best_song():
    cs = candidates("Один", "Два", "Три", "Четыре")
    answer = json.dumps({"items": [{"n": 2, "text": "Инди-поп из Казани, тёплый звук."},
                                   {"n": 2, "text": "повтор того же артиста, не нужен"},
                                   {"n": 7, "text": "такого кандидата нет вообще"},
                                   {"n": 1, "text": "коротко"},
                                   {"n": 3, "text": "Пост-панк с хорошими текстами."},
                                   {"n": 4, "text": "Электроника на русском языке."}],
                         "best": {"n": 3, "title": "Лучшая", "youtube": [f"https://youtu.be/{VIDEO}", "x"],
                                  "spotify": "https://open.spotify.com/track/" + "a" * 22}})
    plan = pick.parse_write(answer, cs)
    assert [n for n, _ in plan.items] == [2, 3, 4]
    assert plan.best == music.Pick("Три", "Лучшая", (VIDEO,), "a" * 22)

    # A best song outside the chosen ones: the first chosen artist's chart song, no links from the model.
    plan = pick.parse_write(json.dumps({"items": [{"n": 2, "text": "Инди-поп из Казани, тёплый звук."}],
                                        "best": {"n": 1, "youtube": [f"https://youtu.be/{VIDEO}"]}}), cs)
    assert plan.best == music.Pick("Два", "Песня Два", (), None)
    assert pick.parse_write('{"items": []}', cs) == pick.Plan([], None)
    assert pick.parse_write("Не нашёл", cs) is None


async def test_render_escapes_and_links_the_chart_song():
    cs = candidates("A<b>")
    plan = pick.Plan([(1, "Текст про <script> артиста.")], music.Pick("A<b>", "Песня", (VIDEO,), None))
    card = await music.build_card(plan.best, FakeChecker(videos={VIDEO: "A - Песня"}))
    text, plain = pick.render(plan, cs, TODAY, card)
    assert text.startswith("🎧 <b>Восходящие русские артисты, 9 октября</b>\n\n• <b>A&lt;b&gt;</b> — "
                           "«<a href=\"https://music.yandex.ru/track/1\">Песня A&lt;b&gt;</a>». Текст про &lt;script&gt;")
    assert text.endswith(card.html) and prompts.MUSIC_TRENDS_BEST in text
    assert "<" not in plain.split("\n")[0] and "https://music.yandex.ru/track/1" in plain


# ---- the service ----

class Charts:
    """Adapters without the network: chart key -> entries or an exception."""

    def __init__(self, **charts):
        self.charts, self.calls = charts, []

    def adapters(self):
        def make(kind):
            async def read(http, now, opts=None):
                self.calls.append(kind)
                result = self.charts.get(kind, [])
                if isinstance(result, Exception):
                    raise result
                return result
            return read
        return {k: make(k) for k in ("apple", "yandex", "shazam", "lastfm")}


def rising_yandex():
    return [Entry(5, "Новичок feat. Гость", "Взлёт", shift=60, url="https://music.yandex.ru/track/5"),
            Entry(10, "Звезда", "Хит", shift=40), Entry(1, "Стоячий", "Вечный", shift=0)] \
        + [Entry(p, f"filler{p}", "x") for p in range(11, 101)]


def make_trends(pool, charts, **kw):
    async def wanted():
        return True
    return TrendsService(pool, is_wanted=wanted, tz=TZ, daily_token_limit=kw.pop("limit", 10**6),
                         http_factory=lambda contact: FakeHttp(contact=contact), adapters=charts.adapters(), **kw)


def checked(messages):
    """The check model: «Звезда» is a star, everyone else a Russian newcomer."""
    rows = [json.loads(line) for line in messages[-1]["content"].splitlines()[1:]]
    return json.dumps({"items": [{"n": r["n"], "russian": True, "known": r["artist"] == "Звезда"} for r in rows]})


def written(messages):
    """The write-up model: the first candidate, its song with a video."""
    first = re.search(r"^\[1\] (.+?) — «(.+?)»", messages[-1]["content"], re.M)
    return json.dumps({"items": [{"n": 1, "text": f"{first.group(1)}: свежий русский инди-поп."}],
                       "best": {"n": 1, "title": first.group(2), "youtube": [f"https://www.youtube.com/watch?v={VIDEO}"]}},
                      ensure_ascii=False)


async def test_charts_are_read_once_a_day_and_a_failure_backs_off(pool, conn):
    charts = Charts(yandex=rising_yandex(), apple=HttpError(503, "x"))
    service = make_trends(pool, charts)
    assert await service.collect(NOW) == {"yandex_ru": "ok", "apple_ru": "http_503", "shazam_ru": "empty"}
    assert await conn.fetchval("SELECT count(*) FROM music_snapshots WHERE source = 'yandex_ru'") == 93
    assert await conn.fetchval("SELECT name FROM music_artists WHERE artist_key = 'новичок'") == "Новичок"
    assert await service.collect(NOW + timedelta(minutes=5)) == {}      # the failed ones wait out their pause
    assert set(await service.collect(NOW + timedelta(minutes=20))) == {"apple_ru", "shazam_ru"}
    states = await store.states(conn)
    assert states["apple_ru"]["failures"] == 2 and states["yandex_ru"]["last_ok_day"] == TODAY
    # A chart that refuses us (Yandex outside Russia) is asked again only the next day.
    charts.charts["shazam"] = HttpError(451, "x")
    later = NOW + timedelta(hours=1)
    assert (await service.collect(later))["shazam_ru"] == "http_451"
    assert "shazam_ru" not in await service.collect(later + timedelta(hours=23))
    assert (await store.states(conn))["shazam_ru"]["next_run_at"] == later + timedelta(hours=24)
    assert "lastfm" not in charts.calls       # no key, not read at all


async def test_a_restart_and_music_now_retry_a_refused_chart(pool, conn):
    # Yandex refused (451) before MUSIC_YANDEX_PROXY was set: the pause lasts a day.
    charts = Charts(yandex=HttpError(451, "x"), apple=rising_yandex())
    assert (await make_trends(pool, charts).collect(NOW))["yandex_ru"] == "http_451"
    charts.charts["yandex"] = rising_yandex()
    # The collector restarted with the proxy: its first read asks Yandex again at once.
    restarted = make_trends(pool, charts)
    assert await restarted.collect(NOW + timedelta(minutes=5)) == {"yandex_ru": "ok", "shazam_ru": "empty"}
    # Later reads of the same process keep the pauses; /music now does not wait for them.
    assert await restarted.collect(NOW + timedelta(minutes=10)) == {}
    assert await restarted.collect(NOW + timedelta(minutes=10), retry=True) == {"shazam_ru": "empty"}


async def test_recommendation_checks_writes_and_builds_the_card(pool, conn):
    charts = Charts(yandex=rising_yandex())
    service = make_trends(pool, charts)
    check, write = ScriptedLLM(checked), ScriptedLLM(written, profile="smart")
    checker = FakeChecker(videos={VIDEO: "Новичок - Взлёт (Official Video)"})
    post = await service.recommend(TODAY, check_llm=check, write_llm=write, checker=checker,
                                   player="https://t.me/ewok_nestor_bot/player", timeout_s=5)
    assert "Звезда" in check.calls[0][-1]["content"]
    assert "Звезда" not in write.calls[0][-1]["content"]       # the star never reaches the write-up
    assert write.calls[0][-1]["content"].splitlines()[1].startswith("[1] Новичок feat. Гость — «Взлёт». Яндекс")
    assert post.html.startswith("🎧 <b>Восходящие русские артисты")
    assert '<a href="https://music.yandex.ru/track/5">Взлёт</a>' in post.html
    assert post.preview_url == f"https://www.youtube.com/watch?v={VIDEO}"
    assert post.keyboard.inline_keyboard[0][0].url.endswith(f"?startapp={VIDEO}")
    artists = await store.artists(conn, ["новичок", "звезда"])
    assert artists["новичок"]["recommended_at"] is not None and artists["звезда"]["known"]
    assert artists["звезда"]["known_reason"] == "model"
    scenarios = [r["scenario"] for r in await conn.fetch("SELECT scenario FROM bot_requests ORDER BY id")]
    assert scenarios == ["music_check", "music_trends"]

    # The day's post is shared; a forced search makes a new one, without the artist just recommended.
    # The same after a restart: the day's post is kept in the database.
    service = make_trends(pool, charts)
    again = await service.recommend(TODAY, check_llm=check, write_llm=write, checker=checker, player=None,
                                    timeout_s=5)
    assert again == post and len(write.calls) == 1
    forced = await service.recommend(TODAY, check_llm=check, write_llm=write, checker=checker, player=None,
                                     timeout_s=5, force=True)
    assert forced.html == "" or "Новичок" not in forced.html
    assert len(check.calls) == 1        # verdicts are remembered


async def test_nothing_read_yet_and_nobody_rising(pool, conn):
    service = make_trends(pool, Charts())
    try:
        await service.recommend(TODAY, check_llm=None, write_llm=ScriptedLLM("{}"), checker=FakeChecker(),
                                player=None, timeout_s=5)
    except NotEnoughMusic:
        pass
    else:
        raise AssertionError("no chart, no recommendation")
    await conn.execute("DELETE FROM music_sources")      # the empty reads above wait out their pause
    service = make_trends(pool, Charts(apple=[Entry(1, "Стоячий", "Вечный")]))
    write = ScriptedLLM("{}")
    assert await service.recommend(TODAY, check_llm=None, write_llm=write, checker=FakeChecker(), player=None,
                                   timeout_s=5) == NOTHING
    assert write.calls == []


async def test_token_limit_stops_the_recommendation(pool, conn):
    service = make_trends(pool, Charts(yandex=rising_yandex()), limit=10)
    await conn.execute("INSERT INTO bot_requests (scenario, input_tokens, output_tokens, status) "
                       "VALUES ('news', 100, 0, 'ok')")
    write = ScriptedLLM(written)
    assert await service.recommend(TODAY, check_llm=None, write_llm=write, checker=FakeChecker(), player=None,
                                   timeout_s=5) is None
    assert write.calls == []


async def test_lastfm_listeners_rule_out_big_artists(pool, conn):
    charts = Charts(yandex=rising_yandex(), lastfm=[Entry(1, "filler11", "x")])
    service = make_trends(pool, charts, lastfm_key="KEY", max_listeners=300_000)
    big = {"Новичок": "5000000", "Звезда": "100"}

    def listeners(call):
        name = call["params"]["artist"]
        return Response(200, json.dumps({"artist": {"stats": {"listeners": big.get(name, "10")}}}).encode(), call["url"])
    service.http_factory = lambda contact: FakeHttp([("GET", "artist.getinfo", listeners)], contact=contact)
    write = ScriptedLLM(written)
    post = await service.recommend(TODAY, check_llm=None, write_llm=write, checker=FakeChecker(), player=None,
                                   timeout_s=5)
    assert "Новичок" not in write.calls[0][-1]["content"] and "Звезда" in post.html
    assert (await store.artists(conn, ["новичок"]))["новичок"]["listeners"] == 5_000_000


async def test_known_by_command_stays_known_and_the_model_can_change_its_mind(conn):
    assert await store.mark_known(conn, "Новичок feat. Гость") == "Новичок"
    await store.set_verdict(conn, "новичок", known=False, russian=True, now=NOW)
    row = (await store.artists(conn, ["новичок"]))["новичок"]
    assert row["known"] and row["known_reason"] == "command"
    await conn.execute("INSERT INTO music_artists (artist_key, name) VALUES ('другой', 'Другой')")
    await store.set_verdict(conn, "другой", known=True, russian=True, now=NOW)
    await store.set_verdict(conn, "другой", known=False, russian=True, now=NOW)
    row = (await store.artists(conn, ["другой"]))["другой"]
    assert not row["known"] and row["known_reason"] is None
    assert await store.mark_known(conn, " ,") is None


# ---- the bot ----

DAY = datetime.now(TZ).date()

def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, dtime(hour, minute), TZ)


def ago(**delta) -> int:
    return int((datetime.now(timezone.utc) - timedelta(**delta)).timestamp())


def with_trends(make_env, pool, charts=None, **cfg):
    smart = ScriptedLLM(written, profile="smart")
    news = ScriptedLLM(checked)
    bot, llm, feed = make_env(smart=smart, news_llm=news, admins={111},
                              checker=FakeChecker(videos={VIDEO: "Новичок - Взлёт"}), **cfg)
    feed.answerer.trends = make_trends(pool, charts or Charts(yandex=rising_yandex()))
    feed.answerer.trends.is_wanted = feed.answerer.music_wanted
    return bot, smart, feed


async def test_music_now_posts_the_card_once_an_hour(make_env, pool, conn):
    bot, smart, feed = with_trends(make_env, pool)
    await feed(update("message", message(1, "/music now", uid=111, date=ago(minutes=1))))
    assert bot.sent[0]["text"] == prompts.MUSIC_TRENDS_SEARCHING
    card = bot.sent[1]
    assert card["reply_to"] == 1 and card["parse_mode"] == "HTML" and "Новичок" in card["text"]
    assert card["link_preview_options"].url == f"https://www.youtube.com/watch?v={VIDEO}"
    assert card["reply_markup"].inline_keyboard
    await feed(update("message", message(2, "/music now", uid=111, date=ago(minutes=1))))
    assert bot.sent[-1]["text"] == prompts.MUSIC_TRENDS_TOO_SOON.format(minutes=60) and len(smart.calls) == 1


async def test_music_needs_an_admin_and_knows_its_words(make_env, pool, conn):
    bot, smart, feed = with_trends(make_env, pool)
    await feed(update("message", message(1, "/music now", uid=222, date=ago(minutes=1))))
    assert bot.sent[-1]["text"] == prompts.ADMINS_ONLY
    await feed(update("message", message(2, "/music known Новичок", uid=111, date=ago(minutes=1))))
    assert bot.sent[-1]["text"] == prompts.MUSIC_TRENDS_KNOWN.format(name="Новичок")
    await feed(update("message", message(3, "/music громче", uid=111, date=ago(minutes=1))))
    assert bot.sent[-1]["text"] == prompts.MUSIC_TRENDS_USAGE
    await feed(update("message", message(4, "/music on", uid=111, date=ago(minutes=1))))
    assert bot.sent[-1]["text"].startswith("🎧 <b>Восходящие русские артисты включены.</b> По пятницам в 19:00")
    assert await feed.answerer.music_wanted()
    await feed(update("message", message(5, "/music status", uid=111, date=ago(minutes=1))))
    assert bot.sent[-1]["text"].startswith(prompts.MUSIC_STATUS_HEADER)
    assert "Яндекс Музыка, чарт России: ещё не читался" in bot.sent[-1]["text"]
    assert smart.calls == []


async def test_weekly_post_on_its_day_skips_the_known(make_env, pool, conn):
    bot, smart, feed = with_trends(make_env, pool, music_chat_ids=frozenset({CHAT_ID}), music_day=DAY.weekday())
    await feed(update("message", message(1, "привет", date=ago(minutes=5))))
    answerer = feed.answerer
    await answerer.run_daily_posts(local(DAY, 18, 59))
    await answerer.drain()
    assert not bot.sent
    await answerer.run_daily_posts(local(DAY, 19, 1))
    await answerer.drain()
    post = bot.sent[-1]
    assert post["reply_to"] is None and post["text"].startswith("🎧 <b>Восходящие русские артисты")
    row = await conn.fetchrow("SELECT status, message_id FROM daily_posts WHERE kind = 'music'")
    assert tuple(row) == ("posted", post["message_id"])
    other_day = local(DAY + timedelta(days=1), 19, 1)
    await answerer.run_daily_posts(other_day)
    await answerer.drain()
    assert bot.sent[-1] is post and len(smart.calls) == 1


async def test_weekly_post_waits_until_a_chart_is_read(make_env, pool, conn):
    bot, smart, feed = with_trends(make_env, pool, charts=Charts(), music_chat_ids=frozenset({CHAT_ID}),
                                   music_day=DAY.weekday())
    await feed(update("message", message(1, "привет", date=ago(minutes=5))))
    await feed.answerer.run_daily_posts(local(DAY, 19, 1))
    await feed.answerer.drain()
    assert await conn.fetchval("SELECT status FROM daily_posts WHERE kind = 'music'") == "waiting"
    assert not bot.sent


async def test_weekly_post_reaches_a_group_that_moved_to_a_supergroup(make_env, pool, conn):
    # ALLOWED_CHAT_IDS still names the old basic group; the bot lives in the supergroup it became.
    new_id = CHAT_ID - 1
    bot, smart, feed = with_trends(make_env, pool, music_day=DAY.weekday())
    await conn.execute("INSERT INTO chats (chat_id, title, type, migrated_from_chat_id, settings) "
                       "VALUES ($1, 'Группа', 'supergroup', $2, '{\"music\": true}')", new_id, CHAT_ID)
    assert await feed.answerer.music_wanted()
    await feed.answerer.run_daily_posts(local(DAY, 19, 1))
    await feed.answerer.drain()
    assert bot.sent[-1]["chat_id"] == new_id and bot.sent[-1]["text"].startswith("🎧 <b>Восходящие")


async def test_check_stops_on_a_bad_proxy_without_showing_it(monkeypatch, capsys):
    from nestor.trends.__main__ import check
    monkeypatch.setenv("MUSIC_YANDEX_PROXY", "http://user:s3cret@host:not-a-port")
    try:
        await check(["yandex_ru"])
    except SystemExit as e:
        assert "s3cret" not in str(e)
    else:
        raise AssertionError("a bad MUSIC_YANDEX_PROXY must stop --check")
    assert "s3cret" not in capsys.readouterr().out
