import pytest
from aiohttp import web

from nestor.bot.llm import HermesClient, LLMError


@pytest.fixture
async def hermes():
    seen = {}

    async def completions(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = await request.json()
        if seen["body"]["messages"][-1]["content"] == "fail":
            return web.json_response({"error": "boom"}, status=500)
        return web.json_response({"model": "claude-x", "choices": [{"message": {"content": " Привет "}}],
                                  "usage": {"prompt_tokens": 50, "completion_tokens": 7}})

    app = web.Application()
    app.router.add_post("/p/group/v1/chat/completions", completions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/p/group/v1", seen
    await runner.cleanup()


async def test_completion_and_usage(hermes):
    url, seen = hermes
    result = await HermesClient(url, "secret").complete([{"role": "user", "content": "hi"}])
    assert (result.text, result.model, result.input_tokens, result.output_tokens) == ("Привет", "claude-x", 50, 7)
    assert seen["auth"] == "Bearer secret" and seen["body"]["stream"] is False
    assert seen["body"]["model"] == "hermes-agent"


async def test_http_error_does_not_leak_body(hermes):
    url, _ = hermes
    with pytest.raises(LLMError, match="HTTP 500"):
        await HermesClient(url, "secret").complete([{"role": "user", "content": "fail"}])
