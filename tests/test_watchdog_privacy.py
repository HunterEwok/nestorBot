"""Stage 4 watchdog: no secret and no message's words in anything it writes. Canaries go into the bot
token, the Hermes keys, the database password and role, a message, a question to the bot, a news source's
error and the error codes of the files and rows it reads (nestor.backup's status, maintenance_runs, the
collector's heartbeat); none may come out in an alert, the daily summary, a log line, state.json,
optouts.json or ops_state."""
import json
import logging
from dataclasses import replace
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiogram.exceptions import TelegramNetworkError
from aiohttp import web

from nestor.logs import JsonFormatter
from nestor.watchdog import alerts, run

from .factories import CHAT_ID
from .test_watchdog_checks import AlertBot, Systemd, host, settings

MSK = ZoneInfo("Europe/Moscow")
TOKEN = "123456:CANARY-bot-token"
HERMES_KEY = "hermes-CANARY-key"
DB_ROLE, DB_PASSWORD = "canary_role", "CANARY-db-password"
TEXT = "КАНАРЕЙКА: секретное сообщение"
SERVER_REPLY = "error: CANARY <b>server</b> reply"


@pytest.fixture
async def hermes_rejecting():
    """A Hermes gateway that turns every key away, repeating it in the reply as a careless server might."""
    async def models(request):
        return web.Response(status=401, text="bad key: " + request.headers.get("Authorization", ""))

    app = web.Application()
    app.router.add_get("/p/{profile}/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    await runner.cleanup()


async def test_no_message_text_or_secrets_anywhere(database, conn, tmp_path, hermes_rejecting, caplog):
    caplog.set_level(logging.DEBUG)
    t0 = datetime.now(MSK)
    # What the database holds: words of people, and what servers and other programs said.
    await conn.execute("INSERT INTO chats (chat_id, type, title) VALUES ($1, 'supergroup', 'Nestor')", CHAT_ID)
    await conn.execute("INSERT INTO users (user_id, first_name) VALUES (111, 'U'), (999, 'Gone')")
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 999")
    await conn.execute("""INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw, stored_at)
                          VALUES ($1, 1, 111, now(), $2, jsonb_build_object('text', $2::text), now() - interval '1 day')""",
                       CHAT_ID, TEXT)
    await conn.execute("""INSERT INTO bot_requests (chat_id, trigger_msg_id, user_id, scenario, question, answer, status)
                          SELECT $1, g, 111, 'search', $2, $2, 'error' FROM generate_series(1, 3) g""", CHAT_ID, TEXT)
    await conn.execute("""INSERT INTO news_sources (key, last_run_at, last_ok_at, status, failures)
                          VALUES ('pubmed', now(), now() - interval '3 days', $1, 5),
                                 ('arctic', now(), now() - interval '3 days', $1, 7)""", SERVER_REPLY)
    await conn.execute("""INSERT INTO maintenance_runs (job, day, status, attempts, error, updated_at)
                          VALUES ('retention', current_date, 'failed', 6, $1, now())""", SERVER_REPLY)
    beat = {"started_at": (t0 - timedelta(hours=1)).isoformat(), "updates_ok_at": (t0 - timedelta(minutes=30)).isoformat(),
            "updates_failures": 9, "updates_error": SERVER_REPLY, "sent": 0, "send_failed": 0, "bad_request": 0, "flood": 0}
    await conn.execute("INSERT INTO ops_state (key, value, updated_at) VALUES ('collector', $1, now())", beat)
    status_file = tmp_path / "backup" / "status.json"
    status_file.parent.mkdir()
    status_file.write_text(json.dumps({
        "version": 1, "running": False, "ok": False, "error": SERVER_REPLY, "failed_nights": 2,
        "last_ok_at": (t0 - timedelta(days=2)).isoformat(), "oldest_dump_at": (t0 - timedelta(days=9)).isoformat(),
        "warnings": [SERVER_REPLY, "shrank"], "shrank_pct": 40, "kept": 7, "last_ok_size": 10 ** 9}))
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    bot = AlertBot(token=TOKEN)
    bot.errors = [TelegramNetworkError(method=None, message=f"could not reach api.telegram.org/bot{TOKEN}")]
    good = database["collector"]
    bad = good.replace("nestor:collector@", f"{DB_ROLE}:{DB_PASSWORD}@")
    assert bad != good

    def cfg(url, **kw):
        return replace(settings(url, alert_quiet=None, backup_status_file=str(status_file),
                                hermes_url=f"{hermes_rejecting}/p/group/v1", hermes_api_key=HERMES_KEY,
                                hermes_news_url=f"{hermes_rejecting}/p/news/v1", hermes_news_api_key=HERMES_KEY, **kw),
                       bot_token=TOKEN)

    reports = []
    async with aiohttp.ClientSession() as http:
        for url, minutes, daily in ((bad, -20, None), (bad, -15, None), (good, 0, time(0, 0)), (good, 5, None)):
            reports.append(await run.run_once(cfg(url, alert_daily_at=daily), now=t0 + timedelta(minutes=minutes),
                                              bot=bot, http=http, systemctl=Systemd(), host=host(), state_dir=state_dir))

    # Every path was taken: the database refused, then each canary's check spoke, and the summary went out.
    assert {"db.unreachable", "backup.failed"} <= {p["key"] for p in reports[1].snapshot["problems"]}
    assert {"backup.failed", "backup.warning:shrank", "hermes.profile:group", "hermes.profile:news",
            "telegram.polling", "model.errors", "retention.failed", "news.sources"} <= {
        p["key"] for p in reports[3].snapshot["problems"]}
    assert "db.unreachable" not in {p["key"] for p in reports[3].snapshot["problems"]}
    assert reports[2].summary is not None and "Nestor" in reports[2].summary
    assert "alert send failed" in caplog.text

    sent = [m["text"] for m in bot.sent]
    logs = [JsonFormatter().format(r) for r in caplog.records if not r.name.startswith("aiohttp.")]
    files = [p.read_text(encoding="utf-8") for p in state_dir.iterdir() if p.is_file()]
    assert {p.name for p in state_dir.iterdir()} >= {run.STATE_FILE, run.OPTOUTS_FILE}
    rows = [r["value"] for r in await conn.fetch("SELECT key, value::text FROM ops_state WHERE key <> 'collector'")]
    assert rows
    shown = [alerts.render_status(r.snapshot, t0, MSK) for r in reports] + [
        r.message.text for r in reports if r.message is not None]
    everything = "\n".join(sent + logs + files + rows + shown).lower()
    for canary in ("canary", "канарейка", "секретное", "server</b>", "bad key"):
        assert canary not in everything, canary
