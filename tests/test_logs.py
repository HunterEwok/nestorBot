"""Stage 4 logs: what reaches the journal through the handler setup_logging installs. Exceptions by their
class names and frames, never their text (PostgreSQL's DETAIL, asyncpg's copy of a query argument,
aiogram's request URL with the bot token); tokens, database passwords and keys blanked out in any line."""
import asyncio
import io
import json
import logging

import asyncpg
import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetUpdates

from nestor.logs import setup_logging

CANARY = "КАНАРЕЙКА секретный текст"
TOKEN = "123456789:AAHcanaryCANARYcanaryCANARYcanary_-x"
DSN = "postgresql://nestor:CANARYpass@192.168.0.1:5432/nestor"


@pytest.fixture
def journal():
    """The root handler of setup_logging, writing into a buffer: what journald would get. Returns the
    lines parsed, so a line that is not JSON fails the test."""
    root = logging.getLogger()
    before, level = root.handlers[:], root.level
    setup_logging("INFO")
    (handler,) = root.handlers
    root.handlers[:] = before                   # pytest's own capture stays as it was
    root.setLevel(level)
    out = io.StringIO()
    handler.setStream(out)
    root.addHandler(handler)
    try:
        yield lambda: (out.getvalue(), [json.loads(line) for line in out.getvalue().splitlines()])
    finally:
        root.removeHandler(handler)


async def test_postgres_error_detail_never_reaches_the_journal(conn, journal):
    try:
        await conn.execute("INSERT INTO users (user_id, first_name) VALUES (NULL, $1)", CANARY)
    except asyncpg.NotNullViolationError as e:
        assert CANARY in str(e)                 # «DETAIL: Failing row contains (null, КАНАРЕЙКА …)»
        logging.getLogger("nestor.collector").exception("save failed")
    raw, (line,) = journal()
    assert line["msg"] == "save failed" and CANARY not in raw and "Failing row" not in raw
    assert line["exc"].startswith("Traceback (most recent call last):\n")
    assert line["exc"].endswith("asyncpg.exceptions.NotNullViolationError (sqlstate=23502)")
    assert "test_logs.py" in line["exc"]        # where it happened is kept


async def test_data_error_argument_is_not_logged(conn, journal):
    try:
        await conn.fetchval("SELECT $1::bigint", CANARY)
    except asyncpg.DataError as e:
        assert "КАНАРЕЙКА" in str(e)            # asyncpg quotes the argument it could not encode
        logging.getLogger("nestor.collector").exception("query failed", extra={"error": e, "op": "save_message"})
    raw, (line,) = journal()
    assert "КАНАРЕЙКА" not in raw
    assert line["error"] == "asyncpg.exceptions.DataError" and line["op"] == "save_message"
    # Every exception of the chain by its class: the encoder's own error, then asyncpg's.
    assert "\nThe above exception was the direct cause of the following exception:\n" in line["exc"]
    assert line["exc"].endswith("asyncpg.exceptions.DataError (sqlstate=22000)")


async def test_aiogram_args_become_class_names_and_tokens_are_redacted(journal):
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    error = TelegramNetworkError(method=GetUpdates(), message=f"ClientConnectorError: {url} {CANARY} {DSN}")
    assert TOKEN in str(error)
    # As aiogram's polling loop logs it (aiogram/dispatcher/dispatcher.py).
    logging.getLogger("aiogram.dispatcher").error("Failed to fetch updates - %s: %s", type(error).__name__, error)
    # A task that failed with nobody waiting for it: asyncio puts the task and its exception's repr in the text.
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    future.set_exception(ValueError(CANARY))
    future.exception()                          # retrieved: no second report when it is collected
    loop.call_exception_handler({"message": "Future exception was never retrieved",
                                 "exception": future.exception(), "future": future})
    # Whatever a line holds, secrets do not get through, and the line stays JSON.
    logging.getLogger("nestor.watchdog").warning(
        'probe %s', f'{url} Authorization: Bearer hermes-CANARY-key {DSN} '
                    f'api_key=CANARY1&x=1 password="CANARY2 \\" end" PASSWORD=CANARY3')
    raw, lines = journal()
    assert [line["logger"] for line in lines] == ["aiogram.dispatcher", "asyncio", "nestor.watchdog"]
    assert lines[0]["msg"] == "Failed to fetch updates - TelegramNetworkError: aiogram.exceptions.TelegramNetworkError"
    assert lines[1]["msg"] == "Future exception was never retrieved" and lines[1]["exc"].endswith("ValueError")
    assert lines[2]["msg"] == ("probe https://api.telegram.org/bot<bot-token>/getUpdates Authorization: Bearer <redacted> "
                               "postgresql://<redacted>@192.168.0.1:5432/nestor api_key=<redacted>&x=1 "
                               'password=<redacted> \\" end" PASSWORD=<redacted>')
    for canary in ("canary", "КАНАРЕЙКА", "AAH"):
        assert canary.lower() not in raw.lower(), canary
