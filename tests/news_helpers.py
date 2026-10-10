"""Fake HTTP for the news adapters: saved responses from tests/news_fixtures/, no network."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

from nestor.news.http import HttpError, Response

FIXTURES = Path(__file__).resolve().parent / "news_fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeHttp:
    """routes: list of (method, regex over the full URL with query, response). A response is
    bytes (status 200), a Response, an HttpError (raised) or a callable(call) returning one
    of those. The first matching route wins; no match raises HttpError(404)."""

    def __init__(self, routes=(), contact: str = ""):
        self.routes = list(routes)
        self.contact = contact
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, pattern: str, response) -> "FakeHttp":
        self.routes.append((method, pattern, response))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url: str, *, params: Mapping[str, Any] | None = None,
                  headers: Mapping[str, str] | None = None, curl_fallback: bool = False,
                  proxy: str | None = None) -> Response:
        full = url + (("&" if "?" in url else "?") + urlencode(params, doseq=True) if params else "")
        return self._answer({"method": "GET", "url": full, "params": dict(params or {}), "headers": headers,
                             "curl_fallback": curl_fallback, "proxy": proxy})

    async def post(self, url: str, *, data: Mapping[str, Any]) -> Response:
        return self._answer({"method": "POST", "url": url, "data": dict(data), "params": {}})

    def _answer(self, call: dict) -> Response:
        self.calls.append(call)
        for method, pattern, response in self.routes:
            if method == call["method"] and re.search(pattern, call["url"]):
                if callable(response) and not isinstance(response, (bytes, Response, HttpError)):
                    response = response(call)
                if isinstance(response, HttpError):
                    raise response
                if isinstance(response, bytes):
                    return Response(200, response, call["url"])
                return response
        raise HttpError(404, call["url"])
