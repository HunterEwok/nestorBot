"""Stage 4 watchdog, the alert engine: when a problem is said, said again, resolved; events; one message
per run, the hourly cap, the outbox; quiet hours and the mute; the state on the card."""
import copy
import json
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nestor.bot import prompts
from nestor.watchdog import alerts
from nestor.watchdog import run as run_mod
from nestor.watchdog.checks import Event, Finding, result, unknown

MSK = ZoneInfo("Europe/Moscow")
QUIET = (time(23), time(8))
T0 = datetime(2026, 10, 5, 14, 0, tzinfo=MSK)          # a Monday afternoon, outside the quiet hours


def at(minutes: float, start: datetime = T0) -> datetime:
    return start + timedelta(minutes=minutes)


def run(state, *results, when, quiet=QUIET):
    """One run; what it sends is taken as delivered."""
    state, out = alerts.evaluate(state, list(results), when, tz=MSK, quiet=quiet)
    if out is not None:
        alerts.delivered(state, out, when)
    return out


def db(down: bool, *, level="crit", confirm=2, clear=2, line="База недоступна"):
    findings = [Finding("db.unreachable", level, line, confirm=confirm, clear=clear)] if down else []
    return result("db", findings)


def test_announced_once_after_confirmation():
    state = alerts.AlertState()
    assert run(state, db(True), when=at(0)) is None                     # one failing run is not enough
    out = run(state, db(True), when=at(5))
    assert out.text == f"{prompts.ALERT_NEW}\n🔴 База недоступна" and not out.silent
    assert [run(state, db(True), when=at(m)) for m in (10, 15, 20)] == [None, None, None]
    assert state.problems["db.unreachable"].since == at(0)


def test_blip_sends_nothing():
    state = alerts.AlertState()
    assert run(state, db(True), when=at(0)) is None
    assert run(state, db(False), when=at(5)) is None
    assert run(state, db(False), when=at(10)) is None
    assert state.problems == {} and state.outbox == []


def test_resolved_silent_with_duration():
    state = alerts.AlertState()
    assert run(state, db(True, confirm=1), when=at(0)) is not None
    assert run(state, db(False), when=at(5)) is None                    # clear=2: one good run is not enough
    out = run(state, db(False), when=at(40))
    assert out.text == f"{prompts.ALERT_RESOLVED}\n• База: снова в порядке, длилось 40 мин" and out.silent
    assert state.problems == {}
    # Longer outages read in hours and days.
    assert (alerts.fmt_duration(timedelta(minutes=125)), alerts.fmt_duration(timedelta(hours=60)),
            alerts.fmt_duration(timedelta(seconds=20))) == ("2 ч 5 мин", "2 дн.", "меньше минуты")


def test_unknown_neither_raises_nor_clears():
    state = alerts.AlertState()
    run(state, db(True), when=at(0))
    assert run(state, unknown("db"), when=at(5)) is None                # not a second failing run
    assert state.problems["db.unreachable"].fails == 1
    assert run(state, db(True), when=at(10)) is not None
    run(state, db(False), when=at(15))
    assert run(state, unknown("db"), when=at(20)) is None               # not a second good run either
    assert "db.unreachable" in state.problems
    assert "снова в порядке" in run(state, db(False), when=at(25)).text
    # A check that sees only part of what it owns: the rest stays as it was.
    state = alerts.AlertState()
    gateway = Finding("hermes.gateway", "crit", "Hermes не отвечает", clear=2)
    profile = Finding("hermes.profile:web", "crit", "Профиль Hermes web: ключ не подходит")
    run(state, result("hermes", [profile]), when=at(0))
    out = run(state, result("hermes", [gateway], unknown=("hermes.profile:",)), when=at(5))
    assert "Hermes не отвечает" in out.text and "снова в порядке" not in out.text
    assert run(state, result("hermes", [gateway], unknown=("hermes.profile:",)), when=at(10)) is None
    assert "hermes.profile:web" in state.problems


def test_escalation_resends():
    state = alerts.AlertState()

    def disk(level):
        return result("disk", [Finding("disk.low:/", level, f"Мало места ({level})")])

    assert "Мало места (warn)" in run(state, disk("warn"), when=at(0)).text
    assert run(state, disk("warn"), when=at(15)) is None
    assert run(state, disk("crit"), when=at(30)).text.endswith("🔴 Мало места (crit)")
    assert run(state, disk("warn"), when=at(45)) is None                # getting better is not news
    assert run(state, disk("crit"), when=at(60)) is None                # announced at crit already


def test_repeat_token_reannounces_backup_each_night():
    state = alerts.AlertState()

    def failed(nights):
        line = prompts.ALERT_BACKUP_FAILED.format(nights=nights, reason="база на роутере недоступна", last_ok="4 октября 04:31")
        return result("backup", [Finding("backup.failed", "crit", line, repeat_token=str(nights))])

    night = datetime(2026, 10, 6, 9, 0, tzinfo=MSK)
    assert "(1-я ночь)" in run(state, failed(1), when=night).text
    assert [run(state, failed(1), when=at(m, night)) for m in (15, 30, 600)] == [None, None, None]
    assert "(2-я ночь)" in run(state, failed(2), when=night + timedelta(days=1)).text
    assert run(state, failed(2), when=night + timedelta(days=1, minutes=15)) is None


def test_events_aggregate_within_cooldown():
    state = alerts.AlertState()

    def restarted(count):
        line = prompts.ALERT_COLLECTOR_RESTARTS.format(count="{count}", reason="не хватило памяти (oom-kill)")
        return result("collector", events=[Event("collector.restarts", line, count=count)])

    out = run(state, restarted(1), when=at(0))
    assert out.text == f"{prompts.ALERT_EVENTS}\n• Сборщик сам перезапускался 1 раз, причина: не хватило памяти (oom-kill)"
    assert not out.silent
    assert run(state, restarted(2), when=at(5)) is None                 # within the hour: counted
    assert run(state, restarted(1), when=at(30)) is None
    assert run(state, result("collector"), when=at(55)) is None
    assert "перезапускался 3 раз" in run(state, result("collector"), when=at(60)).text
    assert run(state, result("collector"), when=at(65)) is None
    assert run(state, result("collector"), when=at(125)) is None and state.events == {}


def test_one_message_per_run_and_hourly_cap():
    state = alerts.AlertState()
    many = result("backup", [Finding(f"backup.warning:w{i:02}", "warn", f"строка {i}") for i in range(30)])
    out = run(state, many, when=at(0))
    lines = out.text.split("\n")
    assert lines[0] == prompts.ALERT_NEW and len(lines) == 1 + alerts.MAX_LINES
    assert lines[-1] == prompts.ALERT_MORE.format(count=6) and lines[1] == "🟠 строка 0"
    out = run(state, many, when=at(5))                                  # the rest goes with the next run
    assert out.text.split("\n")[1:] == [f"🟠 строка {i}" for i in range(24, 30)]
    # A new problem on every run: six messages an hour, then they wait.
    state = alerts.AlertState()
    sent = [run(state, result(f"check{m}", [Finding(f"disk.x{m}", "warn", f"проблема {m}")]), when=at(m)) is not None
            for m in range(0, 60, 5)]
    assert sent == [True] * 6 + [False] * 6 and len(state.outbox) == 6
    out = run(state, when=at(60))                                       # the first one is an hour old
    assert out.text.split("\n")[1:] == [f"🟠 проблема {m}" for m in range(30, 60, 5)] and state.outbox == []
    assert run(state, when=at(61)) is None
    # Long lines: the message stays within Telegram's 4096 characters, the rest waits its turn.
    state = alerts.AlertState()
    long = result("backup", [Finding(f"backup.warning:w{i:02}", "warn", f"{i:02} " + "длинная строка " * 20)
                             for i in range(20)])
    texts = [run(state, long, when=at(m)).text for m in (0, 5)]
    assert all(len(t) < 4096 for t in texts) and texts[0].endswith(prompts.ALERT_MORE.format(count=8))
    assert [line[2:4] for line in texts[1].split("\n")[1:]] == [f"{i:02}" for i in range(12, 20)]


def test_outbox_and_compaction():
    state = alerts.AlertState(sent_times=[at(-10)] * alerts.MAX_PER_HOUR)   # the hour's messages are used
    finding = Finding("collector.down", "crit", "Сборщик не работает", confirm=1)
    assert run(state, result("collector", [finding]), when=at(0)) is None
    assert run(state, result("collector", [finding]), when=at(20)) is None
    assert run(state, result("collector"), when=at(40)) is None
    assert [i["kind"] for i in state.outbox] == ["was"]
    out = run(state, result("collector"), when=at(55))
    assert out.text == f"{prompts.ALERT_RESOLVED}\n• Было: Сборщик не работает (14:00–14:40)" and out.silent
    # Delivered once, then gone while it waited again: the usual «снова в порядке».
    state = alerts.AlertState()
    assert run(state, result("collector", [finding]), when=at(0)) is not None
    state.sent_times = [at(1)] * alerts.MAX_PER_HOUR
    assert run(state, result("collector"), when=at(10)) is None
    assert "Сборщик: снова в порядке, длилось 10 мин" in run(state, result("collector"), when=at(65)).text


def test_quiet_hours_silent_except_old_crit():
    night = datetime(2026, 10, 5, 23, 30, tzinfo=MSK)
    state = alerts.AlertState()
    crit = Finding("collector.down", "crit", "Сборщик не работает")
    warn = Finding("disk.low:/", "warn", "Мало места")
    out = run(state, result("collector", [crit]), result("disk", [warn]), when=night)
    assert out.silent and "Сборщик" in out.text and "Мало места" in out.text
    assert run(state, result("collector", [crit]), result("disk", [warn]), when=at(30, night)) is None
    out = run(state, result("collector", [crit]), result("disk", [warn]), when=at(65, night))
    assert not out.silent and out.text == f"{prompts.ALERT_NEW}\n🔴 Сборщик не работает"   # once, and only the crit
    for m in (70, 300, 600):
        assert run(state, result("collector", [crit]), result("disk", [warn]), when=at(m, night)) is None
    # Outside the quiet hours a new warning rings; no quiet hours at all: everything rings.
    morning = datetime(2026, 10, 6, 9, 0, tzinfo=MSK)
    assert not run(alerts.AlertState(), result("disk", [warn]), when=morning).silent
    assert not run(alerts.AlertState(), result("disk", [warn]), when=night, quiet=None).silent
    assert alerts.in_quiet(time(7, 59), QUIET) and not alerts.in_quiet(time(8), QUIET)
    assert alerts.in_quiet(time(13), (time(12), time(14))) and not alerts.in_quiet(time(23), (time(12), time(14)))


def test_a_warning_that_turns_critical_at_night_rings_an_hour_later():
    evening = datetime(2026, 10, 5, 22, 0, tzinfo=MSK)
    state = alerts.AlertState()

    def disk(level):
        return result("disk", [Finding("disk.low:/", level, f"Мало места ({level})")])

    assert not run(state, disk("warn"), when=evening).silent           # rang as a warning before the quiet hours
    out = run(state, disk("crit"), when=at(120, evening))               # midnight: worse, said without sound
    assert out.silent and out.text.endswith("🔴 Мало места (crit)")
    minutes = range(125, 600, 5)
    outs = [run(state, disk("crit"), when=at(m, evening)) for m in minutes]
    assert [(m, o.silent) for m, o in zip(minutes, outs, strict=True) if o is not None] == [(180, False)]
    # An adopted crit that rang as a crit stays quiet.
    snap = alerts.snapshot(state, at(600, evening))
    adopted = alerts.adopt(alerts.AlertState(), snap)
    assert adopted.problems["disk.low:/"].rung and adopted.problems["disk.low:/"].crit_since == at(120, evening)
    assert run(adopted, disk("crit"), when=at(605, evening)) is None


def test_muted_events_and_old_crits_make_no_sound(tmp_path):
    night = datetime(2026, 10, 5, 23, 30, tzinfo=MSK)
    state = alerts.AlertState()
    crit = result("collector", [Finding("collector.down", "crit", "Сборщик не работает")])
    assert run(state, crit, when=night).silent
    state.mute_until = at(240, night)                                   # works on the router tonight
    assert run(state, crit, when=at(65, night)) is None and not state.problems["collector.down"].rung
    restarted = Event("collector.restarts", "Сборщик сам перезапускался {count} раз")
    noon = datetime(2026, 10, 6, 12, 0, tzinfo=MSK)
    state = alerts.AlertState(mute_until=at(60, noon))
    out = run(state, result("collector", events=[restarted]), when=noon)
    assert "перезапускался 1 раз" in out.text and out.silent           # by day, but muted
    # --mute 60 from the command line: kept in the state, 0 ends it.
    assert run_mod.mute(tmp_path, 60, noon) == at(60, noon)
    assert alerts.load_state(tmp_path / run_mod.STATE_FILE).mute_until == at(60, noon)
    assert run_mod.mute(tmp_path, 0, noon) is None and alerts.load_state(tmp_path / run_mod.STATE_FILE).mute_until is None


def test_mute_holds_then_sends_open_problems():
    state = alerts.AlertState()
    old = Finding("disk.low:/", "warn", "Мало места")
    run(state, result("disk", [old]), when=at(-30))                     # announced before the mute
    state.mute_until = at(60)
    down = Finding("collector.down", "crit", "Сборщик не работает")
    blip = Finding("reminders.late", "warn", "Напоминания задерживаются")
    assert run(state, result("collector", [down]), result("reminders", [blip]), when=at(0)) is None
    out = run(state, result("collector", [down]), result("reminders"), result("disk"), when=at(10))
    assert out.silent and "Место на карте Pi: снова в порядке" in out.text and "Сборщик" not in out.text
    assert alerts.snapshot(state, at(10))["muted_until"] is not None
    assert [p["key"] for p in alerts.snapshot(state, at(10))["problems"]] == ["collector.down"]   # /status shows it
    assert run(state, result("collector", [down]), when=at(55)) is None
    out = run(state, result("collector", [down]), when=at(65))          # the mute is over
    assert not out.silent and out.text == f"{prompts.ALERT_NEW}\n🔴 Сборщик не работает"
    assert "Напоминания" not in out.text                                # it came and went while muted


def test_corrupt_state_starts_fresh(tmp_path, caplog):
    path = tmp_path / "state.json"
    state = alerts.AlertState(mute_until=at(30), last_daily=date(2026, 10, 5), baselines={"collector_restarts": 3})
    run(state, db(True, confirm=1), when=at(0))
    run(state, result("collector", events=[Event("collector.restarts", "x {count}")]), when=at(5))
    alerts.save_state(path, state, at(5))
    assert path.stat().st_mode & 0o777 == 0o600
    again = alerts.load_state(path)
    assert again == state and again.problems["db.unreachable"].since == at(0)
    for garbage in ("{not json", json.dumps({"version": 99}), json.dumps([1, 2]), json.dumps({"version": 1})):
        path.write_text(garbage)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert alerts.load_state(path) == alerts.AlertState()
        assert "starting fresh" in caplog.text
    assert alerts.load_state(tmp_path / "missing.json") == alerts.AlertState()


def test_a_lost_state_takes_the_one_published_with_the_snapshot():
    # The card went read-only: it keeps the state of an hour ago, the snapshot in the database is newer.
    stale = alerts.AlertState()
    run(stale, result("disk", [Finding("disk.low:/", "warn", "Мало места")]), when=at(-60))
    stale.saved_at = at(-60)
    state = copy.deepcopy(stale)                                        # the runs since, which could not save
    sd = Finding("sd.write", "crit", "Не могу записать на карту Pi")
    restarted = Event("collector.restarts", "Сборщик сам перезапускался {count} раз")
    run(state, result("sd", [sd]), result("disk"), result("collector", events=[restarted]), when=at(0))
    state.last_daily, state.baselines = date(2026, 10, 5), {"collector_restarts": 3}
    snap = json.loads(json.dumps(alerts.snapshot(state, at(0))))        # as the database gives it back
    lost = alerts.adopt(stale, snap)
    assert lost.saved_at == at(0) and lost.last_daily == date(2026, 10, 5) and lost.sent_times == [at(0)]
    assert lost.baselines == {"collector_restarts": 3} and set(lost.events) == {"collector.restarts"}
    # Nothing is said again: not the problem, not that the disk is fine, not the event.
    assert run(lost, result("sd", [sd]), result("disk"), result("collector"), when=at(5)) is None
    assert "Запись на карту Pi: снова в порядке" in run(lost, result("sd"), when=at(10)).text
    # A state saved after that snapshot knows better; a snapshot without a readable one changes nothing.
    newer = alerts.AlertState(saved_at=at(1))
    assert alerts.adopt(newer, snap) is newer and newer.problems == {}
    for unusable in ({**snap, "state": {"version": 99}}, {k: v for k, v in snap.items() if k != "state"}, None):
        assert alerts.adopt(stale, unusable) is stale


def test_daily_summary_once_per_local_day_silent():
    state = alerts.AlertState()
    nine = datetime(2026, 10, 6, 9, 29, tzinfo=MSK)
    assert not alerts.daily_due(state, nine, MSK, time(9, 30))
    assert alerts.daily_due(state, at(1, nine), MSK, time(9, 30))
    assert not alerts.daily_due(state, at(1, nine), MSK, None)          # ALERT_DAILY_AT=off
    state.last_daily = date(2026, 10, 6)
    assert not alerts.daily_due(state, at(600, nine), MSK, time(9, 30))
    # 21:00 UTC is already the next day in Moscow.
    assert alerts.daily_due(state, datetime(2026, 10, 7, 6, 31, tzinfo=ZoneInfo("UTC")), MSK, time(9, 30))
    # The summary goes as its own silent message: tests/test_watchdog_checks.py runs it end to end.


def test_status_lists_open_problems_and_what_could_not_be_checked():
    state = alerts.AlertState()
    run(state, db(True, confirm=1), result("hermes", [Finding("hermes.gateway", "crit", "Hermes не отвечает",
                                                               confirm=2)]),
        result("disk", [Finding("disk.low:/", "warn", "Мало места")]), unknown("retention"), when=at(0))
    text = alerts.render_status(alerts.snapshot(state, at(0)), at(3), MSK)
    assert text == "\n".join([prompts.STATUS_HEADER.format(time="14:00"), "🔴 База недоступна", "🟠 Мало места",
                              prompts.STATUS_UNCHECKED.format(names="Чистка архива")])
    run(state, db(False), result("disk"), result("retention"), when=at(5))
    run(state, db(False), result("hermes"), when=at(10))
    assert alerts.render_status(alerts.snapshot(state, at(10)), at(10), MSK).endswith(prompts.STATUS_ALL_OK)
