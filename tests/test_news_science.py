"""Science adapters: PubMed, Europe PMC, bioRxiv/medRxiv and ClinicalTrials.gov, on saved
responses (tests/news_fixtures/), without network or database."""
from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

import nestor.news
from nestor.news.filters import drop_reason
from nestor.news.http import HttpError, Response
from nestor.news.sources import SOURCES

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


pubmed = _adapter("pubmed")
europepmc = _adapter("europepmc")
biorxiv = _adapter("biorxiv")
ctgov = _adapter("ctgov")

SRC = {s.key: s for s in SOURCES}
NOW = datetime(2026, 10, 4, 18, 30, tzinfo=timezone.utc)
UTC = timezone.utc


def _json(value) -> bytes:
    return json.dumps(value).encode()


def _plain_text(item) -> None:
    for text in (item.title, item.summary):
        assert not re.search(r"<[A-Za-z/!]|&[a-z]+;|&#|\s\s", text) and text == text.strip()
    assert item.url.startswith(("https://", "http://"))
    assert item.published_at is None or item.published_at.tzinfo is not None


# ---- PubMed ----

def _pubmed_http(contact: str = "owner@example.org", search: bytes | None = None) -> FakeHttp:
    return FakeHttp([("POST", r"/esearch\.fcgi$", search or fixture("pubmed_esearch.json")),
                     ("POST", r"/efetch\.fcgi$", fixture("pubmed_efetch.xml"))], contact=contact)


async def test_pubmed_requests():
    http = _pubmed_http()
    await pubmed.fetch(http, SRC["pubmed"], {}, NOW)
    search, efetch = http.calls
    assert search["url"] == "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    assert search["data"] == {"db": "pubmed", "term": SRC["pubmed"].params["query"], "datetype": "edat",
                              "reldate": "2", "retmax": "500", "retmode": "json", "tool": "nestorbot",
                              "email": "owner@example.org"}
    assert efetch["url"] == "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    assert efetch["data"] == {"db": "pubmed", "retmode": "xml", "rettype": "abstract",
                              "id": "41234571,41234570,41234569,41234568,41234567", "tool": "nestorbot",
                              "email": "owner@example.org"}


async def test_pubmed_no_email_without_contact():
    http = _pubmed_http(contact="")
    await pubmed.fetch(http, SRC["pubmed"], {}, NOW)
    assert all("email" not in c["data"] and c["data"]["tool"] == "nestorbot" for c in http.calls)


async def test_pubmed_parse():
    result = await pubmed.fetch(_pubmed_http(), SRC["pubmed"], {}, NOW)
    by_id = {i.ext_id: i for i in result.items}
    assert list(by_id) == ["41234567", "41234568", "41234569"]  # no empty title, no book chapter
    for item in result.items:
        _plain_text(item)
        assert item.lang == "en" and item.url == f"https://pubmed.ncbi.nlm.nih.gov/{item.ext_id}/"

    rct = by_id["41234567"]
    assert rct.title == ("Psilocybin-assisted therapy for treatment-resistant depression: "
                         "a randomized, placebo-controlled trial.")
    assert rct.summary.startswith("BACKGROUND: Treatment-resistant depression (TRD) affects about one third")
    assert " METHODS: We randomly assigned 120 adults" in rct.summary
    assert "(difference −7.4 points; 95% CI −10.9 to −3.9; p < 0.001)" in rct.summary
    assert rct.summary.endswith("CONCLUSIONS: Psilocybin with psychological support reduced depressive symptoms "
                                "over 6 weeks.")
    assert "Author(s)" not in rct.summary  # CopyrightInformation is not abstract text
    assert rct.doi == "10.1177/02698811261234567"  # ArticleIdList, not the reference list's DOI
    assert rct.published_at == datetime(2026, 10, 2, 6, 42, tzinfo=UTC)  # 'pubmed' (edat), not 'entrez'
    assert rct.meta == {"pmid": "41234567", "journal": "J Psychopharmacol",
                        "pubtypes": ["Journal Article", "Randomized Controlled Trial"]}

    review = by_id["41234568"]
    assert review.title == "Cannabidiol and Δ9-tetrahydrocannabinol in cannabis use disorder: a systematic review"
    assert review.summary == ("We reviewed 24 trials of cannabidiol (CBD) and Δ9-THC for cannabis use disorder. "
                              "Evidence for withdrawal relief was moderate.")
    assert review.doi == "10.3389/fpsyt.2026.1612345"  # from ELocationID, lowercased
    assert review.published_at == datetime(2026, 10, 3, tzinfo=UTC)
    assert review.meta["journal"] == "Frontiers in psychiatry"  # no ISOAbbreviation
    assert review.meta["pubtypes"] == ["Journal Article", "Systematic Review"]

    german = by_id["41234569"]
    assert german.title == "Medical cannabis prescriptions in Germany: a survey of general practitioners"
    assert german.doi is None
    assert german.summary.startswith("ZIEL DER STUDIE: ")


async def test_pubmed_empty_idlist_skips_efetch():
    http = _pubmed_http(search=fixture("pubmed_esearch_empty.json"))
    result = await pubmed.fetch(http, SRC["pubmed"], {}, NOW)
    assert result.items == [] and len(http.calls) == 1


@pytest.mark.parametrize("body, reason", [
    (fixture("pubmed_esearch_error.json"), "Search Backend failed"),
    (fixture("pubmed_esearch_errorlist.json"), "phrasesnotfound"),
    (b'{"error":"API rate limit exceeded","count":"11"}', "rate limit"),
    (b"<html><body>Service unavailable</body></html>", "not JSON"),
    (b'{"header":{"type":"esearch"}}', "no esearchresult"),
])
async def test_pubmed_error_in_200_body(body, reason):
    http = _pubmed_http(search=body)
    with pytest.raises(ValueError, match=reason):
        await pubmed.fetch(http, SRC["pubmed"], {}, NOW)
    assert len(http.calls) == 1


async def test_pubmed_efetch_in_batches():
    ids = [str(40000000 + n) for n in range(450)]
    search = _json({"esearchresult": {"count": "450", "idlist": ids + ids[:3]}})
    http = FakeHttp([("POST", r"/esearch\.fcgi$", search),
                     ("POST", r"/efetch\.fcgi$", b"<?xml version='1.0'?>\n<PubmedArticleSet></PubmedArticleSet>")])
    await pubmed.fetch(http, SRC["pubmed"], {}, NOW)
    batches = [c["data"]["id"].split(",") for c in http.calls[1:]]
    assert [len(b) for b in batches] == [100, 100, 100, 100, 50]
    assert sum(batches, []) == ids


@pytest.mark.parametrize("body, reason", [
    (b'<?xml version="1.0"?><!DOCTYPE PubmedArticleSet [<!ENTITY a "aaaa">]>'
     b"<PubmedArticleSet><PubmedArticle>&a;</PubmedArticle></PubmedArticleSet>", "entity"),
    ('<?xml version="1.0" encoding="UTF-16"?><PubmedArticleSet/>'.encode("utf-16"), "not XML"),
    (b"<PubmedArticleSet><PubmedArticle>", "bad XML"),
    (b"<eFetchResult><ERROR>Empty id list - nothing todo</ERROR></eFetchResult>", "Empty id list"),
])
def test_pubmed_efetch_refuses(body, reason):
    with pytest.raises(ValueError, match=reason):
        pubmed.parse_efetch(body)


def test_pubmed_long_abstract_clipped():
    text = "word " * 600
    xml = (f"<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID><Article><ArticleTitle>T</ArticleTitle>"
           f"<Abstract><AbstractText>{text}</AbstractText></Abstract></Article></MedlineCitation>"
           f"</PubmedArticle></PubmedArticleSet>").encode()
    (item,) = pubmed.parse_efetch(xml)
    assert len(item.summary) <= 1500 and item.summary.endswith("word…")
    assert item.published_at is None and item.doi is None and item.meta["journal"] == ""


def _one_article(title: str, abstract: str = "", pubmed_data: str = "", extra: str = "") -> bytes:
    return (f"<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>41230001</PMID><Article>"
            f"<ArticleTitle>{title}</ArticleTitle><Abstract>{abstract}</Abstract>{extra}</Article></MedlineCitation>"
            f"<PubmedData>{pubmed_data}</PubmedData></PubmedArticle></PubmedArticleSet>").encode()


@pytest.mark.parametrize("title, expected", [
    ("[Cannabis auf Rezept: eine Umfrage].", "Cannabis auf Rezept: eine Umfrage"),   # a translated title
    ("[Effects of [11C]raclopride binding after LSD]", "Effects of [11C]raclopride binding after LSD"),
    # radiotracers, not translations: the first bracket closes early
    ("[18F]FDG PET in daily cannabis users [corrected].", "[18F]FDG PET in daily cannabis users [corrected]."),
    ("[11C]Cimbi-36 binding after psilocybin [retracted]", "[11C]Cimbi-36 binding after psilocybin [retracted]"),
    ("[Not Available].", None),                       # NLM's placeholder: no usable title
])
def test_pubmed_bracketed_titles(title, expected):
    items = pubmed.parse_efetch(_one_article(title))
    assert [i.title for i in items] == ([expected] if expected else [])


def test_pubmed_unlabelled_part_and_fallbacks():
    abstract = ('<AbstractText Label="UNLABELLED" NlmCategory="UNASSIGNED">Psilocybin was given to 20 adults.'
                '</AbstractText><AbstractText Label="RESULTS" NlmCategory="RESULTS">Mood improved.</AbstractText>')
    history = ("<History><PubMedPubDate PubStatus=\"entrez\"><Year>2026</Year><Month>10</Month><Day>3</Day>"
               "<Hour>14</Hour><Minute>7</Minute></PubMedPubDate></History>")
    extra = ('<ELocationID EIdType="doi" ValidYN="N">10.9999/broken</ELocationID>'
             '<ELocationID EIdType="doi" ValidYN="Y">10.1016/J.EURONEURO.2026.10.003</ELocationID>')
    (item,) = pubmed.parse_efetch(_one_article("Psilocybin in adults", abstract, history, extra))
    assert item.summary == "Psilocybin was given to 20 adults. RESULTS: Mood improved."
    assert item.published_at == datetime(2026, 10, 3, 14, 7, tzinfo=UTC)  # 'entrez' when 'pubmed' is missing
    assert item.doi == "10.1016/j.euroneuro.2026.10.003"  # the invalid ELocationID is passed over


def test_pubmed_double_escaped_text():
    (item,) = pubmed.parse_efetch(_one_article("Q&amp;amp;A: MDMA &amp;amp;amp; PTSD",
                                               "<AbstractText>A &amp;amp;lt; B&amp;#x200B; held.</AbstractText>"))
    assert item.title == "Q&A: MDMA & PTSD" and item.summary == "A < B held."


# ---- Europe PMC ----

MARK = "AoIIQJK98Sg1NjU0ODAyOQ=="


def _epmc_http() -> FakeHttp:
    return FakeHttp([("GET", r"cursorMark=%2A", fixture("europepmc_page1.json")),
                     ("GET", r"cursorMark=AoIIQJK98Sg1NjU0ODAyOQ%3D%3D", fixture("europepmc_page2.json"))])


async def test_europepmc_requests_and_paging():
    http = _epmc_http()
    result = await europepmc.fetch(http, SRC["europepmc"], {}, NOW)
    assert len(http.calls) == 2  # page 2 repeats the cursor mark: done
    first, second = (c["params"] for c in http.calls)
    assert http.calls[0]["url"].startswith("https://www.ebi.ac.uk/europepmc/webservices/rest/search?")
    assert first == {"query": f"TITLE_ABS:({SRC['europepmc'].params['terms']}) AND SRC:PPR "
                              "AND FIRST_IDATE:[2026-10-01 TO 2026-10-04]",
                     "format": "json", "resultType": "core", "pageSize": "100", "cursorMark": "*"}
    assert second == {**first, "cursorMark": MARK}
    assert [i.ext_id for i in result.items] == ["PPR1332402", "PPR1332977", "PPR1333105", "PPR1333240"]


async def test_europepmc_parse():
    result = await europepmc.fetch(_epmc_http(), SRC["europepmc"], {}, NOW)
    by_id = {i.ext_id: i for i in result.items}
    for item in result.items:
        _plain_text(item)
        assert item.lang == "en"

    psyarxiv = by_id["PPR1332402"]
    assert psyarxiv.doi == "10.31234/osf.io/gauby"  # without the _v1 version
    assert psyarxiv.url == "https://doi.org/10.31234/osf.io/gauby_v1"  # the DOI as registered
    assert psyarxiv.summary == ("Background Oregon opened state-regulated psilocybin service centers in 2023. "
                                "Methods We analysed adverse event reports filed by licensed facilitators & service "
                                "centers. Results Serious events were rare (n = 12).")  # thin spaces collapsed too
    assert psyarxiv.published_at == datetime(2026, 10, 2, tzinfo=UTC)  # firstIndexDate
    assert psyarxiv.meta == {"preprint": True, "server": "PsyArXiv", "published_pmid": "42665360"}

    medrxiv = by_id["PPR1332977"]
    assert medrxiv.title == "Cannabis use and first-episode psychosis: a UK Biobank cohort"
    assert medrxiv.doi == "10.64898/2026.09.26.26364086"
    assert medrxiv.summary == ("Using UK Biobank data (N = 402,113) we examined daily cannabis use and "
                               "incident psychosis. Daily use was associated with a higher risk (HR 1.9).")
    assert medrxiv.meta == {"preprint": True, "server": "medRxiv"}

    research_square = by_id["PPR1333105"]
    assert research_square.doi == "10.21203/rs.3.rs-7654321"
    assert research_square.url == "https://doi.org/10.21203/rs.3.rs-7654321/v1"
    assert research_square.published_at == datetime(2026, 10, 1, tzinfo=UTC)  # firstPublicationDate fallback

    no_doi = by_id["PPR1333240"]
    assert no_doi.doi is None and no_doi.url == "https://europepmc.org/article/PPR/PPR1333240"
    assert no_doi.meta == {"preprint": True, "server": "Authorea"}
    assert "PPR1333301" not in by_id  # no title


def test_europepmc_literal_lt_and_amp_kept():
    page = europepmc.parse_search(_json({"hitCount": 1, "resultList": {"result": [{
        "id": "PPR1340001", "source": "PPR", "title": "Cannabis R&D",
        "abstractText": "<p>THC was <LOD in 30% of samples; IL-6<IL-10 when A<B and C>D.</p><p>Q&A &amp; more.</p>",
        "firstIndexDate": "2026-10-03"}]}}))
    (item,) = page.items
    assert item.title == "Cannabis R&D"
    assert item.summary == "THC was <LOD in 30% of samples; IL-6<IL-10 when A<B and C>D. Q&A & more."


def test_europepmc_double_escaped_text():
    page = europepmc.parse_search(_json({"hitCount": 1, "resultList": {"result": [{
        "id": "PPR1340002", "source": "PPR", "title": "MDMA &amp;amp; PTSD &amp;#8212; a review",
        "abstractText": "<p>A &amp;amp;lt; B, &amp;regex; stays</p>", "firstIndexDate": "2026-10-03"}]}}))
    (item,) = page.items
    assert item.title == "MDMA & PTSD — a review" and item.summary == "A < B, &regex; stays"


def _epmc_page(ids: list[str], mark: str | None, hits: int = 1000) -> bytes:
    page = {"version": "6.9", "hitCount": hits, "resultList": {"result": [
        {"id": i, "source": "PPR", "title": f"Psilocybin study {i}", "firstIndexDate": "2026-10-03"} for i in ids]}}
    if mark is not None:
        page["nextCursorMark"] = mark
    return _json(page)


async def test_europepmc_stops_on_empty_page():
    http = FakeHttp([("GET", r"cursorMark=%2A", _epmc_page(["PPR1"], "m1")),
                     ("GET", r"cursorMark=m1", _epmc_page([], "m2"))])
    result = await europepmc.fetch(http, SRC["europepmc"], {}, NOW)
    assert len(http.calls) == 2 and [i.ext_id for i in result.items] == ["PPR1"]


async def test_europepmc_stops_on_repeated_mark():
    http = FakeHttp([("GET", r"cursorMark=%2A", _epmc_page(["PPR1"], "m1")),
                     ("GET", r"cursorMark=m1", _epmc_page(["PPR2"], "m1"))])
    result = await europepmc.fetch(http, SRC["europepmc"], {}, NOW)
    assert len(http.calls) == 2 and [i.ext_id for i in result.items] == ["PPR1", "PPR2"]


async def test_europepmc_at_most_five_pages():
    def page(call):
        n = int(call["params"]["cursorMark"].lstrip("m*") or 0)
        return _epmc_page([f"PPR{n}0", f"PPR{n}1"], f"m{n + 1}")
    http = FakeHttp([("GET", r"europepmc", page)])
    result = await europepmc.fetch(http, SRC["europepmc"], {}, NOW)
    assert [c["params"]["cursorMark"] for c in http.calls] == ["*", "m1", "m2", "m3", "m4"]
    assert len(result.items) == 10


async def test_europepmc_stops_at_hit_count():
    http = FakeHttp([("GET", r"europepmc", _epmc_page(["PPR1", "PPR2"], "m1", hits=2))])
    await europepmc.fetch(http, SRC["europepmc"], {}, NOW)
    assert len(http.calls) == 1


@pytest.mark.parametrize("body, reason", [
    (b"<html>Service Temporarily Unavailable</html>", "not JSON"),
    (b'{"version":"6.9","hitCount":3}', "no resultList"),
    (b'{"errMsg":"Invalid query syntax"}', "Invalid query"),
])
async def test_europepmc_bad_response(body, reason):
    with pytest.raises(ValueError, match=reason):
        await europepmc.fetch(FakeHttp([("GET", r"europepmc", body)]), SRC["europepmc"], {}, NOW)


def test_europepmc_no_hits():
    page = europepmc.parse_search(b'{"version":"6.9","hitCount":0,"request":{},"resultList":{"result":[]}}')
    assert page.items == [] and page.results == 0


# ---- bioRxiv / medRxiv ----

async def test_biorxiv_requests_and_paging():
    http = FakeHttp([("GET", r"/medrxiv/2026-10-02/2026-10-04/0/json$", fixture("biorxiv_medrxiv_0.json")),
                     ("GET", r"/medrxiv/2026-10-02/2026-10-04/30/json$", fixture("biorxiv_medrxiv_30.json"))])
    result = await biorxiv.fetch(http, SRC["medrxiv"], {}, NOW)
    assert [c["url"] for c in http.calls] == ["https://api.biorxiv.org/details/medrxiv/2026-10-02/2026-10-04/0/json",
                                              "https://api.biorxiv.org/details/medrxiv/2026-10-02/2026-10-04/30/json"]
    by_doi = {i.ext_id: i for i in result.items}
    assert sorted(by_doi) == ["10.1101/2025.11.05.25339876", "10.64898/2026.09.10.26362629",
                              "10.64898/2026.09.28.26364300", "10.64898/2026.09.30.26364512"]
    for item in result.items:
        _plain_text(item)
        assert item.doi == item.ext_id and item.lang == "en"

    new = by_doi["10.64898/2026.09.30.26364512"]
    assert new.url == "https://www.medrxiv.org/content/10.64898/2026.09.30.26364512v1"
    assert new.title == "Psilocybin therapy for alcohol use disorder: 12-month follow-up of a randomized trial"
    assert new.summary.endswith("Results: Heavy drinking days stayed lower (p<0.01).")
    assert new.published_at == datetime(2026, 10, 2, tzinfo=UTC)
    assert new.meta == {"server": "medRxiv", "version": "1", "category": "addiction medicine", "preprint": True}

    revised = by_doi["10.64898/2026.09.28.26364300"]  # v1 on page 0, v2 on page 30: v2 wins
    assert revised.url.endswith("26364300v2") and revised.meta["version"] == "2"
    assert revised.meta["first_in_window"]      # v1 is in the window too: a new preprint, not an old one revised
    assert revised.title.endswith("cannabinoid hyperemesis syndrome")
    assert revised.published_at == datetime(2026, 10, 3, tzinfo=UTC)

    published = by_doi["10.64898/2026.09.10.26362629"]
    assert published.title == "Electronic health records and sepsis prediction in rural hospitals"
    assert published.meta["published_doi"] == "10.1016/s2589-7500(26)00123-4"


async def test_biorxiv_keeps_highest_version_in_any_order():
    def page(versions):
        return _json({"messages": [{"status": "ok", "total": 2}], "collection": [
            {"doi": "10.64898/2026.10.01.700001", "title": "Psilocybin in mice", "version": v, "date": "2026-10-03",
             "category": "neuroscience", "abstract": ""} for v in versions]})
    http = FakeHttp([("GET", r"/biorxiv/", page(["3", "1"]))])
    result = await biorxiv.fetch(http, SRC["biorxiv"], {}, NOW)
    assert [i.meta["version"] for i in result.items] == ["3"]
    assert result.items[0].url == "https://www.biorxiv.org/content/10.64898/2026.10.01.700001v3"
    assert result.items[0].meta["server"] == "bioRxiv"
    assert http.calls[0]["url"] == "https://api.biorxiv.org/details/biorxiv/2026-10-02/2026-10-04/0/json"


def test_biorxiv_literal_lt_kept():
    page = biorxiv.parse_details(_json({"messages": [{"status": "ok", "total": "1"}], "collection": [{
        "doi": "10.64898/2026.10.02.700002", "title": "Cannabinoids <i>in vitro</i>", "version": "1",
        "date": "2026-10-03", "category": "pharmacology and toxicology",
        "abstract": "Anandamide stayed <LLOQ in most samples (n<10); CB1<CB2 binding. Results held."}]}), "biorxiv")
    (item,) = page.items
    assert item.title == "Cannabinoids in vitro"
    assert item.summary == "Anandamide stayed <LLOQ in most samples (n<10); CB1<CB2 binding. Results held."


def test_biorxiv_double_escaped_text():
    page = biorxiv.parse_details(_json({"messages": [{"status": "ok", "total": "1"}], "collection": [{
        "doi": "10.64898/2026.10.02.700004", "title": "MDMA &amp;amp; PTSD", "version": "1", "date": "2026-10-03",
        "abstract": "Remission held &amp;amp;#8212; R&D"}]}), "biorxiv")
    (item,) = page.items
    assert item.title == "MDMA & PTSD" and item.summary == "Remission held — R&D"


def test_biorxiv_topic_word_past_the_clip():
    # A structured medRxiv abstract that names the exposure only in its Results, past the 1500 chars of the summary.
    abstract = ("Background: Psychosis risk in young adults. " + "Methods: a cohort of 12,000 people followed for "
                "ten years with yearly interviews and record linkage. " * 18 + "Results: cannabis use predicted psychosis.")
    page = biorxiv.parse_details(_json({"messages": [{"status": "ok", "total": "1"}], "collection": [{
        "doi": "10.64898/2026.10.02.26364999", "title": "Exposures in early adulthood and later psychosis",
        "version": "1", "date": "2026-10-03", "category": "psychiatry and clinical psychology",
        "abstract": abstract}]}), "medrxiv")
    (item,) = page.items
    assert len(abstract) > biorxiv.SUMMARY_MAX >= len(item.summary) and "cannabis" not in item.summary
    assert item.full_text == abstract and "cannabis" in item.full_text
    assert drop_reason(item, SRC["medrxiv"]) is None
    long = biorxiv.parse_details(_json({"messages": [{"status": "ok", "total": "1"}], "collection": [{
        "doi": "10.64898/2026.10.02.26365000", "title": "Long", "version": "1", "abstract": "word " * 5000}]}),
        "medrxiv").items[0]
    assert len(long.full_text) == biorxiv.TEXT_MAX


async def test_biorxiv_paging_with_100_per_page():
    def page(call):
        offset = int(call["url"].rsplit("/", 2)[-2])
        return _json({"messages": [{"status": "ok", "count": 100, "total": "250"}], "collection": [
            {"doi": f"10.64898/2026.10.02.{offset + k:06d}", "title": f"Preprint {offset + k}", "version": "1",
             "date": "2026-10-03"} for k in range(min(100, 250 - offset))]})
    http = FakeHttp([("GET", r"/medrxiv/", page)])
    result = await biorxiv.fetch(http, SRC["medrxiv"], {}, NOW)
    assert [c["url"].rsplit("/", 2)[-2] for c in http.calls] == ["0", "100", "200"] and len(result.items) == 250

    def exact(call):
        return _json({"messages": [{"status": "ok", "count": 100, "total": "100"}], "collection": [
            {"doi": f"10.64898/2026.10.02.{k:06d}", "title": f"Preprint {k}", "version": "1"} for k in range(100)]})
    http = FakeHttp([("GET", r"/medrxiv/", exact)])
    assert len((await biorxiv.fetch(http, SRC["medrxiv"], {}, NOW)).items) == 100 and len(http.calls) == 1


def test_biorxiv_odd_version_is_not_a_crash():
    body = _json({"messages": [{"status": "ok", "total": 2}], "collection": [
        {"doi": "10.64898/2026.10.01.700003", "title": "Psilocybin in rats", "version": v, "date": "2026-10-03"}
        for v in ("\u00b2", "v3")]})
    assert [i.meta["version"] for i in biorxiv.parse_details(body, "biorxiv").items] == ["1", "1"]


async def test_biorxiv_no_posts():
    http = FakeHttp([("GET", r"api\.biorxiv\.org", fixture("biorxiv_none.json"))])
    result = await biorxiv.fetch(http, SRC["biorxiv"], {}, NOW)
    assert result.items == [] and len(http.calls) == 1


async def test_biorxiv_at_most_40_pages():
    def page(call):
        cursor = int(call["url"].split("/")[-2])
        return _json({"messages": [{"status": "ok", "cursor": cursor, "count": 30, "total": "100000"}],
                      "collection": [{"doi": f"10.64898/2026.10.01.{cursor + n:06d}", "title": "t", "version": "1",
                                      "date": "2026-10-03"} for n in range(30)]})
    http = FakeHttp([("GET", r"api\.biorxiv\.org", page)])
    result = await biorxiv.fetch(http, SRC["biorxiv"], {}, NOW)
    assert len(http.calls) == 40 and http.calls[-1]["url"].endswith("/1170/json")
    assert len(result.items) == 1200


@pytest.mark.parametrize("body, reason", [
    (b'{"messages":[{"status":"Both dates must be in yyyy-mm-dd format"}],"collection":[]}', "yyyy-mm-dd"),
    (b'{"collection":[]}', "no messages"),
    (b"Bad gateway", "not JSON"),
    (b'{"messages":[{"status":"ok","total":"x"}],"collection":[]}', "no total"),
])
async def test_biorxiv_bad_response(body, reason):
    with pytest.raises(ValueError, match=reason):
        await biorxiv.fetch(FakeHttp([("GET", r"api\.biorxiv\.org", body)]), SRC["medrxiv"], {}, NOW)


async def test_biorxiv_unknown_server():
    source = dataclasses.replace(SRC["medrxiv"], params={"server": "evil.example/x"})
    with pytest.raises(ValueError, match="unknown server"):
        await biorxiv.fetch(FakeHttp(), source, {}, NOW)


# ---- ClinicalTrials.gov ----

STAMP = "2026-10-03T09:00:04"
NOT_SHOWN = ("80 mg", "40 mg", "25 mg", "Split dose", "Microcrystalline")  # descriptions (doses), other names


BASELINE = r"/api/v2/studies\?.*pageSize=1000"


def _ctgov_http(version: bytes | None = None, baseline=None) -> FakeHttp:
    return FakeHttp([("GET", r"/api/v2/version$", version or fixture("ctgov_version.json")),
                     ("GET", BASELINE + ".*pageToken=QmFzZWxpbmUy", fixture("ctgov_baseline_2.json")),
                     ("GET", BASELINE, baseline or fixture("ctgov_baseline_1.json")),
                     ("GET", r"/api/v2/studies\?.*pageToken=ZVNj7o2Elu8o3lpo", fixture("ctgov_studies_2.json")),
                     ("GET", r"/api/v2/studies\?", fixture("ctgov_studies_1.json"))])


async def test_ctgov_skips_when_data_unchanged():
    http = _ctgov_http()
    result = await ctgov.fetch(http, SRC["ctgov"], {"data_timestamp": STAMP, "statuses": {}}, NOW)
    assert result.skipped and result.items == [] and result.cursor is None
    assert [c["url"] for c in http.calls] == ["https://clinicaltrials.gov/api/v2/version"]
    assert http.calls[0]["curl_fallback"] is True


async def test_ctgov_requests():
    http = _ctgov_http()
    await ctgov.fetch(http, SRC["ctgov"], {"data_timestamp": "2026-10-02T09:00:03", "baseline": True}, NOW)
    assert len(http.calls) == 3 and all(c["curl_fallback"] is True for c in http.calls)  # the baseline is done
    first, second = http.calls[1]["params"], http.calls[2]["params"]
    assert http.calls[1]["url"].startswith("https://clinicaltrials.gov/api/v2/studies?")
    assert first == {
        "format": "json",
        "query.intr": 'psilocybin OR MDMA OR midomafetamine OR "lysergic acid diethylamide" OR LSD OR DMT '
                      'OR dimethyltryptamine OR "5-MeO-DMT" OR mebufotenin OR lysergide OR ayahuasca OR ibogaine '
                      "OR mescaline OR cannabidiol OR CBD OR cannabis OR tetrahydrocannabinol OR THC OR dronabinol "
                      "OR nabilone OR nabiximols OR marijuana OR marihuana OR cannabinoid OR cannabigerol OR psilocin",
        "filter.advanced": "AREA[LastUpdatePostDate]RANGE[2026-10-02,MAX]",
        "fields": "NCTId,BriefTitle,BriefSummary,OverallStatus,LastUpdatePostDate,StudyFirstPostDate,"
                  "ResultsFirstPostDate,Phase,HasResults,Condition,InterventionName,InterventionOtherName,"
                  "LeadSponsorName",
        "sort": "LastUpdatePostDate:desc", "pageSize": "100"}
    assert second == {**first, "pageToken": "ZVNj7o2Elu8o3lpo"}


async def test_ctgov_items_and_labels():
    cursor = {"data_timestamp": "2026-10-02T09:00:03", "baseline": True,
              "statuses": {"NCT05222222": "RECRUITING", "NCT06666666": "RECRUITING", "NCT00000001": "COMPLETED",
                           "NCT06777777": "ACTIVE_NOT_RECRUITING"}}
    result = await ctgov.fetch(_ctgov_http(), SRC["ctgov"], cursor, NOW)
    by_nct = {i.meta["nct"]: i for i in result.items}
    # not ours: NCT04333333 (sertraline, cannabis only in the title), NCT05555555 (an LSD1 inhibitor),
    # NCT05888888 (multiple sclerosis, "Disease-Modifying Therapy (DMT)")
    assert list(by_nct) == ["NCT07012345", "NCT05222222", "NCT03444444", "NCT06666666", "NCT06777777"]
    assert {n: i.meta["change"] for n, i in by_nct.items()} == {
        "NCT07012345": "new", "NCT05222222": "status", "NCT03444444": "results", "NCT06666666": "update",
        "NCT06777777": "update"}
    for item in result.items:
        _plain_text(item)
        assert item.url == f"https://clinicaltrials.gov/study/{item.meta['nct']}" and item.lang == "en"
        assert len(item.summary) <= 800

    new = by_nct["NCT07012345"]
    assert new.ext_id == "NCT07012345:2026-10-02"
    assert new.title == "Psilocybin for Treatment-Resistant Depression in Veterans"
    assert new.summary == ("The goal of this clinical trial is to learn if psilocybin with psychotherapy works to "
                           "treat treatment-resistant depression in Veterans. The main questions it aims to answer "
                           "are: Does psilocybin lower depression scores after 6 weeks? Is it safe in Veterans >21 "
                           "years old? Participants will attend preparation and integration sessions.")
    assert new.published_at == datetime(2026, 10, 2, tzinfo=UTC)
    assert new.meta == {"nct": "NCT07012345", "status": "NOT_YET_RECRUITING", "phases": ["PHASE2"],
                        "has_results": False, "conditions": ["Treatment Resistant Depression", "Depression"],
                        "interventions": ["Psilocybin", "Niacin"], "sponsor": "VA Office of Research and Development",
                        "change": "new"}

    results = by_nct["NCT03444444"]
    assert results.meta["has_results"] is True and results.meta["phases"] == ["PHASE2", "PHASE3"]
    assert results.ext_id == "NCT03444444:2026-10-03"
    assert by_nct["NCT06666666"].meta["phases"] == []  # observational: no phases
    assert by_nct["NCT06666666"].summary == "Registry of older adults prescribed dronabinol & their appetite."
    # kept for its other name "Psilocybin"; meta lists the intervention names only
    assert by_nct["NCT06777777"].meta["interventions"] == ["COMP360", "Placebo"]

    # never a dose from the intervention descriptions, never the other names
    for item in result.items:
        blob = json.dumps([item.title, item.summary, item.meta])
        assert not any(word in blob for word in NOT_SHOWN)

    assert result.cursor == {"data_timestamp": STAMP, "baseline": True, "statuses": {
        "NCT00000001": "COMPLETED", "NCT07012345": "NOT_YET_RECRUITING", "NCT05222222": "ACTIVE_NOT_RECRUITING",
        "NCT03444444": "COMPLETED", "NCT06666666": "RECRUITING", "NCT06777777": "ACTIVE_NOT_RECRUITING"}}


async def test_ctgov_first_run_has_no_status_label():
    # no cursor (the first run, and always --check): no history, and no baseline requests either
    http = _ctgov_http()
    result = await ctgov.fetch(http, SRC["ctgov"], {}, NOW)
    assert {i.meta["nct"]: i.meta["change"] for i in result.items} == {
        "NCT07012345": "new", "NCT05222222": "update", "NCT03444444": "results", "NCT06666666": "update",
        "NCT06777777": "update"}
    assert len(http.calls) == 3 and not [c for c in http.calls if c["params"].get("pageSize") == "1000"]
    assert result.cursor["data_timestamp"] == STAMP and "baseline" not in result.cursor


async def test_ctgov_status_baseline_once_then_status_changes_are_seen():
    http = _ctgov_http()
    first = await ctgov.fetch(http, SRC["ctgov"], {"data_timestamp": "2026-10-02T09:00:03"}, NOW)
    window = [c for c in http.calls[1:] if c["params"]["pageSize"] == "100"]
    base = [c for c in http.calls[1:] if c["params"]["pageSize"] == "1000"]
    assert len(window) == 2 and len(base) == 2 and len(http.calls) == 5 and all(c["curl_fallback"] for c in base)
    assert base[0]["params"] == {"format": "json", "query.intr": window[0]["params"]["query.intr"],
                                 "fields": "NCTId,BriefTitle,OverallStatus,LastUpdatePostDate,InterventionName,"
                                           "InterventionOtherName", "pageSize": "1000"}
    assert base[1]["params"] == {**base[0]["params"], "pageToken": "QmFzZWxpbmUy"}
    # ours only: by name ("Smoked marijuana", "Cannabigerol") or other name ("Psilocybin" for COMP360);
    # not sertraline, not the LSD1 inhibitor, not the unparsable record
    statuses = first.cursor["statuses"]
    assert {k: statuses.get(k) for k in ("NCT04111111", "NCT03222333", "NCT06111222", "NCT05222222")} == {
        "NCT04111111": "RECRUITING", "NCT03222333": "ENROLLING_BY_INVITATION", "NCT06111222": "NOT_YET_RECRUITING",
        "NCT05222222": "ACTIVE_NOT_RECRUITING"}
    assert not {"NCT04999999", "NCT02555555", "NCT04333333"} & set(statuses) and len(statuses) == 8

    # The next day a study known only from the baseline completes: a status change, which the filters keep.
    moved = json.loads(fixture("ctgov_baseline_1.json"))["studies"][1]
    moved["protocolSection"]["statusModule"] = {"overallStatus": "COMPLETED",
                                                "lastUpdatePostDateStruct": {"date": "2026-10-04"},
                                                "studyFirstPostDateStruct": {"date": "2021-02-01"}}
    version = _json({"dataTimestamp": "2026-10-04T09:00:04"})
    http = FakeHttp([("GET", r"/version$", version), ("GET", r"/studies\?", _json({"studies": [moved]}))])
    second = await ctgov.fetch(http, SRC["ctgov"], first.cursor, NOW)
    assert len(http.calls) == 2  # no second baseline
    item, = second.items
    assert item.meta["change"] == "status" and drop_reason(item, SRC["ctgov"]) is None
    assert second.cursor["baseline"] is True and second.cursor["statuses"]["NCT04111111"] == "COMPLETED"


async def test_ctgov_window_goes_out_when_the_baseline_fails():
    cursor = {"data_timestamp": "2026-10-02T09:00:03"}
    result = await ctgov.fetch(_ctgov_http(baseline=HttpError(403, ctgov.API)), SRC["ctgov"], cursor, NOW)
    assert len(result.items) == 5
    assert "baseline" not in result.cursor and len(result.cursor["statuses"]) == 5   # tried again next run
    result = await ctgov.fetch(_ctgov_http(baseline=b"<html>Access Denied</html>"), SRC["ctgov"], cursor, NOW)
    assert len(result.items) == 5 and "baseline" not in result.cursor

    def timeout(call):
        raise asyncio.TimeoutError
    result = await ctgov.fetch(_ctgov_http(baseline=timeout), SRC["ctgov"], cursor, NOW)
    assert len(result.items) == 5 and "baseline" not in result.cursor


async def test_ctgov_slow_baseline_gives_up_within_its_budget(monkeypatch):
    class SlowBaseline(FakeHttp):
        async def get(self, url, **kw):
            if (kw.get("params") or {}).get("pageSize") == "1000":
                await asyncio.sleep(30)
            return await super().get(url, **kw)
    monkeypatch.setattr(ctgov, "BASELINE_DEADLINE_S", 0.05)
    http = SlowBaseline(_ctgov_http().routes)
    result = await asyncio.wait_for(
        ctgov.fetch(http, SRC["ctgov"], {"data_timestamp": "2026-10-02T09:00:03"}, NOW), 5)
    assert len(result.items) == 5 and "baseline" not in result.cursor and result.cursor["data_timestamp"] == STAMP


async def test_ctgov_baseline_pages_are_capped():
    def page(call):
        if call["params"]["pageSize"] == "100":
            return _json({"studies": []})
        n = int(call["params"].get("pageToken", "t0")[1:])
        return _json({"studies": [], "nextPageToken": f"t{n + 1}"})
    http = FakeHttp([("GET", r"/version$", fixture("ctgov_version.json")), ("GET", r"/studies\?", page)])
    result = await ctgov.fetch(http, SRC["ctgov"], {"data_timestamp": "x"}, NOW)
    assert len(http.calls) == 2 + ctgov.BASELINE_PAGES and result.cursor["baseline"] is True

    http = FakeHttp([("GET", r"/version$", fixture("ctgov_version.json")),
                     ("GET", r"/studies\?", _json({"studies": [], "nextPageToken": "same"}))])
    await ctgov.fetch(http, SRC["ctgov"], {"baseline": True}, NOW)
    await ctgov.fetch(http, SRC["ctgov"], {"data_timestamp": "x"}, NOW)
    assert [c["params"].get("pageToken") for c in http.calls if c["params"].get("pageSize") == "1000"] == [
        None, "same"]


async def test_ctgov_statuses_capped():
    old = {f"NCT{n:08d}": "COMPLETED" for n in range(1, ctgov.MAX_STATUSES + 1)}
    result = await ctgov.fetch(_ctgov_http(), SRC["ctgov"], {"data_timestamp": "x", "statuses": old, "baseline": True},
                               NOW)
    statuses = result.cursor["statuses"]
    assert len(statuses) == ctgov.MAX_STATUSES
    assert "NCT00000001" not in statuses and "NCT00000005" not in statuses  # the oldest five made room
    assert "NCT00000006" in statuses and list(statuses)[-1] == "NCT06777777"


async def test_ctgov_at_most_five_pages():
    def page(call):
        n = int(call["params"].get("pageToken", "t0")[1:])
        return _json({"studies": [], "nextPageToken": f"t{n + 1}"})
    http = FakeHttp([("GET", r"/version$", fixture("ctgov_version.json")), ("GET", r"/studies\?", page)])
    result = await ctgov.fetch(http, SRC["ctgov"], {"baseline": True}, NOW)
    assert len(http.calls) == 6 and result.items == []
    assert [c["params"].get("pageToken") for c in http.calls[1:]] == [None, "t1", "t2", "t3", "t4"]


async def test_ctgov_stops_on_repeated_token():
    def page(call):
        return _json({"studies": [], "nextPageToken": "same"})
    http = FakeHttp([("GET", r"/version$", fixture("ctgov_version.json")), ("GET", r"/studies\?", page)])
    await ctgov.fetch(http, SRC["ctgov"], {"baseline": True}, NOW)
    assert [c["params"].get("pageToken") for c in http.calls[1:]] == [None, "same"]


def test_ctgov_text_keeps_query_strings_and_decodes_every_level():
    study = json.loads(fixture("ctgov_studies_1.json"))["studies"][0]
    study["protocolSection"]["identificationModule"]["briefTitle"] = "MDMA &amp;amp; PTSD"
    study["protocolSection"]["descriptionModule"]["briefSummary"] = (
        "Details: https://x.org/?a=1&section=2&reg=3 &amp; more\\&amp;#8212;fine")
    (s,) = ctgov.parse_studies(_json({"studies": [study]}))[0]
    assert s.title == "MDMA & PTSD"
    assert s.summary == "Details: https://x.org/?a=1&section=2&reg=3 & more—fine"


@pytest.mark.parametrize("version, studies, reason", [
    (b"<html>Access Denied</html>", None, "version: not JSON"),
    (b'{"apiVersion":"2.0.5"}', None, "no dataTimestamp"),
    (None, b'{"totalCount": 0}', "no studies"),
    (None, Response(200, b"<html>Access Denied</html>", ""), "not JSON"),
])
async def test_ctgov_bad_response(version, studies, reason):
    http = FakeHttp([("GET", r"/version$", version or fixture("ctgov_version.json")),
                     ("GET", r"/studies\?", studies)])
    with pytest.raises(ValueError, match=reason):
        await ctgov.fetch(http, SRC["ctgov"], {}, NOW)


@pytest.mark.parametrize("name, kept", [
    ("Psilocybin", True), ("COMP360 psilocybin", True), ("MDMA-assisted therapy", True),
    ("Midomafetamine (MDMA)", True), ("LSD", True), ("Iadademstat (LSD1 inhibitor)", False), ("5-MeO-DMT", True),
    ("Dimethyl trisulfide (DMTS)", False), ("Noribogaine", True), ("Cannabidiol oral solution", True),
    ("Sertraline", False), ("COMP360", False), ("d-LSD", True), ("SPL026 (DMT fumarate)", True),
    ("Lysergic acid diethylamide (LSD)", True), ("N,N-Dimethyltryptamine (DMT)", True),
    ("3,4-Methylenedioxymethamphetamine (MDMA)", True), ("Disease-Modifying Therapy (DMT)", False),
    ("Dance/Movement Therapy (DMT)", False), ("DMT (dance movement therapy) or 5-MeO-DMT", True),
    ("Delta-9-tetrahydrocannabinol", True), ("Medical cannabis oil", True), ("", False),
    ("Marijuana", True), ("Smoked marijuana cigarettes", True), ("Marihuana", True), ("Cannabinoids", True),
    ("Synthetic cannabinoid", True), ("Cannabigerol", True), ("Psilocin", True),
])
def test_ctgov_intervention_matcher(name, kept):
    assert ctgov.intervention_matcher(SRC["ctgov"].params["interventions"])(name) is kept
