"""The Russian time parser behind «нестор, напомни …». Pure functions, no database."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from nestor.bot import prompts, reminders

TZ = ZoneInfo("Europe/Tallinn")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=TZ)  # Wednesday, noon


def when(text: str):
    parsed = reminders.parse(text, NOW.astimezone(timezone.utc), TZ)
    return parsed and (parsed.due.astimezone(TZ).strftime("%Y-%m-%d %H:%M"), parsed.text)


@pytest.mark.parametrize("text, expected", [
    ("напомни в пятницу про сбор денег", ("2026-10-09 10:00", "про сбор денег")),
    ("напомни мне через 2 часа позвонить маме", ("2026-10-07 14:00", "позвонить маме")),
    ("напомни через полчаса", ("2026-10-07 12:30", "")),
    ("напомни через полтора часа выключить духовку", ("2026-10-07 13:30", "выключить духовку")),
    ("напомни через неделю", ("2026-10-14 12:00", "")),
    ("напомни 10.10 в 18:30 оплатить квартиру", ("2026-10-10 18:30", "оплатить квартиру")),
    ("напомни завтра в 9 утра что надо купить хлеб", ("2026-10-08 09:00", "надо купить хлеб")),
    ("напомни в 3 часа дня про созвон", ("2026-10-07 15:00", "про созвон")),
    ("напомни в 7 вечера про игру", ("2026-10-07 19:00", "про игру")),
    ("напомни в 10 позвонить в банк", ("2026-10-08 10:00", "позвонить в банк")),  # 10:00 has passed today
    ("напомни в среду", ("2026-10-14 10:00", "")),                              # today's 10:00 has passed
    ("напомни в среду в 18:00 про футбол", ("2026-10-07 18:00", "про футбол")),
    ("напомни 15 числа оплатить интернет", ("2026-10-15 10:00", "оплатить интернет")),
    ("напомни 5 числа", ("2026-11-05 10:00", "")),
    ("напомни 25 декабря про подарки", ("2026-12-25 10:00", "про подарки")),
    ("напомни 01.02 про налоги", ("2027-02-01 10:00", "про налоги")),
    ("напомни вечером позвонить", ("2026-10-07 19:00", "позвонить")),
    ("напомни послезавтра в полдень о встрече", ("2026-10-09 12:00", "о встрече")),
    ("напомни в 10 раз больше", None),
])
def test_parse(text, expected):
    assert when(text) == expected


def test_daylight_saving_switch_keeps_local_time():
    parsed = reminders.parse("напомни 26.10 в 10:00", NOW.astimezone(timezone.utc), TZ)
    assert parsed.due.astimezone(TZ).hour == 10 and parsed.due.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize("text, error", [
    ("напомни сегодня в 11:00", prompts.REMINDER_PAST),
    ("напомни через 2 года", prompts.REMINDER_TOO_FAR),
    ("напомни 31.02 про отчёт", prompts.REMINDER_BAD_DATE),
])
def test_unusable_times(text, error):
    with pytest.raises(reminders.ReminderError) as e:
        reminders.parse(text, NOW.astimezone(timezone.utc), TZ)
    assert str(e.value) == error


@pytest.mark.parametrize("text, expected", [
    ("напомни в пятницу про сбор денег", True),
    ("напомни завтра, что мы встречаемся", True),
    ("поставь напоминание на завтра в 10", True),
    ("напомни, о чём говорили сегодня", False),
    ("напомни, кто это сказал вчера?", False),
    ("напомни что мы решили вчера", False),
    ("напомни про сбор денег", False),       # no time: the bot asks when instead of guessing
    ("какая погода завтра", False),
])
def test_is_request(text, expected):
    assert reminders.is_request(text) is expected


def test_format_due_and_about():
    now = NOW.astimezone(timezone.utc)
    assert reminders.format_due(NOW.replace(hour=15), TZ, now) == "сегодня в 15:00"
    assert reminders.format_due(NOW + timedelta(days=1), TZ, now) == "завтра в 12:00"
    assert reminders.format_due(NOW + timedelta(days=6), TZ, now) == "во вторник, 13 октября, в 12:00"
    assert reminders.format_due(datetime(2027, 1, 1, 9, 0, tzinfo=TZ), TZ, now) == "в пятницу, 1 января 2027, в 09:00"
    assert reminders.about("про сбор денег") == " про сбор денег"
    assert reminders.about("позвонить маме") == ": позвонить маме"
    assert reminders.about("") == ""
