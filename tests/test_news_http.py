"""nestor.news.http against a real local server: whole bodies however they are sent, the size
cap, errors and the curl fallback."""
import asyncio
import gzip
import random
import shutil
import zlib

import pytest
from aiohttp import web

from nestor.news import http as news_http
from nestor.news.http import Http, HttpError

BIG = random.Random(1).randbytes(300_000)   # incompressible: it crosses the socket in many pieces


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(news_http, "DEFAULT_PAUSE_S", 0)


@pytest.fixture
async def server():
    hits = {"guarded": 0}

    async def plain(request):
        return web.Response(body=BIG)

    async def chunked(request):
        size = int(request.query.get("size", len(BIG)))
        resp = web.StreamResponse()
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        try:
            for i in range(0, size, 10_000):
                await resp.write(BIG[i:min(i + 10_000, size)])
                await asyncio.sleep(0)
            await resp.write_eof()
        except ConnectionResetError:   # the client stopped reading at its cap
            pass
        return resp

    async def compressed(request):
        coding = request.match_info["coding"]
        data = bytes(1_000_000) if "bomb" in request.query else BIG   # a megabyte of zeros packs into a kilobyte
        body = gzip.compress(data) if coding == "gzip" else zlib.compress(data)
        return web.Response(body=body, headers={"Content-Encoding": coding})

    async def limited(request):
        return web.Response(status=429, headers={"Retry-After": "120"})

    async def guarded(request):
        hits["guarded"] += 1   # Python is refused and curl, asking next, is served, like ClinicalTrials.gov does
        if hits["guarded"] % 2:
            return web.Response(status=403)
        data = bytes(int(request.query["zeros"])) if "zeros" in request.query else BIG
        return web.Response(body=gzip.compress(data), headers={"Content-Encoding": "gzip"})

    app = web.Application()
    app.router.add_get("/plain", plain)
    app.router.add_get("/chunked", chunked)
    app.router.add_get("/compressed/{coding}", compressed)
    app.router.add_get("/limited", limited)
    app.router.add_get("/guarded", guarded)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", hits
    await runner.cleanup()


async def test_large_bodies_come_back_whole(server):
    url, _ = server
    async with Http() as http:
        for path in ("/plain", "/chunked", "/compressed/gzip", "/compressed/deflate"):
            resp = await http.get(url + path)
            assert (path, resp.status, len(resp.body)) == (path, 200, len(BIG)) and resp.body == BIG


async def test_bodies_over_the_cap_are_refused(server):
    url, _ = server
    async with Http(max_bytes=100_000) as http:
        # by Content-Length, by counting a chunked body, and by the decompressed size of a small gzip
        for path in ("/plain", "/chunked", "/compressed/gzip?bomb=1", "/compressed/deflate?bomb=1"):
            with pytest.raises(HttpError) as e:
                await http.get(url + path)
            assert (path, e.value.status) == (path, 413)
        resp = await http.get(url + "/chunked", params={"size": 100_000})   # exactly the cap is fine
        assert resp.body == BIG[:100_000]


async def test_errors_carry_status_and_retry_after(server):
    url, _ = server
    async with Http() as http:
        with pytest.raises(HttpError) as e:
            await http.get(url + "/limited")
        assert (e.value.status, e.value.retry_after, e.value.url) == (429, 120.0, url + "/limited")
        with pytest.raises(HttpError) as e:
            await http.get(url + "/missing")
        assert (e.value.status, e.value.retry_after) == (404, None)


@pytest.mark.skipif(shutil.which("curl") is None, reason="no curl")
async def test_curl_fallback_returns_the_whole_body(server):
    url, hits = server
    async with Http() as http:
        resp = await http.get(url + "/guarded", curl_fallback=True)
    assert hits["guarded"] == 2 and resp.status == 200 and resp.body == BIG


@pytest.mark.skipif(shutil.which("curl") is None, reason="no curl")
async def test_curl_fallback_keeps_the_cap(server):
    url, _ = server
    async with Http(max_bytes=100_000) as http:
        # curl's --max-filesize counts packed bytes: zeros pack into almost nothing and get past it,
        # a megabyte of them as well as one byte over the cap; a big packed body curl refuses itself
        for params in ({"zeros": 1_000_000}, {"zeros": 100_001}, None):
            with pytest.raises(HttpError) as e:
                await http.get(url + "/guarded", params=params, curl_fallback=True)
            assert (params, e.value.status) == (params, 413)
        resp = await http.get(url + "/guarded", params={"zeros": 100_000}, curl_fallback=True)   # exactly the cap
    assert resp.status == 200 and resp.body == bytes(100_000)
