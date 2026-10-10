"""The viral song of the day (nestor.trends): the charts' formats, the virality score, the model call,
the service, the Wayback history, and /music with the daily post through the real dispatcher."""
import json
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from nestor.bot import music, prompts
from nestor.news.http import HttpError
from nestor.trends import pick, sources, store, viral
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
    moscow = await sources.shazam_msk(FakeHttp([("GET", "csv/top-50/russia/moscow$", body.encode())]), NOW)
    assert moscow == entries


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


def chart(day, source, names, size=100, filler=None, **marks):
    """A chart with the named songs at their places and filler, in its own order, in the places left."""
    rows = [snap(day, source, pos, name, **marks.get(name, {})) for name, pos in names.items()]
    free = [p for p in range(1, size + 1) if p not in set(names.values())]
    rows += [snap(day, source, p, f"filler{i}", **(filler or {})) for i, p in enumerate(free, 1)]
    return rows


def days(source, places, **kw):
    """places: name -> its place on each of the last days, today last; None: not in the chart that day."""
    n = max(len(v) for v in places.values())
    rows = []
    for i in range(n):
        day = TODAY - timedelta(days=n - 1 - i)
        rows += chart(day, source, {name: p[i] for name, p in places.items() if p[i] is not None}, **kw)
    return rows


def picked(rows, **kw):
    return [c.artist for c in viral.viral(rows, TODAY, **kw)]


def test_a_song_whose_popularity_doubles_within_days_went_viral():
    rows = days("shazam_ru", {"Ракета": [90, 80, 60, 40, 20], "Ползун": [60, 58, 56, 54, 52]}) \
        + days("yandex_ru", {"Ракета": [95, 85, 70, 45, 25]})
    [c] = viral.viral(rows, TODAY)
    assert (c.artist, c.best) == ("Ракета", 20) and c.charts == ["shazam_ru", "yandex_ru"]
    assert c.facts == ["Shazam, топ России: 80 → 20 место за 3 дн.", "Яндекс Музыка, чарт России: 85 → 25 место за 3 дн."]
    # The log scale: 40 → 20 is as much growth as 4 → 2, so a climb near the top counts as much.
    top = days("shazam_ru", {"Звезда": [9, 8, 6, 4, 2]}) + days("yandex_ru", {"Звезда": [9, 8, 6, 4, 2]})
    assert picked(top) == ["Звезда"]


def test_a_one_day_blip_needs_a_second_chart():
    blip = {"Вспышка": [None, None, None, None, 10]}
    assert picked(days("shazam_ru", blip)) == []
    assert picked(days("shazam_ru", blip) + days("apple_ru", {"Вспышка": [None, None, None, 70, 30]})) == ["Вспышка"]


def test_too_low_too_slow_or_long_at_the_top_is_not_viral():
    low = days("shazam_ru", {"Низко": [190, 180, 150, 110, 60]}, size=200)       # still below the top 50
    slow = days("shazam_ru", {"Медленно": [50, 48, 46, 44, 42]})               # rising, but far from doubling
    assert picked(low) == [] and picked(slow) == []
    # Weeks in the top 20, then a climb to the top: it has peaked, the same climb of a newcomer scores more.
    old = days("shazam_ru", {"Старый": [15] * 40 + [12, 9, 5, 3, 1]})
    new = days("shazam_ru", {"Новый": [None] * 40 + [60, 40, 20, 10, 5]})
    old_score = viral.viral(old, TODAY, threshold=0)[0].score
    assert viral.viral(new, TODAY)[0].score > 5 * old_score


def test_cooldowns_and_banned_artists():
    rows = days("shazam_ru", {"Ракета": [90, 80, 60, 40, 20]}) + days("yandex_ru", {"Ракета": [95, 85, 70, 45, 25]})
    key = sources.song_key("Ракета", "Песня")
    assert picked(rows, picks=[viral.Pick(TODAY - timedelta(days=10), key, "ракета")]) == []
    assert picked(rows, picks=[viral.Pick(TODAY - timedelta(days=40), key, "ракета")]) == ["Ракета"]
    other = sources.song_key("Ракета", "Другая")
    assert picked(rows, picks=[viral.Pick(TODAY - timedelta(days=1), other, "ракета")]) == []      # the artist
    assert picked(rows, picks=[viral.Pick(TODAY - timedelta(days=5), other, "ракета")]) == ["Ракета"]
    assert picked(rows, picks=[viral.Pick(TODAY, key, "ракета")]) == ["Ракета"]    # today's own pick stays
    assert picked(rows, banned=frozenset({"ракета"})) == []


def test_the_same_song_across_charts_and_a_gap_of_a_day():
    assert sources.song_key("Ракета feat. Гость", "Взлёт (prod. X) [Remix]") == sources.song_key("ракета", "ВЗЛЕТ")
    rows = days("shazam_ru", {"Ракета": [90, 80, 60, 40, 20]})
    rows = [r for r in rows if r["day"] != TODAY - timedelta(days=2)]      # Shazam not read that day
    for r in days("yandex_ru", {"Ракета": [95, 85, 70, 45, 25]}):
        if r["artist"] == "Ракета":
            r["artist"], r["title"] = "Ракета feat. Гость", "Песня (Remix)"
        rows.append(r)
    [c] = viral.viral(rows, TODAY)
    assert c.charts == ["shazam_ru", "yandex_ru"] and c.artist == "Ракета"


def test_the_first_run_trusts_the_charts_own_mark_only_when_it_stands_out():
    rows = chart(TODAY, "yandex_ru", {"Ракета": 10, "Стоячий": 5, "Дебют": 30, "Шаг": 40}, filler={"shift": 0},
                 Ракета={"shift": 80}, Стоячий={"shift": 0}, Дебют={"is_new": True}, Шаг={"shift": 1})
    found = viral.viral(rows, TODAY)
    assert [c.artist for c in found] == ["Ракета", "Дебют"]
    assert found[0].facts == ["Яндекс Музыка, чарт России: 10 место, поднялся на 80"]
    assert viral.viral(chart(TODAY - timedelta(days=3), "yandex_ru", {"Ракета": 1}, Ракета={"shift": 90}), TODAY) == []


# ---- the model call ----

def candidate(artist="Ракета", title="Взлёт", url="https://music.yandex.ru/track/1"):
    return viral.Candidate(sources.song_key(artist, title), sources.artist_key(artist), artist, title, url, 7.5, 3,
                           ["Shazam, топ России: 80 → 3 место за 3 дн."], ["shazam_ru"])


def test_write_plan_takes_the_text_and_the_links():
    c = candidate()
    answer = json.dumps({"text": "Песня разошлась в VK Клипах после челленджа.", "title": "Взлёт",
                         "youtube": [f"https://youtu.be/{VIDEO}", "x"], "spotify": "https://open.spotify.com/track/" + "a" * 22})
    plan = pick.parse_write(answer, c)
    assert plan == pick.Plan("Песня разошлась в VK Клипах после челленджа.", music.Pick("Ракета", "Взлёт", (VIDEO,), "a" * 22))
    assert pick.parse_write(json.dumps({"text": "коротко"}), c) == pick.Plan("", music.Pick("Ракета", "Взлёт", (), None))
    assert pick.parse_write("Не нашёл", c) is None
    assert "Ракета — «Взлёт»" in pick.write_prompt(c, TODAY)[-1]["content"]


async def test_render_escapes_and_links_the_chart_song():
    c = candidate(artist="A<b>", title="Песня")
    plan = pick.Plan("Текст про <script> песню.", music.Pick("A<b>", "Песня", (VIDEO,), None))
    card = await music.build_card(plan.best, FakeChecker(videos={VIDEO: "A - Песня"}))
    text, plain = pick.render(plan, c, TODAY, card)
    assert text.startswith("🔥 <b>Вирусная песня дня, 9 октября</b>\n\n<b>A&lt;b&gt;</b> — "
                           "«<a href=\"https://music.yandex.ru/track/1\">Песня</a>». Текст про &lt;script&gt;")
    assert "Рост в чартах: Shazam, топ России: 80 → 3 место за 3 дн." in text and text.endswith(card.html)
    assert "<" not in plain.split("\n")[0] and "https://music.yandex.ru/track/1" in plain


# ---- the service ----

class Charts:
    """Adapters without the network: chart kind -> entries or an exception."""

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
        return {k: make(k) for k in sources.ADAPTERS}


def rising_yandex():
    """The first read of Yandex: its own marks of the move, most songs standing still."""
    return [Entry(5, "Новичок feat. Гость", "Взлёт", shift=60, url="https://music.yandex.ru/track/5"),
            Entry(10, "Звезда", "Хит", shift=40), Entry(1, "Стоячий", "Вечный", shift=0)] \
        + [Entry(p, f"filler{p}", "x", shift=0) for p in range(11, 101)]


EMPTY = {"apple_ru": "empty", "shazam_ru": "empty", "shazam_msk": "empty"}


def make_trends(pool, charts, **kw):
    async def wanted():
        return True
    return TrendsService(pool, is_wanted=wanted, tz=TZ, daily_token_limit=kw.pop("limit", 10**6),
                         http_factory=lambda contact: FakeHttp(contact=contact), adapters=charts.adapters(), **kw)


def written(messages):
    """The write-up model: a few words and the song's video."""
    song = re.search(r"Песня: (.+?) — «(.+?)»", messages[-1]["content"])
    return json.dumps({"text": f"{song.group(1)}: песня разошлась в VK Клипах.", "title": song.group(2),
                       "youtube": [f"https://www.youtube.com/watch?v={VIDEO}"]}, ensure_ascii=False)


async def test_charts_are_read_once_a_day_and_a_failure_backs_off(pool, conn):
    charts = Charts(yandex=rising_yandex(), apple=HttpError(503, "x"))
    service = make_trends(pool, charts)
    assert await service.collect(NOW) == EMPTY | {"yandex_ru": "ok", "apple_ru": "http_503"}
    assert await conn.fetchval("SELECT count(*) FROM music_snapshots WHERE source = 'yandex_ru'") == 93
    assert await conn.fetchval("SELECT name FROM music_artists WHERE artist_key = 'новичок'") == "Новичок"
    assert await service.collect(NOW + timedelta(minutes=5)) == {}      # the failed ones wait out their pause
    assert set(await service.collect(NOW + timedelta(minutes=20))) == set(EMPTY)
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
    assert await restarted.collect(NOW + timedelta(minutes=5)) == {"yandex_ru": "ok", "shazam_ru": "empty",
                                                                    "shazam_msk": "empty"}
    # Later reads of the same process keep the pauses; /music now does not wait for them.
    assert await restarted.collect(NOW + timedelta(minutes=10)) == {}
    assert await restarted.collect(NOW + timedelta(minutes=10), retry=True) == {"shazam_ru": "empty",
                                                                               "shazam_msk": "empty"}
    # Asked again soon, from this or another group: the pauses hold.
    assert await restarted.collect(NOW + timedelta(minutes=11), retry=True) == {}
    # A forced read with nothing waiting does not use up the hour: the next one still may retry.
    every = rising_yandex()
    fresh = make_trends(pool, Charts(yandex=every, apple=every, shazam=every, shazam_msk=every))
    await fresh.collect(NOW + timedelta(minutes=12))
    assert await fresh.collect(NOW + timedelta(minutes=12), retry=True) == {} and fresh._forced_at is None


async def test_the_song_of_the_day_is_written_kept_and_not_repeated(pool, conn):
    today = datetime.now(TZ).date()     # recommend() reads the charts now
    service = make_trends(pool, Charts(yandex=rising_yandex()))
    write = ScriptedLLM(written, profile="smart")
    checker = FakeChecker(videos={VIDEO: "Новичок - Взлёт (Official Video)"})
    post = await service.recommend(today, write_llm=write, checker=checker,
                                   player="https://t.me/ewok_nestor_bot/player", timeout_s=5)
    assert "Песня: Новичок feat. Гость — «Взлёт»" in write.calls[0][-1]["content"]
    assert post.html.startswith(f"🔥 <b>Вирусная песня дня, {today.day} ")
    assert ('<b>Новичок feat. Гость</b> — «<a href="https://music.yandex.ru/track/5">Взлёт</a>». '
            "Новичок feat. Гость: песня") in post.html
    assert "Рост в чартах: Яндекс Музыка, чарт России: 5 место, поднялся на 60." in post.html
    assert post.preview_url == f"https://www.youtube.com/watch?v={VIDEO}"
    assert post.keyboard.inline_keyboard[0][0].url.endswith(f"?startapp={VIDEO}")
    assert await conn.fetchval("SELECT song_key FROM music_picks WHERE day = $1", today) == "новичок|взлет"
    assert [r["scenario"] for r in await conn.fetch("SELECT scenario FROM bot_requests")] == ["music_trends"]

    # The day's post is shared, also after a restart; /music now makes it afresh: today's song again.
    service = make_trends(pool, Charts(yandex=rising_yandex()))
    assert await service.recommend(today, write_llm=write, checker=checker, player=None, timeout_s=5) == post
    assert len(write.calls) == 1
    forced = await service.recommend(today, write_llm=write, checker=checker, player=None, timeout_s=5, force=True)
    assert "Новичок" in forced.html and len(write.calls) == 2
    # The next day the same song and artist wait out their cooldown: the next viral song.
    nxt = await service.recommend(today + timedelta(days=1), write_llm=write, checker=checker, player=None,
                                  timeout_s=5)
    assert "<b>Звезда</b>" in nxt.html


async def test_nothing_read_yet_and_nothing_viral(pool, conn):
    service = make_trends(pool, Charts())
    try:
        await service.recommend(TODAY, write_llm=ScriptedLLM("{}"), checker=FakeChecker(), player=None, timeout_s=5)
    except NotEnoughMusic:
        pass
    else:
        raise AssertionError("no chart, no song")
    await conn.execute("DELETE FROM music_sources")      # the empty reads above wait out their pause
    service = make_trends(pool, Charts(apple=[Entry(1, "Стоячий", "Вечный")]))
    write = ScriptedLLM("{}")
    assert await service.recommend(TODAY, write_llm=write, checker=FakeChecker(), player=None, timeout_s=5) == NOTHING
    assert write.calls == [] and await conn.fetchval("SELECT count(*) FROM music_picks") == 0


async def test_token_limit_stops_the_song_of_the_day(pool, conn):
    service = make_trends(pool, Charts(yandex=rising_yandex()), limit=10)
    await conn.execute("INSERT INTO bot_requests (scenario, input_tokens, output_tokens, status) "
                       "VALUES ('news', 100, 0, 'ok')")
    write = ScriptedLLM(written)
    assert await service.recommend(TODAY, write_llm=write, checker=FakeChecker(), player=None, timeout_s=5) is None
    assert write.calls == []


async def test_known_by_command_is_never_the_song_of_the_day(pool, conn):
    assert await store.mark_known(conn, "Новичок feat. Гость") == "Новичок"
    assert await store.banned(conn) == frozenset({"новичок"})
    assert await store.mark_known(conn, " ,") is None
    service = make_trends(pool, Charts(yandex=rising_yandex()))
    await service.collect(NOW)
    async with pool.acquire() as c:
        assert [x.artist for x in await service.candidates(c, TODAY)] == ["Звезда"]


async def test_history_from_the_wayback_machine_once(pool, conn):
    from nestor.trends.backfill import backfill

    def day(n):
        return TODAY - timedelta(days=n)

    stamps = [["timestamp"], [f"{day(3):%Y%m%d}120000"], [f"{day(2):%Y%m%d}090000"], [f"{day(1):%Y%m%d}100000"]]
    csv_day = "Rank,Artist,Title\n1,Ракета,Взлёт\n2,Другой,Песня\n"
    http = FakeHttp([("GET", r"cdx.*top-200%2Frussia", json.dumps(stamps).encode()),
                     ("GET", r"cdx", b""),
                     ("GET", rf"web/{day(2):%Y%m%d}090000id_/", HttpError(404, "x")),       # a broken copy
                     ("GET", r"web/\d+id_/https://www.shazam.com/services/charts/csv/top-200/russia",
                      csv_day.encode())])
    await store.save_chart(conn, day(1), "shazam_ru", [Entry(1, "Свой", "Прочитан")])    # the bot's own read
    added = await backfill(pool, http, TODAY)
    assert added == {"shazam_ru": 1, "shazam_msk": 0, "apple_ru": 0}
    rows = await conn.fetch("SELECT day, artist, origin FROM music_snapshots ORDER BY day, position")
    assert [(r["day"], r["artist"], r["origin"]) for r in rows] == [
        (day(3), "Ракета", "wayback"), (day(3), "Другой", "wayback"), (day(1), "Свой", "read")]
    assert http.calls[0]["params"]["collapse"] == "timestamp:8"

    # The service copies the history once and marks it done.
    service = make_trends(pool, Charts())
    service.http_factory = lambda contact: http
    await service.backfill(NOW)
    calls = len(http.calls)
    await service.backfill(NOW)
    assert len(http.calls) == calls and (await store.states(conn))["wayback"]["status"].startswith("ok")


# ---- the bot ----

DAY = datetime.now(TZ).date()

def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, dtime(hour, minute), TZ)


def ago(**delta) -> int:
    return int((datetime.now(timezone.utc) - timedelta(**delta)).timestamp())


def with_trends(make_env, pool, charts=None, **cfg):
    smart = ScriptedLLM(written, profile="smart")
    bot, llm, feed = make_env(smart=smart, admins={111}, checker=FakeChecker(videos={VIDEO: "Новичок - Взлёт"}), **cfg)
    feed.answerer.trends = make_trends(pool, charts or Charts(yandex=rising_yandex()))
    feed.answerer.trends.is_wanted = feed.answerer.music_wanted
    return bot, smart, feed


async def test_music_now_posts_the_card_ten_times_an_hour(make_env, pool, conn):
    bot, smart, feed = with_trends(make_env, pool)
    await feed(update("message", message(1, "/music now", uid=111, date=ago(minutes=1))))
    assert bot.sent[0]["text"] == prompts.MUSIC_TRENDS_SEARCHING
    card = bot.sent[1]
    assert card["reply_to"] == 1 and card["parse_mode"] == "HTML" and "Новичок" in card["text"]
    assert card["link_preview_options"].url == f"https://www.youtube.com/watch?v={VIDEO}"
    assert card["reply_markup"].inline_keyboard
    for n in range(2, 11):
        await feed(update("message", message(n, "/music now", uid=111, date=ago(minutes=1))))
    assert sum(m["text"] == prompts.MUSIC_TRENDS_SEARCHING for m in bot.sent) == 10 and len(smart.calls) == 10
    await feed(update("message", message(11, "/music now", uid=111, date=ago(minutes=1))))
    assert bot.sent[-1]["text"] == prompts.MUSIC_TRENDS_TOO_SOON.format(times=10, minutes=60)
    assert len(smart.calls) == 10


async def test_music_needs_an_admin_and_knows_its_words(make_env, pool, conn):
    bot, smart, feed = with_trends(make_env, pool)
    await feed(update("message", message(1, "/music now", uid=222, date=ago(minutes=1))))
    assert bot.sent[-1]["text"] == prompts.ADMINS_ONLY
    await feed(update("message", message(2, "/music known Новичок", uid=111, date=ago(minutes=1))))
    assert bot.sent[-1]["text"] == prompts.MUSIC_TRENDS_KNOWN.format(name="Новичок")
    await feed(update("message", message(3, "/music громче", uid=111, date=ago(minutes=1))))
    assert bot.sent[-1]["text"] == prompts.MUSIC_TRENDS_USAGE
    await feed(update("message", message(4, "/music on", uid=111, date=ago(minutes=1))))
    assert bot.sent[-1]["text"].startswith("🔥 <b>Вирусная песня дня включена.</b> Каждый день в 19:00")
    assert await feed.answerer.music_wanted()
    await feed(update("message", message(5, "/music status", uid=111, date=ago(minutes=1))))
    assert bot.sent[-1]["text"].startswith(prompts.MUSIC_STATUS_HEADER)
    assert "Яндекс Музыка, чарт России: ещё не читался" in bot.sent[-1]["text"]
    assert "Дней истории в базе: 0, песен дня уже было: 0." in bot.sent[-1]["text"]
    assert smart.calls == []


async def test_the_daily_post_every_day(make_env, pool, conn):
    bot, smart, feed = with_trends(make_env, pool, music_chat_ids=frozenset({CHAT_ID}))
    await feed(update("message", message(1, "привет", date=ago(minutes=5))))
    answerer = feed.answerer
    await answerer.run_daily_posts(local(DAY, 18, 59))
    await answerer.drain()
    assert not bot.sent
    await answerer.run_daily_posts(local(DAY, 19, 1))
    await answerer.drain()
    post = bot.sent[-1]
    assert post["reply_to"] is None and post["text"].startswith("🔥 <b>Вирусная песня дня")
    assert "<b>Новичок feat. Гость</b>" in post["text"]
    row = await conn.fetchrow("SELECT status, message_id FROM daily_posts WHERE kind = 'music'")
    assert tuple(row) == ("posted", post["message_id"])
    await answerer.run_daily_posts(local(DAY, 19, 30))      # once a day
    await answerer.drain()
    assert bot.sent[-1] is post
    await answerer.run_daily_posts(local(DAY + timedelta(days=1), 19, 1))
    await answerer.drain()
    assert bot.sent[-1] is not post and "<b>Звезда</b>" in bot.sent[-1]["text"] and len(smart.calls) == 2


async def test_the_daily_post_waits_until_a_chart_is_read(make_env, pool, conn):
    bot, smart, feed = with_trends(make_env, pool, charts=Charts(), music_chat_ids=frozenset({CHAT_ID}))
    await feed(update("message", message(1, "привет", date=ago(minutes=5))))
    await feed.answerer.run_daily_posts(local(DAY, 19, 1))
    await feed.answerer.drain()
    assert await conn.fetchval("SELECT status FROM daily_posts WHERE kind = 'music'") == "waiting"
    assert not bot.sent


async def test_the_daily_post_reaches_a_group_that_moved_to_a_supergroup(make_env, pool, conn):
    # ALLOWED_CHAT_IDS still names the old basic group; the bot lives in the supergroup it became.
    new_id = CHAT_ID - 1
    bot, smart, feed = with_trends(make_env, pool)
    await conn.execute("INSERT INTO chats (chat_id, title, type, migrated_from_chat_id, settings) "
                       "VALUES ($1, 'Группа', 'supergroup', $2, '{\"music\": true}')", new_id, CHAT_ID)
    assert await feed.answerer.music_wanted()
    await feed.answerer.run_daily_posts(local(DAY, 19, 1))
    await feed.answerer.drain()
    assert bot.sent[-1]["chat_id"] == new_id and bot.sent[-1]["text"].startswith("🔥 <b>Вирусная")


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


async def test_backtest_prints_the_song_each_day_would_have_got(database, conn, monkeypatch, capsys):
    from nestor.trends import __main__ as cli

    today = datetime.now(TZ).date()
    places = {"Ракета": [90, 80, 60, 40, 20, 15], "Тихий": [30, 30, 30, 30, 30, 30]}
    for i in range(6):
        day = today - timedelta(days=5 - i)
        for source in ("shazam_ru", "yandex_ru"):
            entries = [Entry(p[i], name, "Песня") for name, p in places.items()]
            taken = {e.position for e in entries}
            entries += [Entry(p, f"filler{n}", "x") for n, p in enumerate((p for p in range(1, 101) if p not in taken), 1)]
            await store.save_chart(conn, day, source, entries)
    monkeypatch.setenv("DATABASE_URL", database["collector"])
    assert await cli.backtest(5, viral.THRESHOLD, 1) == 0
    out = capsys.readouterr().out
    assert "В базе дней с чартами: 6" in out
    assert f"{today - timedelta(days=1):%d.%m.%Y}  Ракета — Песня" in out       # the first day it qualifies
    assert f"{today:%d.%m.%Y}  —  вирусной песни нет" in out                     # picked yesterday: its cooldown
    assert "Дней с вирусной песней: 1." in out
