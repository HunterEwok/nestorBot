"""PubMed through NCBI E-utilities: ESearch for the PMIDs added in the last two days, then
EFetch XML for titles and abstracts.

Quirks:
- Both calls are POSTs: the query is long, and NCBI asks for POST above about 200 ids.
  EFetch gets 100 PMIDs per call: full records with reference lists run to 20-40 KB each.
- NCBI reports most errors as HTTP 200 with "ERROR" or a non-empty "errorlist" in the body
  (bad parameters, a backend timeout, phrases it could not find); only the rate limit is a 429.
- datetype=edat is the Entrez date, PubMedPubDate[@PubStatus='pubmed'] in the XML; 'entrez'
  there is the create date (crdt) and only a fallback. NLM does not say which clock its
  Hour/Minute use; they are taken as UTC (a few hours do not matter for a daily digest).
- The XML starts with a DOCTYPE pointing to NLM's DTD; expat never fetches it. Entity
  declarations are refused.
- ArticleTitle and AbstractText can hold inline markup (<i>, <sup>, MathML), so text is
  collected with itertext(). Structured abstracts come as several labelled AbstractText parts;
  NLM labels the unlabelled part of one "UNLABELLED", which is not shown.
  Titles of non-English articles are English translations in brackets: "[Title]."; but
  "[18F]FDG PET ... [corrected]." is a tracer name, not a translation. Records without an
  English title carry the placeholder "[Not Available]." and are skipped.
- tool= and email= identify us to NCBI; email only when the owner set a contact.
- Terms: NCBI's Disclaimer and Copyright notice must be visible to users, and abstracts may
  be copyrighted: the abstract only feeds the triage model, the digest shows our own summary.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from ..http import Http
from ..model import FetchResult, Item, Source

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TOOL = "nestorbot"
BATCH = 100       # NCBI wants POST above ~200 ids; 100 full records stay well under Http's 8 MB cap
SUMMARY_MAX = 1500

_PMID = re.compile(r"[0-9]{1,9}")
_DOI = re.compile(r"10\.\d{4,9}/\S+")
_WS = re.compile(r"\s+")
_BRACKETED = re.compile(r"\[(.+)\]\.?")
_NO_TITLE = {"not available", "no title available"}


async def fetch(http: Http, source: Source, cursor: dict, now: datetime) -> FetchResult:
    resp = await http.post(EUTILS + "esearch.fcgi", data={
        "db": "pubmed", "term": source.params["query"], "datetype": "edat", "reldate": "2", "retmax": "500",
        "retmode": "json", **_ident(http)})
    pmids = parse_esearch(resp.body)
    items: list[Item] = []
    for start in range(0, len(pmids), BATCH):
        resp = await http.post(EUTILS + "efetch.fcgi", data={
            "db": "pubmed", "retmode": "xml", "rettype": "abstract", "id": ",".join(pmids[start:start + BATCH]),
            **_ident(http)})
        items += parse_efetch(resp.body)
    return FetchResult(items)


def _ident(http: Http) -> dict[str, str]:
    return {"tool": TOOL, "email": http.contact} if http.contact else {"tool": TOOL}


def _short(value: Any) -> str:
    return _WS.sub(" ", str(value)).strip()[:120]


def parse_esearch(body: bytes) -> list[str]:
    """PMIDs from an ESearch JSON answer; ValueError when NCBI reports an error."""
    try:
        data = json.loads(body)
    except ValueError:
        raise ValueError("esearch: not JSON") from None
    if not isinstance(data, dict):
        raise ValueError("esearch: unexpected JSON")
    if data.get("error"):  # the rate limit: {"error": "API rate limit exceeded", "count": "11"}
        raise ValueError(f"esearch: {_short(data['error'])}")
    result = data.get("esearchresult")
    if not isinstance(result, dict):
        raise ValueError("esearch: no esearchresult")
    if result.get("ERROR"):
        raise ValueError(f"esearch: {_short(result['ERROR'])}")
    errors = result.get("errorlist")
    if isinstance(errors, dict) and any(errors.values()):
        raise ValueError(f"esearch: errorlist {_short(json.dumps(errors))}")
    ids = result.get("idlist")
    if not isinstance(ids, list):
        raise ValueError("esearch: no idlist")
    return list(dict.fromkeys(p for p in (str(v).strip() for v in ids) if _PMID.fullmatch(p)))


def parse_efetch(body: bytes) -> list[Item]:
    """Items from an EFetch PubmedArticleSet; book chapters and broken records are skipped."""
    if b"<!ENTITY" in body:
        raise ValueError("efetch: entity declarations refused")
    if not body.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):  # also refuses UTF-16, which hides <!ENTITY
        raise ValueError("efetch: not XML")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise ValueError(f"efetch: bad XML ({e})") from None
    if root.tag != "PubmedArticleSet":  # <eFetchResult><ERROR>...</ERROR></eFetchResult>
        error = root.findtext(".//ERROR")
        raise ValueError(f"efetch: {_short(error)}" if error else f"efetch: unexpected <{root.tag[:40]}>")
    items: list[Item] = []
    for article in root.findall("PubmedArticle"):
        item = _article(article)
        if item is not None:
            items.append(item)
    return items


def _text(el: ET.Element | None) -> str:
    return "" if el is None else _WS.sub(" ", "".join(el.itertext())).strip()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:.") + "…"


def _doi(value: str | None) -> str | None:
    doi = (value or "").strip().lower().removeprefix("https://doi.org/").removeprefix("doi:").strip()
    return doi if _DOI.fullmatch(doi) else None


def _article(art: ET.Element) -> Item | None:
    citation = art.find("MedlineCitation")
    article = citation.find("Article") if citation is not None else None
    if citation is None or article is None:
        return None
    pmid = (citation.findtext("PMID") or "").strip()
    title = _title(_text(article.find("ArticleTitle")))
    if not _PMID.fullmatch(pmid) or not title:
        return None

    parts = []
    for el in article.findall("Abstract/AbstractText"):
        text = _text(el)
        label = _WS.sub(" ", el.get("Label") or "").strip()
        if label.upper() == "UNLABELLED":
            label = ""
        if text:
            parts.append(f"{label}: {text}" if label else text)

    doi = None
    for el in art.findall("PubmedData/ArticleIdList/ArticleId"):  # not ReferenceList's: those are citations
        if el.get("IdType") == "doi" and (doi := _doi(el.text)):
            break
    if doi is None:
        for el in article.findall("ELocationID"):
            if el.get("EIdType") == "doi" and el.get("ValidYN", "Y") == "Y" and (doi := _doi(el.text)):
                break

    journal = article.find("Journal")
    name = (_text(journal.find("ISOAbbreviation")) or _text(journal.find("Title"))) if journal is not None else ""
    pubtypes = [t for t in (_text(el) for el in article.findall("PublicationTypeList/PublicationType")) if t]
    return Item(ext_id=pmid, url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", title=title,
                summary=_clip(" ".join(parts), SUMMARY_MAX), published_at=_entry_date(art), doi=doi, lang="en",
                meta={"pmid": pmid, "journal": name, "pubtypes": pubtypes})


def _title(title: str) -> str:
    """A bracketed translation unwrapped, only when the first bracket closes at the end; "" for NLM's placeholder."""
    if (m := _BRACKETED.fullmatch(title)) is not None:
        depth = 0
        for ch in m.group(1):
            depth += (ch == "[") - (ch == "]")
            if depth < 0:  # "[18F]FDG ... [corrected]": the first bracket closed early
                break
        else:
            if depth == 0:
                title = m.group(1).strip()
    return "" if title.rstrip(".").strip().lower() in _NO_TITLE else title


def _entry_date(art: ET.Element) -> datetime | None:
    dates = {el.get("PubStatus"): el for el in art.findall("PubmedData/History/PubMedPubDate")}
    for status in ("pubmed", "entrez"):
        el = dates.get(status)
        if el is None:
            continue
        try:
            year, month, day = (int(el.findtext(tag) or "") for tag in ("Year", "Month", "Day"))
            return datetime(year, month, day, int(el.findtext("Hour") or 0), int(el.findtext("Minute") or 0),
                            tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
