"""Settings from the environment, as systemd's EnvironmentFile hands them over."""
import asyncio
import json
import logging
from dataclasses import fields
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramNetworkError
from dotenv import dotenv_values

from nestor import config
from nestor.bot import prompts
from nestor.watchdog import __main__ as watchdog_main
from nestor.watchdog import alerts, run

from .test_watchdog_checks import AlertBot

DEAD_DB = "postgresql://nestor:x@127.0.0.1:1/nestor"

NAMES = ("ALLOWED_CHAT_IDS", "MODEL_ROUTES", "MUSIC_DAILY_LIMIT", "MUSIC_PLAYER_APP", "NEWS_CHAT_IDS", "NEWS_TIME",
         "NEWS_MAX_TOPICS", "NEWS_CONTACT_EMAIL", "NEWS_TELEGRAM_CHANNELS", "WISDOM_TIME", "DIGEST_TIME", "TIMEZONE",
         "RETENTION_TIME", "ALERT_CHAT_ID", "ALERT_DAILY_AT", "ALERT_QUIET", "ALERT_DISK_MIN_GB", "ALERT_MAX_MEMBERS",
         "BACKUP_STATUS_FILE")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "42:TEST#not-a-comment")
    monkeypatch.setenv("DATABASE_URL", "postgresql://nestor:p#w@db/nestor")
    return monkeypatch


def test_trailing_comments_are_not_part_of_the_value(env):
    # systemd keeps everything after the «=», comments included; python-dotenv would strip them.
    env.setenv("NEWS_TIME", "20:00                 # время сводки по Таллину")
    env.setenv("NEWS_MAX_TOPICS", "8   # тем в сводке")
    env.setenv("NEWS_TELEGRAM_CHANNELS", "psychedelicsocietyvienna   # публичные каналы через запятую")
    env.setenv("MODEL_ROUTES", "# перенос задач между профилями, см. ниже")
    env.setenv("NEWS_CHAT_IDS", "-1004446259317 # Nestor test")
    s = config.load_settings()
    assert (s.news_time, s.news_max_topics, s.news_telegram_channels) == (time(20, 0), 8,
                                                                         ("psychedelicsocietyvienna",))
    assert s.model_routes == {} and s.news_chat_ids == frozenset({-1004446259317})
    # Secrets are taken as they are: a «#» inside them is not a comment.
    assert s.bot_token == "42:TEST#not-a-comment" and s.database_url == "postgresql://nestor:p#w@db/nestor"


def test_defaults_and_bad_values(env):
    s = config.load_settings()
    assert (s.news_time, s.news_max_topics, s.music_player_app, s.timezone) == (time(20, 0), 10, "",
                                                                               "Europe/Moscow")
    env.setenv("NEWS_MAX_TOPICS", "десять")
    with pytest.raises(SystemExit, match="NEWS_MAX_TOPICS must be a whole number"):
        config.load_settings()
    env.setenv("NEWS_MAX_TOPICS", "10")
    env.setenv("MODEL_ROUTES", "theses:web")
    with pytest.raises(SystemExit, match="MODEL_ROUTES"):
        config.load_settings()


def test_retention_time_and_timezone_validation(env):
    assert config.load_settings().retention_time == time(3, 30)
    env.setenv("RETENTION_TIME", "04:15   # ночная чистка архива")
    env.setenv("TIMEZONE", "Europe/Tallinn")
    s = config.load_settings()
    assert (s.retention_time, s.timezone) == (time(4, 15), "Europe/Tallinn")
    env.setenv("RETENTION_TIME", "в полчетвёртого")
    with pytest.raises(SystemExit, match="RETENTION_TIME must look like 21:00"):
        config.load_settings()
    env.setenv("RETENTION_TIME", "03:30")
    # A wrong zone would move every nightly job and every local time; the service does not start.
    for bad in ("Moscow", "Europe/Atlantis", "../etc/passwd", "МСК"):
        env.setenv("TIMEZONE", bad)
        with pytest.raises(SystemExit, match="TIMEZONE must be an IANA name like Europe/Moscow"):
            config.load_settings()


def test_alert_settings_parse_off_and_garbage(env):
    s = config.load_settings()
    assert (s.alert_chat_id, s.alert_daily_at, s.alert_quiet, s.alert_disk_min_gb, s.alert_max_members,
            s.backup_status_file) == (None, time(9, 30), (time(23), time(8)), 5, 2, "")
    env.setenv("ALERT_CHAT_ID", "-1004446259317   # Nestor alerts")
    env.setenv("ALERT_DAILY_AT", "08:45 # сводка")
    env.setenv("ALERT_QUIET", "22:30 - 07:15")
    env.setenv("ALERT_DISK_MIN_GB", "8")
    env.setenv("ALERT_MAX_MEMBERS", "3")
    env.setenv("BACKUP_STATUS_FILE", "/var/lib/nestor-backup/status.json   # nestor.backup")
    s = config.load_settings()
    assert (s.alert_chat_id, s.alert_daily_at, s.alert_quiet, s.alert_disk_min_gb, s.alert_max_members,
            s.backup_status_file) == (-1004446259317, time(8, 45), (time(22, 30), time(7, 15)), 8, 3,
                                      "/var/lib/nestor-backup/status.json")
    for off in ("off", "OFF", "выкл", "0", "нет"):
        env.setenv("ALERT_DAILY_AT", off)
        env.setenv("ALERT_QUIET", off)
        s = config.load_settings()
        assert (s.alert_daily_at, s.alert_quiet) == (None, None)
    env.setenv("ALERT_DAILY_AT", "09:30")
    env.setenv("ALERT_QUIET", "23:00-08:00")
    for name, bad, message in (("ALERT_CHAT_ID", "@nestor_alerts", "ALERT_CHAT_ID must be a chat id"),
                               ("ALERT_DAILY_AT", "утром", "ALERT_DAILY_AT must look like 21:00"),
                               ("ALERT_QUIET", "23:00", "ALERT_QUIET must look like 23:00-08:00 or off"),
                               ("ALERT_QUIET", "23:00-08:00-09:00", "ALERT_QUIET must look like"),
                               ("ALERT_QUIET", "23:00-23:00", "ALERT_QUIET must look like"),
                               ("ALERT_QUIET", "ночью", "ALERT_QUIET must look like"),
                               ("ALERT_DISK_MIN_GB", "пять", "ALERT_DISK_MIN_GB must be a whole number"),
                               ("ALERT_MAX_MEMBERS", "двое", "ALERT_MAX_MEMBERS must be a whole number")):
        good = config._env(name)
        env.setenv(name, bad)
        with pytest.raises(SystemExit, match=message):
            config.load_settings()
        env.setenv(name, good)
    config.load_settings()


def test_alert_chat_must_be_allowed(env, tmp_path):
    env.setenv("ALLOWED_CHAT_IDS", "-1001111111111")
    env.setenv("ALERT_CHAT_ID", "-1002222222222")
    s = config.load_settings()
    assert config.check_alert_chat(s) == "ALERT_CHAT_ID must be one of ALLOWED_CHAT_IDS: the bot leaves every other group"
    # The watchdog does not start with it: it would send into a group the bot has left.
    env.setattr(watchdog_main, "setup_logging", lambda level: None)        # keeps pytest's log capture
    env.setenv("STATE_DIRECTORY", str(tmp_path))
    env.setenv("DATABASE_URL", DEAD_DB)
    with pytest.raises(SystemExit, match="ALERT_CHAT_ID must be one of ALLOWED_CHAT_IDS"):
        watchdog_main.main([])
    env.setenv("ALLOWED_CHAT_IDS", "-1001111111111, -1002222222222")
    assert config.check_alert_chat(config.load_settings()) is None
    env.delenv("ALERT_CHAT_ID")
    assert config.check_alert_chat(config.load_settings()) is None          # no alerts: nothing to check
    with pytest.raises(SystemExit, match="ALERT_CHAT_ID is empty"):
        watchdog_main.main(["--test"])


def test_env_example_has_the_stage_4_settings_with_their_defaults(env):
    example = dotenv_values(Path(__file__).parent.parent / ".env.example")
    assert set(NAMES) <= set(example)
    for name in NAMES:
        env.setenv(name, example[name])
    s = config.load_settings()
    stage4 = ("retention_time", "alert_chat_id", "alert_daily_at", "alert_quiet", "alert_disk_min_gb",
              "alert_max_members")
    defaults = {f.name: f.default for f in fields(config.Settings)}
    assert {name: getattr(s, name) for name in stage4} == {name: defaults[name] for name in stage4}
    assert config.check_alert_chat(s) is None
    # The backups are watched unless the line is emptied: the unit fixes where their status is on the Pi.
    assert s.backup_status_file == "/var/lib/nestor-backup/status.json"


def _main(argv: list[str]):
    """watchdog_main.main in a thread (it runs its own event loop); a SystemExit is returned, not raised."""
    try:
        return watchdog_main.main(argv)
    except SystemExit as e:
        return e


async def test_the_watchdog_keeps_the_optouts_with_alerts_off_or_refused(env, database, conn, tmp_path, caplog):
    env.setattr(watchdog_main, "setup_logging", lambda level: None)
    env.setenv("DATABASE_URL", database["collector"])
    env.setenv("STATE_DIRECTORY", str(tmp_path))
    for uid, out in ((111, True), (222, False)):
        await conn.execute("INSERT INTO users (user_id, opted_out) VALUES ($1, $2)", uid, out)
    path = tmp_path / run.OPTOUTS_FILE
    path.write_text("[555]\n")
    # ALERT_CHAT_ID empty, as .env.example has it: nobody to tell, the opt-outs a restore needs are kept.
    with caplog.at_level(logging.WARNING, logger="nestor.watchdog"):
        assert await asyncio.to_thread(_main, []) == 0
    assert "alerts are off: ALERT_CHAT_ID is empty" in caplog.text
    assert json.loads(path.read_text()) == [111, 555] and path.stat().st_mode & 0o777 == 0o600
    # An alert group the bot would leave: the unit fails, but the opt-outs are kept first.
    await conn.execute("UPDATE users SET opted_out = TRUE WHERE user_id = 222")
    env.setenv("ALLOWED_CHAT_IDS", "-1001111111111")
    env.setenv("ALERT_CHAT_ID", "-1002222222222")
    refused = await asyncio.to_thread(_main, [])
    assert isinstance(refused, SystemExit) and "ALERT_CHAT_ID must be one of ALLOWED_CHAT_IDS" in str(refused)
    assert json.loads(path.read_text()) == [111, 222, 555]
    # --dry-run refuses the same, and saves nothing.
    await conn.execute("INSERT INTO users (user_id, opted_out) VALUES (333, TRUE)")
    assert isinstance(await asyncio.to_thread(_main, ["--dry-run"]), SystemExit)
    assert json.loads(path.read_text()) == [111, 222, 555]


def test_the_watchdog_test_and_dry_run_say_what_they_did(env, tmp_path, capsys):
    env.setattr(watchdog_main, "setup_logging", lambda level: None)
    env.setenv("STATE_DIRECTORY", str(tmp_path))
    env.setenv("ALLOWED_CHAT_IDS", "-1001111111111")
    env.setenv("ALERT_CHAT_ID", "-1001111111111")
    bot = AlertBot()
    env.setattr(watchdog_main, "Bot", lambda token: bot)
    assert watchdog_main.main(["--test"]) == 0 and capsys.readouterr().out == "test sent\n"
    assert [(m["chat_id"], m["text"], m["disable_notification"]) for m in bot.sent] == [
        (-1001111111111, prompts.ALERT_TEST, False)]
    bot.errors = [TelegramNetworkError(method=None, message="down")]
    assert watchdog_main.main(["--test"]) == 1 and capsys.readouterr().out == "test failed: see the journal\n"
    # --dry-run: what the last look found and the message a run would send, as plain text; nothing is sent.
    snap = {"checked_at": "2026-10-05T11:00:00+00:00", "checks": {},
            "problems": [{"key": "disk.low:/", "level": "warn", "line": "Мало <b>места</b>"}]}
    message = alerts.Outgoing(f"{prompts.ALERT_NEW}\n🟠 Мало <b>места</b>", silent=False, items=[])

    async def dry(settings, **kw):
        assert kw["dry_run"] is True and kw["bot"] is bot
        return run.Report([], snap, message)

    env.setattr(watchdog_main, "run_once", dry)
    assert watchdog_main.main(["--dry-run"]) == 0
    status, sent = capsys.readouterr().out.split("\n---\n")
    assert status.startswith("🩺 Состояние Нестора") and "🟠 Мало места" in status and "<b>" not in status
    assert sent == "🚨 Нестор: проблема\n🟠 Мало места\n" and len(bot.sent) == 1


class _LateEvening(datetime):
    """23:30 in Moscow: an hour's mute ends tomorrow."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 5, 20, 30, tzinfo=UTC).astimezone(tz)


def test_the_watchdog_mute_from_the_command_line(env, tmp_path, capsys):
    env.setattr(watchdog_main, "setup_logging", lambda level: None)
    for module in (watchdog_main, run):
        env.setattr(module, "datetime", _LateEvening)
    env.setenv("STATE_DIRECTORY", str(tmp_path))
    env.setenv("RUNTIME_DIRECTORY", str(tmp_path / "run"))
    env.setenv("ALLOWED_CHAT_IDS", "-1001111111111")
    env.setenv("ALERT_CHAT_ID", "-1001111111111")

    async def no_run(settings, **kw):
        raise AssertionError("--mute ran the watchdog")

    env.setattr(watchdog_main, "run_once", no_run)
    path, in_memory = tmp_path / run.STATE_FILE, tmp_path / "run" / run.STATE_FILE
    in_memory.parent.mkdir()
    assert watchdog_main.main(["--mute", "60"]) == 0
    assert capsys.readouterr().out == "muted until 6 октября 00:30\n"
    assert alerts.load_state(path).mute_until == datetime(2026, 10, 5, 21, 30, tzinfo=UTC)
    assert alerts.load_state(in_memory).mute_until == alerts.load_state(path).mute_until
    # 0 ends it: it unmutes, and runs nothing.
    assert watchdog_main.main(["--mute", "0"]) == 0 and capsys.readouterr().out == "unmuted\n"
    assert alerts.load_state(path).mute_until is None and alerts.load_state(in_memory).mute_until is None
    # A card that cannot be written: one line with the directory and the error's class, no traceback.
    (tmp_path / (run.STATE_FILE + ".tmp")).mkdir()
    with pytest.raises(SystemExit) as refused:
        watchdog_main.main(["--mute", "60"])
    assert str(refused.value) == f"cannot write the watchdog state in {tmp_path}: IsADirectoryError"
    assert alerts.load_state(path).mute_until is None and alerts.load_state(in_memory).mute_until is None


def test_a_watchdog_run_while_another_holds_the_state_is_not_a_failure(env, tmp_path, caplog):
    env.setattr(watchdog_main, "setup_logging", lambda level: None)
    env.setenv("STATE_DIRECTORY", str(tmp_path))
    env.setenv("ALLOWED_CHAT_IDS", "-1001111111111")
    env.setenv("ALERT_CHAT_ID", "-1001111111111")
    env.setattr(watchdog_main, "Bot", lambda token: AlertBot())

    async def busy(settings, **kw):
        raise run.Busy

    env.setattr(watchdog_main, "run_once", busy)
    with caplog.at_level(logging.INFO, logger="nestor.watchdog"):
        assert watchdog_main.main([]) == 0
    assert "watchdog already running" in caplog.text


def test_the_watchdog_unit_keeps_its_state_where_the_code_looks():
    unit = (Path(__file__).parent.parent / "deploy" / "systemd" / "nestor-watchdog.service").read_text()
    assert f"StateDirectory={run.STATE_DIR.name}\n" in unit and f"RuntimeDirectory={run.RUNTIME_DIR.name}\n" in unit
    assert "RuntimeDirectoryPreserve=yes\n" in unit         # the copy in memory outlives each run of the timer
