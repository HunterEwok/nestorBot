"""Community adapters: Reddit through Arctic Shift, Telegram channel previews (t.me/s) and
Reggaeville's list pages, on saved responses (tests/news_fixtures/{arctic,tme,reggaeville}_*),
without network or database."""
from __future__ import annotations

import dataclasses
import importlib
import json
import re
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import nestor.news
from nestor.news.filters import drop_reason
from nestor.news.http import HttpError, Response
from nestor.news.model import FetchResult, Source
from nestor.news.sources import ARCTIC_SHIFT, SOURCES, telegram_source

from .news_helpers import FakeHttp, fixture


def _adapter(name: str) -> types.ModuleType:
    """The adapter module. While sibling adapters are not written yet, the package __init__
    (which imports all of them) is bypassed."""
    try:
        return importlib.import_module(f"nestor.news.adapters.{name}")
    except ImportError as e:
        if not (e.name or "").startswith("nestor.news.adapters"):
            raise
    pkg = types.ModuleType("nestor.news.adapters")
    pkg.__path__ = [str(Path(nestor.news.__file__).parent / "adapters")]
    sys.modules["nestor.news.adapters"] = pkg
    try:
        return importlib.import_module(f"nestor.news.adapters.{name}")
    finally:
        del sys.modules["nestor.news.adapters"]


arctic = _adapter("arctic")
tme = _adapter("tme")
reggaeville = _adapter("reggaeville")

UTC = timezone.utc
NOW = datetime(2026, 10, 4, 18, 30, tzinfo=UTC)
AFTER = int((NOW - timedelta(hours=60)).timestamp())     # 2026-10-02 06:30 UTC
BEFORE = int((NOW - timedelta(hours=36)).timestamp())    # 2026-10-03 06:30 UTC
SRC = {s.key: s for s in SOURCES}
ARCTIC_URL = r"^https://arctic-shift\.photon-reddit\.com/api/posts/search\?"
PSV = "psychedelicsocietyvienna"
# The end of a 2188-char post, well past the 1500 chars of its summary.
TRADE_TAIL = "Bestellung per PN, Versand nach ganz Österreich, 30 € pro Packung"
LONG_POST = "Integration circle notes from Vienna, thank you all for coming. " * 33 + "Bis morgen " + TRADE_TAIL


def _plain_text(item) -> None:
    for text in (item.title, item.summary):
        assert not re.search(r"</?(?:a|b|i|br|p|div|span|img)\b|&[a-z]+;|&#|\s\s", text) and text == text.strip()
    assert item.title
    assert item.url.startswith(("https://", "http://"))
    assert item.published_at is None or item.published_at.tzinfo is not None


def _by_id(items) -> dict:
    return {i.ext_id: i for i in items}


# ---- Arctic Shift ----

async def test_arctic_request_parameters():
    http = FakeHttp([("GET", ARCTIC_URL, fixture("arctic_psychonaut.json"))])
    result = await arctic.fetch(http, SRC["reddit_psychonaut"], {}, NOW)
    assert isinstance(result, FetchResult) and not result.skipped
    assert result.cursor == {"until": BEFORE}  # where the next run's window starts if it comes late
    assert len(http.calls) == 1  # fewer than 100 posts: no second page
    call = http.calls[0]
    assert call["url"].startswith(ARCTIC_SHIFT + "?")
    assert call["params"] == {"subreddit": "Psychonaut", "after": AFTER, "before": BEFORE, "limit": 100,
                              "sort": "desc"}
    assert "fields" not in call["url"]  # fields= with permalink is HTTP 400
    assert [i.ext_id for i in result.items] == ["1ww4k2p", "1ww2c9x", "1ww1zz0", "1ww0q7f", "1ww0g3a"]
    for item in result.items:
        _plain_text(item)
        assert item.lang == "en" and item.published_at.tzinfo == UTC


async def test_arctic_window_does_not_depend_on_the_timezone_of_now():
    http = FakeHttp([("GET", ARCTIC_URL, fixture("arctic_empty.json"))])
    moscow = NOW.astimezone(timezone(timedelta(hours=3)))
    result = await arctic.fetch(http, SRC["reddit_reggae"], {}, moscow)
    assert result.items == []
    assert http.calls[0]["params"]["after"] == AFTER and http.calls[0]["params"]["before"] == BEFORE
    assert http.calls[0]["params"]["subreddit"] == "reggae"


@pytest.mark.parametrize("cursor, after", [
    ({"until": AFTER - 3600}, AFTER - 3601),            # a run an hour late: from where the last window ended
    ({"until": AFTER - 24 * 3600}, AFTER - 24 * 3600 - 1),  # a day lost: still caught up
    ({"until": AFTER - 24 * 3600 - 1}, AFTER),          # longer outages: only the usual window
    ({"until": AFTER}, AFTER),                          # on time
    ({"until": AFTER + 600}, AFTER),                    # an early run only overlaps; the store dedupes
    ({"until": True}, AFTER), ({"until": str(AFTER - 60)}, AFTER), ({"until": None}, AFTER), ({}, AFTER),
])
async def test_arctic_late_run_starts_where_the_last_window_ended(cursor, after):
    http = FakeHttp([("GET", ARCTIC_URL, fixture("arctic_empty.json"))])
    result = await arctic.fetch(http, SRC["reddit_dub"], cursor, NOW)
    assert http.calls[0]["params"]["after"] == after and http.calls[0]["params"]["before"] == BEFORE
    assert result.cursor == {"until": BEFORE}


async def test_arctic_consecutive_late_runs_leave_no_gap():
    windows = []
    http = FakeHttp([("GET", ARCTIC_URL, lambda call: windows.append(call["params"]) or fixture("arctic_empty.json"))])
    first = await arctic.fetch(http, SRC["reddit_dub"], {}, NOW)
    await arctic.fetch(http, SRC["reddit_dub"], first.cursor, NOW + timedelta(hours=24, minutes=7))
    assert windows[1]["after"] < windows[0]["before"]   # 7 minutes late: the slice in between is asked for too


def test_arctic_link_and_self_posts():
    items = _by_id(arctic.parse_posts(fixture("arctic_psychonaut.json"), "Psychonaut").items)
    link = items["1ww4k2p"]
    assert link.url == "https://www.reddit.com/r/Psychonaut/comments/1ww4k2p/psilocybin_therapy_shows_lasting_benefit_in/"
    assert link.title == ("Psilocybin therapy shows lasting benefit in treatment-resistant depression & anxiety, "
                          "5-year follow-up finds")
    assert link.summary == ""
    assert link.published_at == datetime(2026, 10, 3, 4, 20, 34, tzinfo=UTC)
    assert link.meta == {
        "subreddit": "Psychonaut", "score": 87, "num_comments": 23, "is_self": False,
        "link": "https://www.psypost.org/psilocybin-therapy-lasting-benefit-five-year-follow-up/"
                "?utm_source=reddit&utm_medium=social",
        "domain": "psypost.org", "over_18": False, "stickied": False, "removed": False, "flair": "Research",
    }

    own = items["1ww2c9x"]
    assert own.title == "Integration after a difficult experience — what helped you?"
    assert own.meta["is_self"] is True and own.meta["link"] is None and own.meta["domain"] == "self.Psychonaut"
    assert own.meta["score"] == 41 and own.meta["num_comments"] == 57 and own.meta["flair"] == "Discussion"
    # Markdown and escaped entities as plain text: no **, no [text](url), no "&#x200B;", "<3" kept.
    assert own.summary.startswith("Background A month ago I had a really rough night & I'm still processing it. "
                                  "The mind is a terrible thing to waste Things that helped so far: journaling ")
    assert "talking to a MAPS integration therapist Any other ideas? Thanks <3" in own.summary
    assert "maps.org" not in own.summary and "**" not in own.summary and "​" not in own.summary


def test_arctic_removed_flags():
    items = _by_id(arctic.parse_posts(fixture("arctic_psychonaut.json"), "Psychonaut").items)
    # Spam-filtered: removed_by_category "reddit" and selftext "[removed]"; the raw category is kept for --check -v.
    assert items["1ww1zz0"].meta["removed"] is True and items["1ww1zz0"].summary == ""
    assert items["1ww1zz0"].meta["removed_by"] == "reddit"
    # Deleted by the author: removed_by_category "deleted", selftext "[deleted]" is not a summary.
    assert items["1ww0q7f"].meta["removed"] is True and items["1ww0q7f"].summary == ""
    assert items["1ww0q7f"].meta["removed_by"] == "deleted"
    # Live posts: the key is absent (1ww4k2p, 1ww2c9x) or, defensively, null (1ww0g3a).
    assert not any(items[k].meta["removed"] for k in ("1ww4k2p", "1ww2c9x", "1ww0g3a"))
    assert not any("removed_by" in items[k].meta for k in ("1ww4k2p", "1ww2c9x", "1ww0g3a"))

    def post(**kw) -> dict:
        return {"id": "1abc", "title": "t", "subreddit": "dub", "created_utc": BEFORE - 100, "is_self": True, **kw}

    body = json.dumps({"data": [
        post(id="a1", selftext="[removed]"),                     # no category yet, body already gone
        post(id="a2", removed_by_category="moderator", selftext="still archived text"),
        post(id="a3", removed_by_category=None, selftext="live"),
        post(id="a4", selftext="[deleted]"),                     # deleted, no category: the body was never seen
        post(id="a5", removed_by_category="", selftext="live too"),
    ]}).encode()
    items = arctic.parse_posts(body, "dub").items
    flags = {i.ext_id: (i.meta["removed"], i.summary) for i in items}
    assert flags == {"a1": (True, ""), "a2": (True, "still archived text"), "a3": (False, "live"), "a4": (True, ""),
                     "a5": (False, "live too")}
    assert {i.ext_id: i.meta.get("removed_by") for i in items} == {
        "a1": None, "a2": "moderator", "a3": None, "a4": None, "a5": None}
    deleted = next(i for i in items if i.ext_id == "a4")
    therapy = dataclasses.replace(deleted, meta={**deleted.meta, "subreddit": "PsychedelicTherapy", "score": 20})
    assert drop_reason(therapy, SRC["reddit_psychedelictherapy"]) == "removed"


def test_arctic_fallback_url_and_malformed_posts():
    page = arctic.parse_posts(fixture("arctic_psychonaut.json"), "Psychonaut")
    assert page.posts == 7 and page.oldest == 1790940000  # the oldest post counts even though it is skipped
    gallery = _by_id(page.items)["1ww0g3a"]
    assert gallery.url == "https://www.reddit.com/r/Psychonaut/comments/1ww0g3a/"  # no permalink in the object
    assert gallery.title == 'Visual art I made: "Ego dissolution" <series 3>'
    assert gallery.meta["over_18"] is True and gallery.meta["flair"] is None
    assert gallery.meta["link"] == "https://www.reddit.com/gallery/1ww0g3a" and gallery.meta["domain"] == "reddit.com"
    # blank title (1ww0e11) and an invalid id are skipped
    assert "1ww0e11" not in _by_id(page.items) and len(page.items) == 5

    body = json.dumps({"data": [
        "not an object",
        {"id": "b1", "title": "Permalink of another post", "subreddit": "dub", "created_utc": "1790950000.0",
         "permalink": "/r/dub/comments/zzzz/other/", "is_self": False, "url": "javascript:alert(1)",
         "score": "12", "num_comments": None, "over_18": "true"},
        {"id": "b2", "title": "Huge numbers", "subreddit": "dub", "created_utc": "9" * 400, "score": "9" * 5000},
    ]}).encode()
    item, huge = arctic.parse_posts(body, "dub").items
    assert huge.published_at is None and huge.meta["score"] == 0
    assert item.url == "https://www.reddit.com/r/dub/comments/b1/"
    assert item.published_at == datetime.fromtimestamp(1790950000, UTC)
    assert item.meta["link"] is None and item.meta["domain"] == ""
    assert item.meta["score"] == 12 and item.meta["num_comments"] == 0 and item.meta["over_18"] is False


def test_arctic_reggae_and_posts_of_other_subs():
    items = arctic.parse_posts(fixture("arctic_reggae.json"), "reggae").items
    assert [i.ext_id for i in items] == ["1wwhb0c", "1wwf93d", "1wwc2k8"]  # the r/dub post is dropped
    video, album, question = items
    assert video.title == "Major Mackerel - Bad Ras" and video.meta["flair"] == "Official"
    assert video.meta["link"] == "https://v.redd.it/7k2qz0xw4jsf1" and video.meta["domain"] == "v.redd.it"
    assert album.title == "Sly & Robbie x Lone Ark – Showcase Vol. 2 (full album)"
    assert album.meta["link"] == "https://www.youtube.com/watch?v=Qm2xV8pT0aE&list=OLAK5uy_kq2"
    assert album.meta["flair"] == ":vinyl: New Release"
    assert question.summary == "Been looping Same Song all week. Who else should I check out?"
    # the subreddit check ignores case, as Arctic Shift does
    assert len(arctic.parse_posts(fixture("arctic_reggae.json"), "Reggae").items) == 3


def _page(newest: int, count: int, step: int = 60) -> bytes:
    return json.dumps({"data": [
        {"id": f"p{newest - k * step:x}", "title": f"post {newest - k * step}", "subreddit": "dub",
         "created_utc": newest - k * step, "permalink": f"/r/dub/comments/p{newest - k * step:x}/post/",
         "is_self": False, "url": "https://youtu.be/x", "domain": "youtu.be", "score": 1, "num_comments": 0}
        for k in range(count)]}).encode()


async def test_arctic_pages_until_a_short_page():
    first_oldest = BEFORE - 1 - 99 * 60
    second_oldest = first_oldest - 99 * 60

    def answer(call):
        before = call["params"]["before"]
        if before == BEFORE:
            return _page(BEFORE - 1, 100)
        if before == first_oldest:
            return _page(first_oldest, 100)  # starts with the last post of the first page again
        if before == second_oldest:
            return _page(second_oldest - 60, 30)
        raise AssertionError(f"unexpected before={before}")

    http = FakeHttp([("GET", ARCTIC_URL, answer)])
    result = await arctic.fetch(http, SRC["reddit_dub"], {}, NOW)
    assert [c["params"]["before"] for c in http.calls] == [BEFORE, first_oldest, second_oldest]
    assert all(c["params"]["after"] == AFTER and c["params"]["limit"] == 100 for c in http.calls)
    ids = [i.ext_id for i in result.items]
    assert len(ids) == len(set(ids)) == 100 + 99 + 30


async def test_arctic_pages_at_most_five_times():
    http = FakeHttp([("GET", ARCTIC_URL, lambda call: _page(call["params"]["before"] - 1, 100, step=10))])
    result = await arctic.fetch(http, SRC["reddit_dub"], {}, NOW)
    assert len(http.calls) == arctic.MAX_PAGES == 5
    assert len(result.items) == 500


async def test_arctic_stops_when_the_page_does_not_move_back():
    # A server that ignores before= would loop forever.
    http = FakeHttp([("GET", ARCTIC_URL, _page(BEFORE - 1, 100))])
    result = await arctic.fetch(http, SRC["reddit_dub"], {}, NOW)
    assert len(http.calls) == 2 and len(result.items) == 100


async def test_arctic_empty_window():
    http = FakeHttp([("GET", ARCTIC_URL, fixture("arctic_empty.json"))])
    result = await arctic.fetch(http, SRC["reddit_psychedelictherapy"], {}, NOW)
    assert result.items == [] and len(http.calls) == 1


@pytest.mark.parametrize("body, reason", [
    (b"<html><body>502 Bad Gateway</body></html>", "not JSON"),
    (fixture("arctic_error.json"), "Timeout. Maybe slow down a bit"),
    (b'{"data": {"id": "x"}}', "no data list"),
    (b"[]", "unexpected response"),
])
async def test_arctic_bad_responses(body, reason):
    http = FakeHttp([("GET", ARCTIC_URL, body)])
    with pytest.raises(ValueError, match=re.escape(reason)):
        await arctic.fetch(http, SRC["reddit_psychonaut"], {}, NOW)


async def test_arctic_http_errors_and_bad_subreddit():
    http = FakeHttp([("GET", ARCTIC_URL, HttpError(429, ARCTIC_SHIFT, retry_after=30))])
    with pytest.raises(HttpError):
        await arctic.fetch(http, SRC["reddit_psychonaut"], {}, NOW)
    bad = dataclasses.replace(SRC["reddit_psychonaut"], params={"subreddit": "Psychonaut&fields=id"})
    http = FakeHttp([("GET", ARCTIC_URL, fixture("arctic_empty.json"))])
    with pytest.raises(ValueError, match="subreddit"):
        await arctic.fetch(http, bad, {}, NOW)
    assert http.calls == []


def test_arctic_markdown_leftovers_and_query_strings():
    # Spoilers, links to URLs with parentheses and nested quotes leave no Markdown behind; "&amp;section="
    # in a URL becomes "&section=", not "§ion=" (html.unescape decodes "&sect" without its semicolon).
    body = json.dumps({"data": [{
        "id": "c1", "title": "Dub &amp; roots, raw &region=eu stays", "subreddit": "dub", "created_utc": BEFORE - 100,
        "is_self": False, "url": "https://example.org/a?x=1&amp;section=news&amp;region=eu",
        "selftext": "&gt;!spoiler here!&lt; and [Psilocybin](https://en.wikipedia.org/wiki/Psilocybin_(drug)) wiki"
                    "\n\n&gt;&gt; nested quote\n\nsee https://example.org/a?x=1&amp;section=news&amp;param=2"
                    "&amp;currency=EUR &amp;amp; more",
    }, {
        "id": "c2", "title": "Raw link", "subreddit": "dub", "created_utc": BEFORE - 200, "is_self": False,
        "url": "https://example.org/b?x=1&section=news&notify=1",
    }]}).encode()
    item, raw = arctic.parse_posts(body, "dub").items
    assert item.title == "Dub & roots, raw &region=eu stays"
    assert item.meta["link"] == "https://example.org/a?x=1&section=news&region=eu"
    assert item.summary == ("spoiler here and Psilocybin wiki nested quote "
                            "see https://example.org/a?x=1&section=news&param=2&currency=EUR & more")
    assert raw.meta["link"] == "https://example.org/b?x=1&section=news&notify=1"


def test_arctic_entities_of_pasted_titles_are_decoded_to_the_end():
    # Seen live on r/reggae: the title already held "&#39;", the API escaped it once more.
    body = json.dumps({"data": [{
        "id": "f1", "title": "It&amp;#39;s Bandcamp Friday &amp;amp; more, raw &region=eu", "subreddit": "reggae",
        "created_utc": BEFORE - 100, "is_self": False, "link_flair_text": "Bandcamp &amp;amp; Friday",
        "url": "https://e.org/a?x=1&amp;section=news&amp;copy2026=1",
    }, {
        "id": "f2", "title": "Tickets 20&amp;#8364; at the door", "subreddit": "reggae", "created_utc": BEFORE - 200,
        "is_self": True, "score": 40, "selftext": "Write &amp;amp;#64;someone &amp;regex; &amp;sect;ion &amp;amp;#x200B;",
    }]}).encode()
    pasted, tickets = arctic.parse_posts(body, "reggae").items
    assert pasted.title == "It's Bandcamp Friday & more, raw &region=eu"
    assert pasted.meta["flair"] == "Bandcamp & Friday"
    assert pasted.meta["link"] == "https://e.org/a?x=1&section=news&copy2026=1"  # a link keeps the API's one level
    assert tickets.title == "Tickets 20€ at the door"
    assert tickets.summary == "Write @someone &regex; §ion"   # exact names only; the zero-width space is gone
    assert drop_reason(tickets, SRC["reddit_reggae"]) == "trade"


def _self_post(post_id: str, selftext: str, **kw) -> dict:
    return {"id": post_id, "title": "Integration circle", "subreddit": "PsychedelicTherapy", "created_utc": BEFORE - 100,
            "is_self": True, "score": 50, "selftext": selftext, **kw}


def test_arctic_filters_read_the_whole_selftext():
    body = json.dumps({"data": [_self_post("d1", LONG_POST.replace(". ", ".\n\n**Note:** ", 3))]}).encode()
    item, = arctic.parse_posts(body, "PsychedelicTherapy").items
    assert len(item.summary) <= arctic.SUMMARY_MAX and TRADE_TAIL not in item.summary
    assert item.full_text.endswith(TRADE_TAIL) and "**" not in item.full_text
    assert drop_reason(item, SRC["reddit_psychedelictherapy"]) == "trade"
    # removed posts have no text at all
    gone, = arctic.parse_posts(json.dumps({"data": [_self_post("d2", "[removed]")]}).encode(),
                               "PsychedelicTherapy").items
    assert gone.summary == gone.full_text == ""


def test_arctic_keeps_link_targets_for_the_blocklist():
    # _markdown keeps only the link text: "join here" says nothing of where it leads.
    posts = [
        _self_post("e1", "Great albums, [join here](https://t.me/reggae_music_albums) and [wiki]"
                         "(https://en.wikipedia.org/wiki/Psilocybin_(drug))"),
        _self_post("e2", "Better than [this channel](https://t.me/microdosetoday)!"),
        _self_post("e3", "Bare and escaped: t.me/reggae\\_music\\_albums and telegram.me/s/GrowBud"),
        _self_post("e4", " ".join(f"[{k}](https://example.org/{k})" for k in range(12)) + " [x](https://t.me/growbud)"),
        _self_post("e5", "No links, just [brackets] and (parentheses)"),
    ]
    items = _by_id(arctic.parse_posts(json.dumps({"data": posts}).encode(), "PsychedelicTherapy").items)
    assert items["e1"].meta["links"] == ["https://t.me/reggae_music_albums",
                                         "https://en.wikipedia.org/wiki/Psilocybin_(drug)"]
    assert items["e1"].summary == "Great albums, join here and wiki"
    assert items["e2"].meta["links"] == ["https://t.me/microdosetoday"]
    assert items["e3"].meta["links"] == ["https://t.me/reggae_music_albums", "https://telegram.me/s/GrowBud"]
    # the cap holds for other links, never for Telegram ones
    assert items["e4"].meta["links"] == [*(f"https://example.org/{k}" for k in range(arctic.LINKS_MAX)),
                                         "https://t.me/growbud"]
    assert "links" not in items["e5"].meta
    source = SRC["reddit_psychedelictherapy"]
    assert [drop_reason(items[k], source) for k in ("e1", "e2", "e3", "e4", "e5")] == ["blocklist"] * 4 + [None]


def test_arctic_hostile_selftext_is_cheap():
    # Untrusted Markdown of up to 40 000 chars: "[[[[..." alone took 6 s of CPU on the event loop.
    texts = ["[" * 40000, "_a " * 13000, "*a " * 13000, "&gt;!" * 8000, "&lt;!--" * 5700, "[a](" + "x" * 40000,
             " ".join(f"t.me/c{k}" for k in range(5000)), "[a](t.me/" * 5000]
    body = json.dumps({"data": [{"id": f"h{k}", "title": "t", "subreddit": "dub", "created_utc": BEFORE - 100,
                                 "is_self": True, "selftext": text} for k, text in enumerate(texts)]}).encode()
    start = time.perf_counter()
    items = arctic.parse_posts(body, "dub").items
    assert len(items) == len(texts) and time.perf_counter() - start < 3
    assert all(len(i.summary) <= arctic.SUMMARY_MAX for i in items)


# ---- Telegram channel preview ----

TME_FIRST = rf"^https://t\.me/s/{PSV}$"


async def test_tme_first_page_and_cursor():
    http = FakeHttp([("GET", TME_FIRST, fixture("tme_channel.html"))])
    result = await tme.fetch(http, telegram_source(PSV), {}, NOW)
    assert http.calls[0]["url"] == f"https://t.me/s/{PSV}" and http.calls[0]["params"] == {}
    # 257 is the highest id; the service post 250 and the photo 252 count as seen too
    assert result.cursor == {"last_id": 257}
    assert [i.ext_id for i in result.items] == ["251", "254", "255", "257"]


async def test_tme_after_cursor():
    http = FakeHttp([("GET", rf"^https://t\.me/s/{PSV}\?after=249$", fixture("tme_channel.html"))])
    result = await tme.fetch(http, telegram_source(PSV), {"last_id": 249}, NOW)
    assert http.calls[0]["params"] == {"after": 249}
    assert result.cursor == {"last_id": 257}


async def test_tme_cursor_kept_when_nothing_is_new():
    http = FakeHttp([("GET", r"\?after=257$", fixture("tme_after_empty.html"))])
    result = await tme.fetch(http, telegram_source(PSV), {"last_id": 257}, NOW)
    assert result.items == [] and result.cursor == {"last_id": 257}

    # The preview ignored after= and sent the newest page: the cursor never goes back.
    http = FakeHttp([("GET", r"\?after=300$", fixture("tme_channel.html"))])
    result = await tme.fetch(http, telegram_source(PSV), {"last_id": 300}, NOW)
    assert len(result.items) == 4 and result.cursor == {"last_id": 300}

    # No cursor and no posts: nothing to keep.
    http = FakeHttp([("GET", TME_FIRST, fixture("tme_after_empty.html"))])
    result = await tme.fetch(http, telegram_source(PSV), {}, NOW)
    assert result.items == [] and result.cursor is None

    # A broken cursor is ignored, not sent.
    http = FakeHttp([("GET", TME_FIRST, fixture("tme_channel.html"))])
    result = await tme.fetch(http, telegram_source(PSV), {"last_id": "abc"}, NOW)
    assert http.calls[0]["params"] == {} and result.cursor == {"last_id": 257}


def test_tme_items():
    items = _by_id(tme.parse_page(fixture("tme_channel.html"), PSV).items)
    assert "250" not in items  # service message "Channel photo updated"
    assert "252" not in items  # photo without text
    for item in items.values():
        _plain_text(item)
        assert item.lang == "" and item.url == f"https://t.me/{PSV}/{item.ext_id}"
        assert item.meta["channel"] == PSV and item.published_at.tzinfo == UTC

    lecture = items["251"]
    # <br> separates the title line; &quot; and &amp; are unescaped; the emoji image keeps its character
    assert lecture.title == 'Lecture: "The Anarchic Brain" & psychedelic neuroscience'
    assert lecture.summary == ('Lecture: "The Anarchic Brain" & psychedelic neuroscience Join us on Thursday for a '
                               "talk on the REBUS model with Q&A afterwards. Entry 5€, members free. 📍Lecture hall "
                               "C1, Spitalgasse 2 #lecture #neuroscience")
    assert lecture.published_at == datetime(2026, 8, 22, 13, 53, 10, tzinfo=UTC)
    assert lecture.meta == {"channel": PSV, "forwarded": False}  # hashtag links (?q=%23...) are not links

    forward = items["254"]
    assert forward.meta == {
        "channel": PSV, "forwarded": True, "forwarded_from": "https://t.me/mapsorg/1187",
        # the preview's "&amp;amp;" in the href undone; the link preview's href too
        "links": ["https://maps.org/2026/09/20/mdma-long-term-follow-up/?utm_source=telegram&utm_medium=channel",
                  "https://maps.org/2026/09/20/mdma-long-term-follow-up/"],
    }
    assert forward.title == "New paper: MDMA-assisted therapy for PTSD — long-term follow-up published"
    assert "Participants were followed" not in forward.summary  # the link preview is not the post's text

    reply = items["255"]
    assert reply.summary == "Update: the lecture moves to Sunday, 19.07.2026 — same room."  # without the quote

    circle = items["257"]
    # leading empty lines skipped, a double-escaped "&amp;amp;" unescaped, +02:00 converted to UTC
    assert circle.title.startswith("Sharing circle this Friday at the Donauinsel, bring a blanket & something")
    assert len(circle.title) <= tme.TITLE_MAX and circle.title.endswith("…")
    assert circle.summary.endswith("ring the bell marked PSV. Everyone is welcome — first-timers too.")
    assert circle.published_at == datetime(2026, 10, 1, 8, 5, tzinfo=UTC)


def test_tme_long_posts_and_foreign_posts():
    first = "Ayahuasca integration talk " * 10
    body = f"""<html><body><section class="tgme_channel_history">
      <div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="{PSV}/300">
        <div class="tgme_widget_message_text js-message_text">{first}<br>{'word ' * 400}</div>
        <a class="tgme_widget_message_date" href="https://t.me/{PSV}/300"><time datetime="bad">x</time></a>
      </div></div>
      <div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="{PSV}/302">
        <div class="tgme_widget_message_forwarded_from">Forwarded from&nbsp;<span
          class="tgme_widget_message_forwarded_from_name">Anon Admin</span></div>
        <div class="tgme_widget_message_text">Shop is open, <a href="https://t.me/microdosetoday/12">DM</a>
          <a href="javascript:alert(1)">x</a> <a href="tg://resolve?domain=x">y</a></div></div></div>
      <div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="otherchannel/999">
        <div class="tgme_widget_message_text">Not ours</div></div></div>
      <div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="{PSV}/abc">
        <div class="tgme_widget_message_text">No id</div></div></div>
      <div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="{PSV}/301">
        <div class="tgme_widget_message_text">   <br>  </div></div></div>
    </section></body></html>"""
    page = tme.parse_page(body, PSV)
    assert page.max_id == 302 and [i.ext_id for i in page.items] == ["300", "302"]
    item, repost = page.items
    # a forward without a public source keeps the name; only http(s) links are kept for the filters
    assert repost.meta == {"channel": PSV, "forwarded": True, "forwarded_from": "Anon Admin",
                           "links": ["https://t.me/microdosetoday/12"]}
    assert len(item.title) <= 120 and item.title.startswith("Ayahuasca integration talk") and item.title.endswith("…")
    assert len(item.summary) <= 1500 and item.summary.endswith("…")
    assert item.published_at is None


async def test_tme_no_preview():
    # A gone channel (here @reggae_dub) is a "Contact @name" card, like a user or a bot.
    http = FakeHttp([("GET", r"^https://t\.me/s/reggae_dub$", fixture("tme_contact.html"))])
    with pytest.raises(ValueError, match="^no_preview$"):
        await tme.fetch(http, telegram_source("reggae_dub"), {}, NOW)
    # A channel with the preview disabled shows "View in Telegram" and "Preview channel" instead of posts.
    with pytest.raises(ValueError, match="^no_preview$"):
        tme.parse_page(fixture("tme_preview_disabled.html"), "reggaefoundation")


def test_tme_not_a_channel_page():
    with pytest.raises(ValueError) as e:
        tme.parse_page(b"<html><body><h1>502 Bad Gateway</h1></body></html>", PSV)
    assert str(e.value) != "no_preview"
    with pytest.raises(ValueError):
        tme.parse_page(b"", PSV)


async def test_tme_blocked_and_malformed_channels():
    for channel in ("reggaefoundation", "Reggae_Music_Albums", "bad name", "ab", None):
        source = Source("tg_x", "Telegram", "tme", "https://t.me/s/x", "community", every_h=1, mode="community",
                        lang="", params={"channel": channel})
        http = FakeHttp([("GET", r".", fixture("tme_channel.html"))])
        with pytest.raises(ValueError):
            await tme.fetch(http, source, {}, NOW)
        assert http.calls == []


def _tme_page(*posts: str) -> str:
    return '<html><body><section class="tgme_channel_history">' + "".join(posts) + "</section></body></html>"


def _tme_post(post_id: int, inner: str, when: str = "2026-10-01T10:05:00+00:00") -> str:
    return (f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="{PSV}/{post_id}">'
            f'{inner}<a class="tgme_widget_message_date" href="https://t.me/{PSV}/{post_id}">'
            f'<time datetime="{when}">10:05</time></a></div></div>')


def test_tme_entities_in_links_and_text():
    # An href escaped once ("&amp;section=") or twice ("&amp;amp;section=") gives "&section=", never "§ion=";
    # text unescaped a second time keeps "&copy2026" and "&para=1" (no semicolon: not entities).
    body = _tme_page(_tme_post(400, (
        '<div class="tgme_widget_message_text js-message_text">Q&amp;A &amp;amp; more, &amp;copy2026 and '
        '&amp;para=1 <a href="https://example.org/?a=1&amp;section=news&amp;region=eu">link</a></div>'
        '<a class="tgme_widget_message_link_preview" href="https://example.org/?a=1&amp;amp;section=news">'
        '<div class="link_preview_title">Preview</div></a>')))
    item, = tme.parse_page(body, PSV).items
    assert item.summary == "Q&A & more, &copy2026 and &para=1 link"
    assert item.meta["links"] == ["https://example.org/?a=1&section=news&region=eu",
                                  "https://example.org/?a=1&section=news"]


def test_tme_text_and_forward_label_are_decoded_to_the_end():
    # A third level of escaping in the text, and in the label of a forward without a link: "€" and "@"
    # must reach the trade and blocklist patterns as themselves. Unknown names stay as written.
    body = _tme_page(_tme_post(420, (
        '<div class="tgme_widget_message_forwarded_from">Forwarded from <span class="tgme_widget_message_'
        'forwarded_from_name">Grow &amp;amp;amp; Co &amp;amp;#64;growbud</span></div>'
        '<div class="tgme_widget_message_text">Eintritt &amp;amp;amp;#8364;20 &amp;amp;regex; '
        '&amp;amp;copy2026</div>')))
    item, = tme.parse_page(body, PSV).items
    assert item.title == "Eintritt €20 &regex; &copy2026"
    assert item.meta["forwarded_from"] == "Grow & Co @growbud"
    source = telegram_source(PSV)
    assert drop_reason(item, source) == "blocklist"
    clean = dataclasses.replace(item, meta={"channel": PSV, "forwarded": False})
    assert drop_reason(clean, source) == "trade"


def test_tme_filters_read_the_whole_post():
    body = _tme_page(_tme_post(410, f'<div class="tgme_widget_message_text">{LONG_POST.replace(". ", ".<br>", 5)}</div>'))
    item, = tme.parse_page(body, PSV).items
    assert len(LONG_POST) == 2188 and len(item.summary) <= tme.SUMMARY_MAX and TRADE_TAIL not in item.summary
    assert item.full_text.endswith(TRADE_TAIL) and item.full_text.startswith(item.title)
    assert drop_reason(item, telegram_source(PSV)) == "trade"


def test_tme_telegram_links_are_kept_past_the_cap():
    anchors = "".join(f'<a href="https://example.org/{k}">{k}</a> ' for k in range(tme.LINKS_MAX + 2))
    body = _tme_page(_tme_post(411, f'<div class="tgme_widget_message_text">Links: {anchors}'
                                    '<a href="https://t.me/growbud">more</a></div>'))
    item, = tme.parse_page(body, PSV).items
    assert item.meta["links"] == [*(f"https://example.org/{k}" for k in range(tme.LINKS_MAX)), "https://t.me/growbud"]
    assert drop_reason(item, telegram_source(PSV)) == "blocklist"


def test_tme_bad_times_and_nested_posts():
    text = '<div class="tgme_widget_message_text">{}</div>'
    body = _tme_page(_tme_post(401, text.format("Far past"), "0001-01-01T00:00:00+01:00"),
                     _tme_post(402, text.format("Far future"), "9999-12-31T23:59:59-23:59"),
                     _tme_post(403, text.format("Fine")))
    page = tme.parse_page(body, PSV)   # an OverflowError in one post used to cost the page
    assert [(i.ext_id, i.published_at) for i in page.items] == [
        ("401", None), ("402", None), ("403", datetime(2026, 10, 1, 10, 5, tzinfo=UTC))]

    # Unclosed divs nest every post in the one before: a page we do not understand, not posts
    # carrying the text of all later ones (and minutes of CPU for a few hundred of them).
    nested = '<section class="tgme_channel_history">' + "".join(
        f'<div class="tgme_widget_message" data-post="{PSV}/{i}"><div class="tgme_widget_message_text">Hi {i}'
        for i in range(1, 3000)) + "</section>"
    start = time.perf_counter()
    with pytest.raises(ValueError, match="nested"):
        tme.parse_page(nested, PSV)
    assert time.perf_counter() - start < 3


def test_tme_long_posts_are_cheap_and_the_cap_keeps_the_cursor():
    # One pass over the text: replacing 20 000 <br> one by one took 9 s.
    start = time.perf_counter()
    item, = tme.parse_page(_tme_page(_tme_post(500, '<div class="tgme_widget_message_text">'
                                               + "line<br/>" * 20000 + "</div>")), PSV).items
    assert time.perf_counter() - start < 3
    assert item.title == "line" and len(item.summary) <= tme.SUMMARY_MAX
    # More posts than a preview page holds: the cursor stops at the last post read, the next run gets the rest.
    posts = [_tme_post(i, f'<div class="tgme_widget_message_text">Post {i}</div>') for i in range(1, 61)]
    page = tme.parse_page(_tme_page(*posts), PSV)
    assert len(page.items) == tme.MESSAGES_MAX and page.max_id == tme.MESSAGES_MAX


# ---- Reggaeville ----

def _rv(items) -> dict:
    return {i.url.removeprefix("https://www.reggaeville.com/artist-details/"): i for i in items}


async def test_reggaeville_news():
    source = SRC["reggaeville_news"]
    http = FakeHttp([("GET", r"^https://www\.reggaeville\.com/news/$", fixture("reggaeville_news.html"))])
    result = await reggaeville.fetch(http, source, {}, NOW)
    assert [c["url"] for c in http.calls] == ["https://www.reggaeville.com/news/"]
    assert result.cursor is None
    urls = [i.url for i in result.items]
    assert urls == [
        "https://www.reggaeville.com/artist-details/lake-anthony/news/view/"
        "lake-anthony-unites-global-reggae-artists-for-world-needs-love/",
        "https://www.reggaeville.com/artist-details/marcia-griffiths/news/view/marcia-griffiths-announces-farewell-tour-2027/",
        "https://www.reggaeville.com/artist-details/protoje/news/view/protoje-drops-new-single-in-search-of-lost-time/",
        "https://www.reggaeville.com/artist-details/lieutenant-stitchie/news/view/lieutenant-stitchie-rest-in-power/",
        "https://www.reggaeville.com/dates/festival-details/city-splash-2027/news/news/"
        "damian-marley-headlines-city-splash-2027/",
        "https://www.reggaeville.com/artist-details/sly-robbie/news/view/sly-amp-robbie-taxi-gang-reunion/",
    ]  # no release from the sidebar, no Facebook link, no "show more" eID link
    for item in result.items:
        _plain_text(item)
        assert item.ext_id == item.url and item.summary == "" and item.lang == "en" and item.meta == {"page": "news"}
        assert "?" not in item.url and "#" not in item.url


def test_reggaeville_duplicate_hrefs_and_titles():
    items = _rv(reggaeville.parse_page(fixture("reggaeville_news.html"), "news", "https://www.reggaeville.com/news/"))
    # image link (alt "Lake Anthony"), featured image link and title link: the title link's text wins;
    # TYPO3 escaped this title twice over ("&amp;amp;#39;", seen live), every level is decoded
    lake = items["lake-anthony/news/view/lake-anthony-unites-global-reggae-artists-for-world-needs-love/"]
    assert lake.title == "Lake Anthony Unites Global Reggae Artists For 'World Needs Love'"
    # absolute with ?utm_*#comments and relative to <base href> without the trailing slash: one item
    protoje = items["protoje/news/view/protoje-drops-new-single-in-search-of-lost-time/"]
    assert protoje.title == 'Protoje Drops New Single "In Search Of Lost Time"'
    # "read more »" is not a title: the image's alt is
    assert items["lieutenant-stitchie/news/view/lieutenant-stitchie-rest-in-power/"].title == (
        "Lieutenant Stitchie - Rest In Power")
    # an anchor around a whole card: its heading, not the teaser; entities unescaped
    city = next(i for i in items.values() if "city-splash" in i.url)
    assert city.title == "Damian Marley Headlines City Splash 2027 & More Acts Confirmed"
    # protocol-relative with a double slash, double-escaped "&amp;amp;"
    assert items["sly-robbie/news/view/sly-amp-robbie-taxi-gang-reunion/"].title == (
        "Sly & Robbie Announce Taxi Gang Reunion")


def test_reggaeville_dates():
    items = reggaeville.parse_page(fixture("reggaeville_news.html"), "news", "https://www.reggaeville.com/news/")
    days = {i.url.split("/")[4]: (i.published_at and i.published_at.date().isoformat()) for i in items}
    assert days == {
        "lake-anthony": "2026-09-30",        # also featured above the first header: the list date counts
        "marcia-griffiths": None,            # only above the first header
        "protoje": "2026-09-30",
        "lieutenant-stitchie": "2026-09-28",
        "festival-details": "2026-09-25",    # /dates/festival-details/city-splash-2027/news/news/...
        "sly-robbie": "2026-09-25",          # the script's "10/03/2026" after the list is not a header
    }
    assert all(i.published_at is None or (i.published_at.hour, i.published_at.tzinfo) == (12, UTC) for i in items)


async def test_reggaeville_releases():
    source = SRC["reggaeville_releases"]
    http = FakeHttp([("GET", r"^https://www\.reggaeville\.com/music/releases/$", fixture("reggaeville_releases.html"))])
    result = await reggaeville.fetch(http, source, {}, NOW)
    got = [(i.title, i.published_at.date().isoformat()) for i in result.items]
    assert got == [
        ("Tarrus Riley - Red Flag [Single]", "2026-10-02"),
        ("Lone Ark x The 18th Parallel - Showcase, Vol. 2", "2026-10-02"),
        ("Takana Zion - Few Words", "2026-10-02"),
        ("Jahcoustix - Footprints", "2026-10-01"),         # "Release Date 10/16/2026" is not a header
        ("Mad Professor & Jah Shaka - Dub From The Roots", "2026-10-01"),
    ]  # the news link in the sidebar belongs to the other page
    assert all(i.meta == {"page": "releases"} and "/releases/release/" in i.url for i in result.items)
    assert result.items[1].url == ("https://www.reggaeville.com/artist-details/reggaeville/releases/release/"
                                   "lone-ark-x-the-18th-parallel-showcase-vol-2/")


@pytest.mark.parametrize("body", [fixture("reggaeville_redesign.html"), b"", b"<html><body>Maintenance</body></html>"])
async def test_reggaeville_layout_changed(body):
    http = FakeHttp([("GET", r"reggaeville", body)])
    with pytest.raises(ValueError, match="^layout changed$"):
        await reggaeville.fetch(http, SRC["reggaeville_news"], {}, NOW)


def test_reggaeville_links_without_text_and_bad_headers():
    body = """<html><head><base href="http://evil.example/"></head><body>
      <div class="top"><a href="/artist-details/z/news/view/dated-below/">
        Dated Below: The Whole Headline (Featured)</a></div>
      <div>13/45/2026</div>
      <a href="/artist-details/x/news/view/no-text/"><img src="/a.jpg" alt=""></a>
      <a href="/artist-details/x/news/view/symbols/">»</a>
      <div>10/03/2026</div>
      <a href="https://reggaeville.com/artist-details/y/news/view/bare-host/">Bare host</a>
      <a href="http://evil.example/artist-details/y/news/view/elsewhere/">Elsewhere</a>
      <a href="javascript:void(0)">/news/view/js/</a>
      <div>10/02/2026</div>
      <a href="/artist-details/z/news/view/dated-below/">Dated Below</a>
    </body></html>"""
    items = reggaeville.parse_page(body, "news")
    assert [(i.url, i.title, i.published_at) for i in items] == [
        # the longer text wins; the date comes from the occurrence under a header when the title's has none
        ("https://www.reggaeville.com/artist-details/z/news/view/dated-below/",
         "Dated Below: The Whole Headline (Featured)", datetime(2026, 10, 2, 12, tzinfo=UTC)),
        ("https://www.reggaeville.com/artist-details/y/news/view/bare-host/", "Bare host",
         datetime(2026, 10, 3, 12, tzinfo=UTC)),
    ]  # links without a usable title are skipped, but they do not mean the layout changed; the off-site base is ignored
    broken_base = '<base href="http://[::1"><div>10/02/2026</div><a href="/artist-details/q/news/view/q/">Q</a>'
    assert [i.url for i in reggaeville.parse_page(broken_base, "news")] == [
        "https://www.reggaeville.com/artist-details/q/news/view/q/"]


async def test_reggaeville_unknown_page():
    bad = dataclasses.replace(SRC["reggaeville_news"], params={"page": "tours"})
    http = FakeHttp([("GET", r".", fixture("reggaeville_news.html"))])
    with pytest.raises(ValueError, match="unknown page"):
        await reggaeville.fetch(http, bad, {}, NOW)
    assert http.calls == []


async def test_reggaeville_relative_links_use_the_final_url():
    # Without <base href>, relative links resolve against the page the request ended on.
    body = b'<html><body><div>09/30/2026</div><a href="view/some-story/">Some Story</a></body></html>'
    http = FakeHttp([("GET", r".", Response(200, body, "https://www.reggaeville.com/news/"))])
    result = await reggaeville.fetch(http, SRC["reggaeville_news"], {}, NOW)
    assert [i.url for i in result.items] == ["https://www.reggaeville.com/news/view/some-story/"]


def test_reggaeville_dates_inside_cards_and_unclosed_links():
    # A card link with its own date: that date (it was the previous card's), and not in the title.
    cards = """<div class="news-date">10/04/2026</div>
      <a class="card" href="/artist-details/a/news/view/a/"><span class="date">10/03/2026</span><h3>Story A</h3></a>
      <a class="card" href="/artist-details/b/news/view/b/"><h3>Story B</h3><span>10/01/2026</span></a>
      <a class="card" href="/artist-details/c/news/view/c/"><span>09/30/2026</span> Story C &amp;amp; Co</a>
      <a class="card" href="/artist-details/d/news/view/d/"><h3>Story D &amp;copy2026</h3></a>"""
    got = [(i.title, i.published_at.date().isoformat()) for i in reggaeville.parse_page(cards, "news")]
    assert got == [("Story A", "2026-10-03"), ("Story B", "2026-10-01"), ("Story C & Co", "2026-09-30"),
                   ("Story D &copy2026", "2026-09-30")]

    # Every level the parser left is decoded, from the anchor and from an img alt alike; only exact names
    # count ("&regex;" is not "&reg;" + "ex;").
    triple = """<div>10/04/2026</div>
      <h3><a href="/artist-details/x/news/view/x/">For &amp;amp;#39;World Needs Love&amp;amp;#39;</a></h3>
      <a href="/artist-details/y/news/view/y/"><img alt="For &amp;amp;#39;Alt Text&amp;amp;#39;"></a>
      <a href="/artist-details/z/news/view/z/">Raw &amp;amp;regex; &amp;amp;sect;ion</a>"""
    assert [i.title for i in reggaeville.parse_page(triple, "news")] == [
        "For 'World Needs Love'", "For 'Alt Text'", "Raw &regex; §ion"]

    # A template without </a>: html.parser nests every later link (and the next header) in the first one,
    # where a browser closes it. Each entry keeps its own text and the header before it.
    unclosed = """<div>10/02/2026</div>
      <a href="/artist-details/a/news/view/a/">Story A
      <a href="/artist-details/b/news/view/b/">Story B
      <div>10/01/2026</div>
      <a href="/artist-details/c/news/view/c/">Story C"""
    got = [(i.title, i.published_at.date().isoformat()) for i in reggaeville.parse_page(unclosed, "news")]
    assert got == [("Story A", "2026-10-02"), ("Story B", "2026-10-02"), ("Story C", "2026-10-01")]

    many = "".join(f'<a href="/artist-details/x/news/view/s{i}/">Story {i} ' for i in range(2000))
    start = time.perf_counter()
    items = reggaeville.parse_page(many, "news")   # walking each nested link again took 5 s
    assert time.perf_counter() - start < 3
    assert [i.title for i in items[:2]] == ["Story 0", "Story 1"] and len(items) == 2000


# ---- registry ----

def test_registry_points_to_these_adapters():
    try:
        from nestor.news.adapters import ADAPTERS
    except ImportError as e:  # a sibling adapter is not written yet
        pytest.skip(str(e))
    assert ADAPTERS["arctic"] is arctic.fetch
    assert ADAPTERS["tme"] is tme.fetch
    assert ADAPTERS["reggaeville"] is reggaeville.fetch
    kinds = {s.kind for s in SOURCES}
    assert {"arctic", "reggaeville"} <= kinds
    for source in SOURCES:
        if source.kind == "arctic":
            assert arctic._SUB.fullmatch(source.params["subreddit"])
        if source.kind == "reggaeville":
            assert source.params["page"] in reggaeville.PATTERNS
