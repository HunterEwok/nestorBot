"""One adapter per kind of source. Each module has

    async def fetch(http: Http, source: Source, cursor: dict, now: datetime) -> FetchResult

which makes the requests (through nestor.news.http.Http only) and returns every parsed
item, filtering left to nestor.news.filters, plus pure parse functions over response
bodies that the tests feed with saved responses from tests/news_fixtures/.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from . import arctic, biorxiv, ctgov, europepmc, feeds, pubmed, reggaeville, tme

Fetch = Callable[..., Awaitable["FetchResult"]]  # noqa: F821 - see nestor.news.model

ADAPTERS: dict[str, Fetch] = {
    "rss": feeds.fetch,
    "pubmed": pubmed.fetch,
    "europepmc": europepmc.fetch,
    "biorxiv": biorxiv.fetch,
    "ctgov": ctgov.fetch,
    "arctic": arctic.fetch,
    "tme": tme.fetch,
    "reggaeville": reggaeville.fetch,
}
