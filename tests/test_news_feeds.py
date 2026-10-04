"""The generic RSS/Atom/RDF adapter on saved feeds (tests/news_fixtures/feeds_*), without network
or database."""
from __future__ import annotations

import dataclasses
import importlib
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

import nestor.news
from nestor.news.http import HttpError, Response
from nestor.news.model import FetchResult, Source
from nestor.news.sources import SOURCES
from tests.news_helpers import FakeHttp, fixture


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


feeds = _adapter("feeds")

UTC = timezone.utc
NOW = datetime(2026, 10, 4, 18, 30, tzinfo=UTC)
SRC = {s.key: s for s in SOURCES}
GENERIC = Source("test_feed", "Test", "rss", "https://example.org/feed/", None, every_h=1)
# Every kind "rss" source of the registry and the saved feed it is tested on.
FIXTURE_OF = {
    "psypost": "feeds_psypost.xml",
    "sciencedaily_marijuana": "feeds_sciencedaily.xml",
    "sciencedaily_psychedelics": "feeds_sciencedaily.xml",
    "marijuanamoment": "feeds_marijuanamoment.xml",
    "norml_news": "feeds_norml_news.xml",
    "norml_blog": "feeds_norml_blog.xml",
    "leafly_politics": "feeds_leafly_politics.xml",
    "hightimes": "feeds_hightimes.xml",
    "psychedelicalpha": "feeds_psychedelicalpha.xml",
    "lucidnews": "feeds_lucidnews.xml",
    "maps": "feeds_maps.xml",
    "worldareggae": "feeds_worldareggae.xml",
    "dailyreggae": "feeds_dailyreggae.xml",
    "nplus1": "feeds_nplus1.xml",
    "knife": "feeds_knife.xml",
    "meduza": "feeds_meduza.xml",
}


def _parse(name: str, source: Source = GENERIC):
    return feeds.parse(fixture(name), source)


def _by_url(items) -> dict:
    return {i.url: i for i in items}


def _rss(items_xml: str, encoding: str = "UTF-8") -> bytes:
    return (f'<?xml version="1.0" encoding="{encoding}"?><rss version="2.0" '
            f'xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>t</title>'
            f"<link>https://example.org/</link>{items_xml}</channel></rss>").encode(encoding)


# ---- fetch ----

def test_registry_rss_sources_all_have_a_fixture():
    assert {s.key for s in SOURCES if s.kind == "rss"} == set(FIXTURE_OF)


@pytest.mark.parametrize("key", sorted(FIXTURE_OF))
async def test_fetch_every_rss_source(key):
    source = SRC[key]
    http = FakeHttp([("GET", "^" + re.escape(source.url), fixture(FIXTURE_OF[key]))])
    result = await feeds.fetch(http, source, {}, NOW)
    assert isinstance(result, FetchResult) and result.cursor is None and not result.skipped
    assert len(http.calls) == 1 and http.calls[0]["method"] == "GET"
    assert result.items
    for item in result.items:
        assert item.url.startswith(("https://", "http://")) and "utm_" not in item.url and "#" not in item.url
        assert item.title and len(item.title) <= feeds.TITLE_MAX and "<" not in item.title
        assert "&#" not in item.title and "&amp;" not in item.title
        assert len(item.summary) <= feeds.SUMMARY_MAX and "<p" not in item.summary
        assert item.published_at is not None and item.published_at.utcoffset().total_seconds() == 0
        assert item.lang == (source.lang or "en")
        assert all(c == c.lower().strip() and c for c in item.categories)
        assert item.ext_id
    assert len({i.ext_id for i in result.items}) == len(result.items)


async def test_fetch_requests_the_feed_url_as_is():
    http = FakeHttp([("GET", r".", fixture("feeds_marijuanamoment.xml"))])
    await feeds.fetch(http, SRC["marijuanamoment"], {}, NOW)
    assert http.calls[0]["url"] == "https://www.marijuanamoment.net/feed/"
    assert http.calls[0]["params"] == {}


async def test_fetch_cache_buster_appends_the_unix_hour():
    http = FakeHttp([("GET", r".", fixture("feeds_sciencedaily.xml"))])
    await feeds.fetch(http, SRC["sciencedaily_marijuana"], {}, NOW)
    assert http.calls[0]["url"] == "https://www.sciencedaily.com/rss/mind_brain/marijuana.xml?_=497538"
    assert int(NOW.timestamp()) // 3600 == 497538


async def test_fetch_cache_buster_on_a_url_with_a_query():
    source = dataclasses.replace(SRC["dailyreggae"], params={"cache_buster": True, "id_from_link": True})
    http = FakeHttp([("GET", r".", fixture("feeds_dailyreggae.xml"))])
    result = await feeds.fetch(http, source, {}, NOW)
    assert http.calls[0]["url"] == "https://dailyreggae.com/news?format=rss&_=497538"
    assert len(result.items) == 3


async def test_fetch_passes_http_errors_through():
    http = FakeHttp([("GET", r".", HttpError(503, "https://norml.org/feed/", retry_after=120))])
    with pytest.raises(HttpError) as e:
        await feeds.fetch(http, SRC["norml_blog"], {}, NOW)
    assert e.value.status == 503 and e.value.retry_after == 120


async def test_fetch_bad_body_is_a_value_error():
    http = FakeHttp([("GET", r".", Response(200, fixture("feeds_not_a_feed.html"), "https://hightimes.com/x"))])
    with pytest.raises(ValueError):
        await feeds.fetch(http, SRC["hightimes"], {}, NOW)


# ---- WordPress RSS 2.0 ----

def test_marijuana_moment_wordpress_content_encoded():
    items = _parse("feeds_marijuanamoment.xml", SRC["marijuanamoment"])
    assert len(items) == 2
    first = items[0]
    url = ("https://www.marijuanamoment.net/federal-changes-will-put-hemp-industry-operators-out-of-business-"
           "oregon-retailers-and-suppliers-say/")
    assert first.ext_id == url and first.url == url   # guid isPermaLink="false" is the article URL
    assert first.title == ("Federal Changes Will Put Hemp Industry Operators Out Of Business, Oregon Retailers "
                           "And Suppliers Say")
    assert first.published_at == datetime(2026, 10, 3, 16, 27, 23, tzinfo=UTC)
    assert first.categories == ("politics",)
    assert first.lang == "en" and first.doi is None and first.meta == {}
    # From content:encoded (the full post), not the one-line description; the WordPress footer is dropped.
    assert first.summary.startswith("Oregon hemp retailers and suppliers told state lawmakers")
    assert "“The proposed federal definition" in first.summary
    assert "appeared first on" not in first.summary and "The post" not in first.summary
    assert items[1].categories == ("politics", "pennsylvania")
    assert "voted 9–5 on Friday" in items[1].summary


def test_norml_news_guid_with_encoded_ampersand_and_description_only():
    items = _parse("feeds_norml_news.xml", SRC["norml_news"])
    assert [i.ext_id for i in items] == ["https://norml.org/?post_type=news&p=80801",
                                         "https://norml.org/?post_type=news&p=80655"]
    first = items[0]
    assert first.url == ("https://norml.org/news/2026/10/01/analysis-teen-marijuana-use-has-declined-"
                         "significantly-in-adult-use-legalization-states/")
    assert first.categories == ("2026", "high school", "teens")
    assert first.summary.startswith("Self-reported marijuana use by high-school students fell")
    assert "Addictive Behaviors. “These results" in first.summary and "NORML’s Deputy Director" in first.summary
    assert "<em>" not in first.summary


def test_norml_blog_description_starting_with_an_image():
    first = _parse("feeds_norml_blog.xml", SRC["norml_blog"])[0]
    assert first.summary.startswith("Voters in Massachusetts will decide in November")
    assert "i0.wp.com" not in first.summary and "img" not in first.summary
    assert first.summary.endswith("vote no on Question 2…")
    assert first.categories == ("blog", "elections", "massachusetts")


def test_high_times_utm_links_and_sponsored_category():
    items = _by_url(_parse("feeds_hightimes.xml", SRC["hightimes"]))
    jamaica = items["https://hightimes.com/news/world/jamaicas-cannabis-reform-opens-a-new-path-for-small-growers/"]
    assert jamaica.ext_id == "https://hightimes.com/?p=320307"
    assert jamaica.title == "Jamaica’s Cannabis Reform Opens a New Path for Small Growers"
    assert jamaica.categories == ("news", "business", "world")
    # The adapter returns sponsored posts too; filters.py drops them by params drop_categories.
    sponsored = items["https://hightimes.com/sponsored/3-asks-what-youd-do-if-you-knew-you-had-72-hrs-left/"]
    assert "sponsored" in sponsored.categories
    assert sponsored.title == "‘3’ Asks What You’d Do If You Knew You Had 72 Hrs Left"
    # fbclid and utm_* go, other parameters stay, the #fragment goes.
    assert ("https://hightimes.com/news/brazils-patient-associations-win-court-fight-over-home-cultivation/"
            "?amp_ref=feed") in items


def test_psychedelic_alpha_paid_marker_in_title_or_category():
    items = _parse("feeds_psychedelicalpha.xml", SRC["psychedelicalpha"])
    assert [i.meta.get("paid", False) for i in items] == [True, True, False]
    assert "pα+" in items[0].categories                      # marker only among the categories
    assert items[1].title.startswith("Pα+ Psychedelic Bulletin #232")   # marker only in the title
    assert "paid" not in items[2].meta


def test_paid_marker_needs_the_source_param():
    items = _parse("feeds_psychedelicalpha.xml", GENERIC)
    assert all("paid" not in i.meta for i in items)


def test_psypost_doi_from_content():
    items = _parse("feeds_psypost.xml", SRC["psypost"])
    by_id = {i.ext_id: i for i in items}
    # The paper is the last DOI of the post; an earlier one (with parentheses) is a cited review.
    assert by_id["https://www.psypost.org/?p=235435"].doi == "10.1001/jamapsychiatry.2026.2817"
    # dx.doi.org with a percent-encoded slash.
    assert by_id["https://www.psypost.org/?p=235437"].doi == "10.1177/02698811261234567"
    # "doi: 10.xxxx" in the text, followed by a closing bracket and a period.
    assert by_id["https://www.psypost.org/?p=235390"].doi == "10.1038/s41380-026-03312-7"
    first = by_id["https://www.psypost.org/?p=235435"]
    assert first.categories == ("cannabis", "cognitive science", "developmental psychology")
    assert first.summary.startswith("Adolescents who started using cannabis early")
    assert "Photo credit" not in first.summary          # figure captions are not text
    assert by_id["https://www.psypost.org/?p=235390"].url == (
        "https://www.psypost.org/semaglutide-users-report-fewer-alcohol-cravings-survey-finds/")


@pytest.mark.parametrize("html, doi", [
    ('<p>See https://doi.org/10.1016/S0140-6736(26)01234-5).</p>', "10.1016/s0140-6736(26)01234-5"),
    # Closing quotes as characters (Meduza, N+1 and many UTF-8 feeds do not use entities).
    ("<p>“doi: 10.1038/s41380-026-03312-7”.</p>", "10.1038/s41380-026-03312-7"),
    ("<p>Статья: «doi:10.1038/s41380-026-03312-7».</p>", "10.1038/s41380-026-03312-7"),
    ('<p><a href="https://doi.org/10.1093/ijnp/pyab012%E2%80%9D">a quote pasted into the link</a></p>',
     "10.1093/ijnp/pyab012"),
    ('<p><a href="https://doi.org/10.1093/ijnp/pyab012%20">a space in the link</a></p>', "10.1093/ijnp/pyab012"),
    ('<p>(<a href="https://doi.org/10.1002/jclp.23456?utm_source=x">paper</a>)</p>', "10.1002/jclp.23456"),
    ('<p><a href="https://journals.sagepub.com/doi/full/10.1177/02698811261111111">paper</a></p>',
     "10.1177/02698811261111111"),
    ("<p>DOI: 10.26828/cannabis/2026/000390.</p>", "10.26828/cannabis/2026/000390"),
    ("<p>doi:10.1000/ABC-1;</p>", "10.1000/abc-1"),
    ("<p>No identifier, just 10.1000 cases.</p>", None),
    ('<p><a href="https://doi.org/">broken</a> doi: 11.1000/x</p>', None),
])
def test_doi_forms(html, doi):
    body = _rss(f"<item><title>T</title><link>https://example.org/a</link>"
                f"<content:encoded><![CDATA[{html}]]></content:encoded></item>")
    assert feeds.parse(body, GENERIC)[0].doi == doi


def test_lucid_news_block_tags_become_spaces():
    first = _parse("feeds_lucidnews.xml", SRC["lucidnews"])[0]
    assert first.summary.startswith("A first for the agency Researchers, veterans and patients testified")
    assert "psychedelic-assisted therapy. “This is the first time" in first.summary
    assert "said. Day one: safety monitoring Day two: training of facilitators" in first.summary
    assert "mdma" in first.categories and "drug policy" in first.categories


def test_maps_nbsp_and_entity_channel():
    first = _parse("feeds_maps.xml", SRC["maps"])[0]
    assert "Schedule I and urges" in first.summary and "\xa0" not in first.summary
    assert first.categories == ("uncategorized",)


def test_world_a_reggae_dashes_and_ampersands_in_titles():
    items = _parse("feeds_worldareggae.xml", SRC["worldareggae"])
    assert items[0].title == "Perfect Creation – Micah Shemaiah w/ Rapha Pico"
    assert items[1].title == "Mortimer & Lila Ike announce European tour dates"
    assert items[0].ext_id == "https://www.worldareggae.com/?p=82304"


def test_leafly_politics_em_dash():
    first = _parse("feeds_leafly_politics.xml", SRC["leafly_politics"])[0]
    assert first.title == "Texas government outlaws THC—confuses country, consumers, and themselves"
    assert first.ext_id == "https://www.leafly.com/wp-home/?p=284163"


# ---- Squarespace ----

def test_daily_reggae_keys_items_by_link_not_opaque_guid():
    items = _parse("feeds_dailyreggae.xml", SRC["dailyreggae"])
    assert [i.ext_id for i in items] == [
        "https://www.dailyreggae.com/news/iration-falling-remix",
        "https://www.dailyreggae.com/news/stick-figure-shine",          # utm_* stripped from the key too
        "https://www.dailyreggae.com/news/rebelution-collie-buddz-highest-grade",
    ]
    assert all(i.ext_id == i.url for i in items)
    first = items[0]
    assert first.title == "Get this weekend's party started with Iration's rocking remix of \"Falling\""
    assert first.summary.startswith("Iration’s new remix of “Falling” adds horns")
    assert first.categories == ("reggae rock", "new music")


def test_daily_reggae_without_id_from_link_uses_the_guid():
    items = _parse("feeds_dailyreggae.xml", GENERIC)
    assert items[0].ext_id == "5f9dc79d2a1b3c4d5e6f7a8b:604545f7c1d2e3f4a5b6c7d8:6ac11b5b9e8d7c6b5a4f3e2d"


# ---- ScienceDaily ----

def test_sciencedaily_no_text_kept_and_edt_est_dates():
    items = _parse("feeds_sciencedaily.xml", SRC["sciencedaily_psychedelics"])
    assert [i.summary for i in items] == ["", "", ""]          # their terms: keep_text False
    assert [i.published_at for i in items] == [
        datetime(2026, 9, 30, 12, 49, 1, tzinfo=UTC),         # 08:49:01 EDT
        datetime(2026, 8, 9, 12, 40, 58, tzinfo=UTC),         # 08:40:58 EDT
        datetime(2026, 2, 5, 15, 12, 33, tzinfo=UTC),         # 10:12:33 EST
    ]
    assert items[0].ext_id == items[0].url == "https://www.sciencedaily.com/releases/2026/09/260930020255.htm"
    assert items[1].title == "Frequent cannabis users wake up with their 'stress hormone' already elevated"
    assert all(i.categories == () for i in items)


def test_sciencedaily_text_kept_when_allowed():
    items = _parse("feeds_sciencedaily.xml", GENERIC)
    assert items[0].summary.startswith("A systematic review of 30 studies")


# ---- Atom and RDF ----

def test_atom_feed():
    items = _parse("feeds_atom.xml")
    assert len(items) == 3
    first = items[0]
    assert first.ext_id == "tag:notes.example.org,2026:post-118"
    assert first.url == "https://notes.example.org/2026/10/mdma-ptsd?ref=feed"
    assert first.title == "MDMA & PTSD: what the “confirmatory” trial changed"
    assert first.published_at == datetime(2026, 10, 2, 19, 30, tzinfo=UTC)    # published, not updated
    assert first.categories == ("mdma", "ptsd")
    assert first.summary == ("The second Phase 3 trial replicated the first. "
                             "Remission rates stayed similar across sites.")
    assert first.doi == "10.1038/s41591-026-04012-x"                         # link rel="related"
    assert items[1].published_at == datetime(2026, 9, 30, 12, tzinfo=UTC)     # only <updated>
    assert items[1].summary == "Plain text summary."
    assert items[2].published_at is None and items[2].summary == ""


def test_rdf_feed():
    items = _parse("feeds_rdf.xml")
    assert [i.doi for i in items] == ["10.1101/2026.10.01.26321234", "10.1101/2026.09.30.26321100"]
    assert items[0].url == "http://medrxiv.org/cgi/content/short/2026.10.01.26321234v1?rss=1"
    assert items[0].published_at == datetime(2026, 10, 2, tzinfo=UTC)
    assert items[0].title.startswith("Ketamine versus electroconvulsive therapy")
    assert items[1].categories == ("psychiatry and clinical psychology",)


# ---- Russian feeds ----

def test_nplus1_moscow_time_and_title_repeated_as_description():
    items = _parse("feeds_nplus1.xml", SRC["nplus1"])
    first = items[0]
    assert first.title == "Псилоцибин ослабил симптомы нервной анорексии в небольшом исследовании"
    assert first.published_at == datetime(2026, 10, 2, 12, 38, tzinfo=UTC)    # 15:38 +0300
    assert first.lang == "ru"
    assert first.summary == ""                      # the description only repeated the title
    assert first.ext_id == first.url == "https://nplus1.ru/news/2026/10/02/psilocybin-anorexia"
    assert items[1].title == "Жевательный табак оставил след в зубном налёте у жителей Скандинавии XVII века"
    assert items[1].summary == "Археологи нашли следы никотина в зубном камне"


def test_meduza_content_and_moscow_time():
    first = _parse("feeds_meduza.xml", SRC["meduza"])[0]
    assert first.published_at == datetime(2026, 10, 3, 12, 27, 17, tzinfo=UTC)
    assert first.summary.startswith("Бундестаг принял поправки к закону о медицинском каннабисе.")
    assert "а не только специалист. Закон вступит в силу" in first.summary
    assert first.lang == "ru" and first.categories == ()


def test_knife_cyrillic_categories():
    first = _parse("feeds_knife.xml", SRC["knife"])[0]
    assert first.ext_id == "https://knife.media/?p=273790"
    assert first.categories == ("наука", "психоделики")
    assert first.title == "Как психоделики вернулись в науку: краткая история «психоделического ренессанса»"
    assert first.published_at == datetime(2026, 10, 3, 13, 50, 13, tzinfo=UTC)


def test_windows_1251_declared_in_the_xml_header():
    body = fixture("feeds_cp1251.xml")
    assert b'encoding="windows-1251"' in body and "Учёные".encode("cp1251") in body
    items = feeds.parse(body, dataclasses.replace(GENERIC, lang="ru"))
    first = items[0]
    assert first.title == "Учёные изучили действие каннабидиола при эпилепсии — «обнадёживающие» результаты"
    assert first.summary == "Исследование № 42: у детей с синдромом Драве число приступов снизилось на треть."
    assert first.categories == ("медицина",)
    assert first.published_at == datetime(2026, 10, 3, 8, 20, tzinfo=UTC)
    assert first.lang == "ru"


def test_blank_lines_before_the_xml_declaration_keep_the_encoding():
    # A classic WordPress fault (a plugin echoes a newline); expat then ignores the declared encoding.
    items = feeds.parse(b"\n\n" + fixture("feeds_cp1251.xml"), GENERIC)
    assert items[0].title.startswith("Учёные изучили действие каннабидиола")
    assert items[1].summary == "Плацебо-контролируемые исследования пока не подтвердили эффект."


def test_utf8_declared_in_the_xml_header():
    body = _rss("<item><title>Нож &amp; ложка</title><link>https://knife.media/a/</link></item>")
    assert feeds.parse(body, GENERIC)[0].title == "Нож & ложка"


def test_empty_source_lang_means_english():
    source = dataclasses.replace(GENERIC, lang="")
    assert _parse("feeds_marijuanamoment.xml", source)[0].lang == "en"


# ---- broken entries and documents ----

def test_entries_without_link_or_title_are_skipped():
    items = _parse("feeds_partial.xml")
    assert [i.url for i in items] == [
        "https://example.org/guid-permalink",       # no <link>, but the guid is a permalink
        "https://example.org/double-escaped",
        "https://example.org/markup-title",
        "https://example.org/dup-1",                 # the second entry with the same guid is dropped
    ]
    assert items[0].published_at is None             # "not a date"


def test_html_entities_and_markup_in_titles():
    items = _by_url(_parse("feeds_partial.xml"))
    assert items["https://example.org/double-escaped"].title == (
        "Double-escaped: Why ‘microdosing’ studies disagree & what next")
    markup = items["https://example.org/markup-title"]
    assert markup.title == "Bold title with a link — and AT&T, 5 < 7"
    assert markup.summary == "Visible texthere."     # no script or style text, no invisible characters


def test_an_escaped_less_than_sign_stays_text():
    body = _rss("<item><title>THC &lt;LOQ in most hemp drinks, &gt;LOQ in 3</title><link>https://example.org/loq</link>"
                "<description><![CDATA[<p>THC was &lt;LOQ in 12 of 15 samples and &gt;LOQ in 3, "
                "p&lt;0.05.</p>]]></description></item>")
    item = feeds.parse(body, GENERIC)[0]
    assert item.title == "THC <LOQ in most hemp drinks, >LOQ in 3"
    assert item.summary == "THC was <LOQ in 12 of 15 samples and >LOQ in 3, p<0.05."


def test_double_escaped_markup_inside_cdata_is_stripped():
    # Some CMSs escape the body and then wrap it in CDATA: one pass of HTML parsing leaves markup behind.
    body = _rss("<item><title>T</title><link>https://example.org/d</link><description><![CDATA["
                "&lt;section&gt;&lt;script&gt;var ad = 1;&lt;/script&gt;&lt;h2&gt;Lead&lt;/h2&gt;Text "
                "&lt;code&gt;here&lt;/code&gt;&lt;/section&gt;]]></description></item>")
    assert feeds.parse(body, GENERIC)[0].summary == "Lead Text here"


def test_plain_text_title_is_not_parsed_as_html():
    body = _rss("<item><title>Why &lt;script&gt; tags in feeds worry security teams</title>"
                "<link>https://example.org/s</link></item>")
    assert feeds.parse(body, GENERIC)[0].title == "Why <script> tags in feeds worry security teams"


def test_atom_plain_text_elements_with_escaped_html():
    # Atom says type="text", but many publishers put escaped HTML there.
    body = ('<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>'
            "<entry><id>tag:x.org,2026:1</id><title>MDMA &amp;amp; PTSD &amp;#8212; a review</title>"
            '<link href="https://example.org/a"/><summary>&lt;p&gt;Remission held &amp;amp; '
            "relapse fell.&lt;/p&gt;</summary></entry></feed>").encode()
    item = feeds.parse(body, GENERIC)[0]
    assert item.title == "MDMA & PTSD — a review"
    assert item.summary == "Remission held & relapse fell."


def test_bare_ampersands_in_a_malformed_feed():
    # Not well-formed: feedparser's loose parser turns "R&D" into "R&D;"; the text comes out as written.
    body = _rss("<item><title>Q&A: R&D money for psilocybin trials</title><link>https://example.org/q?a=1&b=2</link>"
                "<description>Hemp & CBD</description></item>")
    item = feeds.parse(body, GENERIC)[0]
    assert item.title == "Q&A: R&D money for psilocybin trials"
    assert item.summary == "Hemp & CBD" and item.url == "https://example.org/q?a=1&b=2"


def test_long_title_and_summary_are_cut_on_a_word_boundary():
    words = " ".join(f"word{n}" for n in range(600))
    body = _rss(f"<item><title>{words}</title><link>https://example.org/long</link>"
                f"<description>Text: {words}</description></item>")
    item = feeds.parse(body, GENERIC)[0]
    assert len(item.title) <= 300 and len(item.summary) <= 1500
    assert item.title.endswith("…") and item.summary.endswith("…")
    assert re.fullmatch(r"(word\d+ )*word\d+…", item.title)
    assert re.fullmatch(r"Text: (word\d+ )*word\d+…", item.summary)
    assert item.summary[6:-1] in words and len(item.summary) > 1400


def test_not_a_feed_html_page():
    with pytest.raises(ValueError, match="not a feed"):
        _parse("feeds_not_a_feed.html")


@pytest.mark.parametrize("body", [
    b'<?xml version="1.0"?><html><body><p>Hello</p></body></html>',   # well-formed, but no feed
    b'{"items": [{"title": "json"}]}',
    b"\x1b\x8b\x08\x00\xff\xfe\x00garbage\x00\x01",                  # a compressed body that was not decoded
    b"",
])
def test_malformed_or_non_xml_bodies(body):
    with pytest.raises(ValueError):
        feeds.parse(body, GENERIC)


def test_valid_feed_without_entries_is_empty_not_an_error():
    assert _parse("feeds_empty.xml") == []


def test_entity_declarations_are_refused():
    with pytest.raises(ValueError, match="entity"):
        _parse("feeds_entity.xml")


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_entity_declarations_are_refused_in_wide_encodings(encoding):
    doc = (f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE rss [<!ENTITY x "boom">]><rss version="2.0">'
           "<channel><item><title>&x;</title><link>https://example.org/a</link></item></channel></rss>")
    with pytest.raises(ValueError, match="entity"):
        feeds.parse(doc.encode(encoding), GENERIC)


def test_body_is_never_taken_for_a_file_name(tmp_path):
    path = tmp_path / "feed.xml"
    path.write_bytes(fixture("feeds_marijuanamoment.xml"))
    with pytest.raises(ValueError):
        feeds.parse(str(path).encode(), GENERIC)


# ---- link normalization ----

@pytest.mark.parametrize("raw, expected", [
    ("https://a.org/p/?utm_source=rss&utm_medium=rss&id=5#comments", "https://a.org/p/?id=5"),
    ("https://a.org/p?fbclid=IwAR0&gclid=x&mc_cid=1&mc_eid=2", "https://a.org/p"),
    ("https://a.org/p?UTM_Campaign=x&q=%D0%BD%D0%BE%D0%B6&x=1+2&flag", "https://a.org/p?q=%D0%BD%D0%BE%D0%B6&x=1+2&flag"),
    ("  HTTP://A.org/Path?a=1&&b=2  ", "http://A.org/Path?a=1&b=2"),
    ("https://a.org/news?format=rss", "https://a.org/news?format=rss"),
    ("https://a.org/a b", "https://a.org/a%20b"),
    ("javascript:alert(1)", None),
    ("ftp://a.org/file", None),
    ("/relative/path", None),
    ("https://", None),
    ("https://a.org/\x00x", None),
    ("https://a.org/" + "x" * 3000, None),
    (None, None),
])
def test_normalize_link(raw, expected):
    assert feeds.normalize_link(raw) == expected


def test_relative_links_resolve_against_the_feed_url():
    body = _rss("<item><title>Relative</title><link>/2026/10/relative/</link></item>")
    source = dataclasses.replace(GENERIC, url="https://www.lucid.news/feed/")
    assert feeds.parse(body, source)[0].url == "https://www.lucid.news/2026/10/relative/"


def test_opaque_guid_is_never_made_into_a_link():
    # A guid without isPermaLink="false" counts as a permalink; feedparser copies it into a missing <link>.
    body = _rss("<item><title>Opaque, no link</title><guid>post-80412</guid></item>"
                "<item><title>Opaque before a relative link</title><guid>80413</guid><link>/2026/10/x/</link></item>"
                "<item><title>Permalink guid</title><guid>https://www.lucid.news/2026/10/y/</guid></item>")
    source = dataclasses.replace(GENERIC, url="https://www.lucid.news/feed/")
    items = feeds.parse(body, source)
    assert [(i.ext_id, i.url) for i in items] == [
        ("80413", "https://www.lucid.news/2026/10/x/"),          # the guid is kept as written, not resolved
        ("https://www.lucid.news/2026/10/y/", "https://www.lucid.news/2026/10/y/"),
    ]


def test_atom_relative_link_after_the_id():
    body = (b'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>'
            b'<entry><id>tag:notes.example.org,2026:7</id><title>Relative</title><link href="/2026/10/a"/></entry>'
            b"<entry><title>Only an id</title><id>post-8</id></entry></feed>")
    items = feeds.parse(body, GENERIC)
    assert [(i.ext_id, i.url) for i in items] == [("tag:notes.example.org,2026:7", "https://example.org/2026/10/a")]


def test_id_from_link_dedupes_links_that_differ_only_in_tracking():
    body = _rss("<item><title>A</title><link>https://a.org/x?utm_source=a</link><guid>g1</guid></item>"
                "<item><title>A again</title><link>https://a.org/x#top</link><guid>g2</guid></item>")
    source = dataclasses.replace(GENERIC, params={"id_from_link": True})
    assert [i.ext_id for i in feeds.parse(body, source)] == ["https://a.org/x"]
