"""Stage 4 watchdog, its probes: the Hermes gateway on a local server, systemctl's output, the free space
on the card and a card that cannot be written."""
import asyncio
import logging
import socket
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp import web

from nestor.bot import prompts
from nestor.watchdog import alerts, checks, probes, run
from nestor.watchdog.alerts import AlertState
from nestor.watchdog.checks import Ctx

from .test_watchdog_checks import BY_NAME, AlertBot, Systemd, host, settings

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 5, 14, 0, tzinfo=MSK)


def ctx_without_db(*, state=None, http=None, systemd=None, hst=None, **kw) -> Ctx:
    return Ctx(settings(**kw), NOW, MSK, state or AlertState(), None, None, http, systemd or Systemd(), hst or host())


@pytest.fixture
async def hermes():
    """A Hermes gateway: /p/<profile>/v1/models answers by profile; `seen` keeps the keys it was sent."""
    seen = []

    async def models(request):
        seen.append(request.headers.get("Authorization"))
        profile = request.match_info["profile"]
        if profile == "slow":
            await asyncio.sleep(2)
        if profile in ("web", "gone"):
            return web.Response(status=404)
        if profile == "broken":
            return web.Response(status=500, text="Traceback: secret things")
        if request.headers.get("Authorization") != "Bearer good-key":
            return web.Response(status=401, text="bad key " + request.headers.get("Authorization", ""))
        return web.json_response({"data": [{"id": "hermes-agent"}]})

    app = web.Application()
    app.router.add_get("/p/{profile}/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", seen
    await runner.cleanup()


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_hermes_probe(hermes, caplog):
    base, seen = hermes
    url = f"{base}/p/{{}}/v1".format
    async with aiohttp.ClientSession() as http:
        # Every profile fine.
        ctx = ctx_without_db(http=http, hermes_url=url("group"), hermes_api_key="good-key",
                             hermes_news_url=url("news"), hermes_news_api_key="good-key")
        assert (await run.run_check(ctx, BY_NAME["hermes"])).status == "ok"
        assert seen == ["Bearer good-key"] * 2
        # A wrong key, a profile that is missing, one that breaks: one line each.
        ctx = ctx_without_db(http=http, hermes_url=url("group"), hermes_api_key="good-key",
                             hermes_deep_url=url("deep"), hermes_deep_api_key="old-key", hermes_web_url=url("web"),
                             hermes_web_api_key="good-key", hermes_smart_url=url("broken"),
                             hermes_smart_api_key="good-key")
        res = await run.run_check(ctx, BY_NAME["hermes"])
        assert [(f.key, f.line, f.confirm, f.clear) for f in res.findings] == [
            ("hermes.profile:deep", prompts.ALERT_HERMES_PROFILE.format(profile="deep", problem="ключ не подходит (HTTP 401)"),
             2, 2),
            ("hermes.profile:smart", prompts.ALERT_HERMES_PROFILE.format(profile="smart", problem="отвечает HTTP 500"), 2, 2),
            ("hermes.profile:web", prompts.ALERT_HERMES_PROFILE.format(profile="web", problem=prompts.HERMES_MISSING), 2, 2)]
        # 404 everywhere: this Hermes has no /models. It answers, so the gateway is up; of the profiles nothing
        # can be told, said once in the log.
        state = AlertState()
        for _ in range(2):
            ctx = ctx_without_db(http=http, state=state, hermes_url=url("gone"), hermes_web_url=url("web"))
            res = await run.run_check(ctx, BY_NAME["hermes"])
            assert (res.status, res.findings, res.unknown) == ("ok", [], ("hermes.profile:",))
        assert caplog.text.count("404 to /models") == 1
        # The gateway is down: one line for it, the profiles neither raised nor cleared.
        down = f"http://127.0.0.1:{closed_port()}/p/{{}}/v1".format
        ctx = ctx_without_db(http=http, hermes_url=down("group"), hermes_web_url=down("web"))
        res = await run.run_check(ctx, BY_NAME["hermes"])
        assert [(f.key, f.line, f.confirm) for f in res.findings] == [
            ("hermes.gateway", prompts.ALERT_HERMES_DOWN.format(error="в соединении отказано"), 2)]
        assert res.unknown == ("hermes.profile:",)
        # Down, then back answering 404 everywhere: that it is back is said; a profile it knew of stays.
        state = AlertState(problems={"hermes.profile:web": alerts.Problem(
            "hermes.profile:web", "hermes", "crit", "Профиль web", "Hermes", NOW, fails=2, announced="crit")})
        texts = []
        for minutes, urls in ((0, down), (5, down), (10, url), (15, url)):
            ctx = ctx_without_db(http=http, state=state, hermes_url=urls("gone"), hermes_web_url=urls("web"))
            res = await run.run_check(ctx, BY_NAME["hermes"])
            state, out = alerts.evaluate(state, [res], NOW + timedelta(minutes=minutes), tz=MSK, quiet=None)
            if out is not None:
                alerts.delivered(state, out, NOW + timedelta(minutes=minutes))
                texts.append(out.text)
        assert len(texts) == 2 and "Hermes не отвечает" in texts[0] and "снова в порядке" in texts[1]
        assert set(state.problems) == {"hermes.profile:web"}
        # No answer within the time: a timeout, the same gateway line.
        assert await probes.probe_hermes(http, [("smart", url("slow"), "good-key")], timeout_s=0.2) == {"smart": "timeout"}
    # Archiving only: nothing to probe.
    assert (await run.run_check(ctx_without_db(), BY_NAME["hermes"])).status == "ok"


async def test_parse_systemctl_show(tmp_path, monkeypatch):
    shown = probes.parse_show("ActiveState=activating\nSubState=auto-restart\nResult=oom-kill\nNRestarts=3\n"
                              "ExecMainStatus=9\nMemoryPeak=[not set]\n\n")
    assert shown == {"ActiveState": "activating", "SubState": "auto-restart", "Result": "oom-kill", "NRestarts": "3",
                     "ExecMainStatus": "9", "MemoryPeak": "[not set]"}
    assert (probes.as_int(shown["NRestarts"]), probes.as_int(shown["MemoryPeak"]), probes.as_int("infinity"),
            probes.as_int(None)) == (3, None, None, None)
    # The real call, with a stand-in systemctl on the PATH.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "systemctl"
    script.write_text('#!/bin/sh\n[ "$2" = "nestor-collector" ] || exit 4\necho "ActiveState=active"\necho "Args=$*"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    shown = probes.parse_show(await probes.run_systemctl("show", "nestor-collector", "-p", "ActiveState"))
    assert shown == {"ActiveState": "active", "Args": "show nestor-collector -p ActiveState"}
    with pytest.raises(probes.ProbeError, match="systemctl_exit_4"):
        await probes.run_systemctl("show", "other")
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    with pytest.raises(probes.ProbeError, match="FileNotFoundError"):
        await probes.run_systemctl("show", "nestor-collector")
    # Without systemctl the checks that need it are broken checks, said as such; the others go on.
    res = await run.run_check(ctx_without_db(systemd=probes.run_systemctl), BY_NAME["collector"])
    assert [f.key for f in res.findings] == ["watchdog.check:collector"] and res.status == "error"
    assert res.findings[0].line == prompts.ALERT_CHECK_BROKEN.format(name="collector", error="ProbeError")


async def test_disk_hysteresis():
    state = AlertState()

    async def look(free_gb):
        res = await run.run_check(ctx_without_db(state=state, hst=host(free_gb)), BY_NAME["disk"])
        alerts.evaluate(state, [res], NOW, tz=MSK, quiet=None)
        return [(f.level, f.line) for f in res.findings]

    assert await look(5.5) == []
    assert await look(4.5) == [("warn", prompts.ALERT_DISK.format(free="4,5 ГБ", total="58,0 ГБ"))]
    assert await look(5.5) == [("warn", prompts.ALERT_DISK.format(free="5,5 ГБ", total="58,0 ГБ"))]   # not on a wobble
    assert await look(0.5) == [("crit", prompts.ALERT_DISK.format(free="512 МБ", total="58,0 ГБ"))]
    assert await look(6.1) == []
    assert "disk.low:/" not in state.problems
    assert await look(5.5) == []                                          # closed: back to the threshold


async def test_sd_write_failure_immediate(database, pool, tmp_path, caplog):
    assert checks.card_result(tmp_path).status == "ok" and (tmp_path / "probe").exists()
    card = tmp_path / "card"
    card.write_text("a file where the directory should be: every write under it fails")
    res = checks.card_result(card)
    assert [(f.key, f.level, f.confirm) for f in res.findings] == [("sd.write", "crit", 1)]
    assert res.findings[0].line == prompts.ALERT_SD_WRITE.format(error="<code>ENOTDIR</code>")
    # A whole run on that card: said at once; the state cannot be saved, the snapshot in the database
    # keeps what was announced, so the next run does not say it again.
    bot = AlertBot()
    cfg = settings(database["collector"], alert_daily_at=None, alert_quiet=None)
    only = tuple(c for c in checks.CHECKS if c.name in ("migrations", "reminders"))
    now = datetime.now(MSK)
    report = await run.run_once(cfg, now=now, bot=bot, systemctl=Systemd(), host=host(), state_dir=card, checks=only)
    assert [m["text"] for m in bot.sent] == [f"{prompts.ALERT_NEW}\n🔴 {res.findings[0].line}"]
    assert report.sent and not bot.sent[0]["disable_notification"]
    assert "watchdog state not saved" in caplog.text
    await run.run_once(cfg, now=now + timedelta(minutes=5), bot=bot, systemctl=Systemd(), host=host(), state_dir=card,
                       checks=only)
    assert len(bot.sent) == 1
    # The card is back: the state is saved again and the problem resolved.
    fixed = tmp_path / "fixed"
    fixed.mkdir()
    await run.run_once(cfg, now=now + timedelta(minutes=10), bot=bot, systemctl=Systemd(), host=host(),
                       state_dir=fixed, checks=only)
    assert "Запись на карту Pi: снова в порядке" in bot.sent[-1]["text"] and (fixed / run.STATE_FILE).exists()


def test_safe_status_safe_code_and_read_json(tmp_path):
    for status in ("ok", "skipped", "timeout", "http_403"):
        assert probes.safe_status(status) == status
    for status in ("error: Server says <b>secret</b>", "http_40", "ok ", None, "HTTP_403"):
        assert probes.safe_status(status) == "error"
    for code in ("pg_dump_failed", "CheckViolationError", "401", "5xx", "clock_skew"):
        assert probes.safe_code(code) == code
    for code in ("error: Server says <b>secret</b>", "a b", "", "x" * 65, None):
        assert probes.safe_code(code) == "error"
    assert probes.read_json(tmp_path / "missing.json") is None
    (tmp_path / "list.json").write_text("[1]")
    with pytest.raises(TypeError):
        probes.read_json(tmp_path / "list.json")


def test_hermes_profiles_are_the_configured_ones():
    s = replace(settings(), hermes_url="http://h/p/group/v1", hermes_api_key="k1", hermes_news_url="http://h/p/news/v1")
    assert probes.hermes_profiles(s) == [("group", "http://h/p/group/v1", "k1"), ("news", "http://h/p/news/v1", "")]
    assert probes.hermes_profiles(settings()) == []
    logging.getLogger("nestor.watchdog")    # the probes log nothing of their own
