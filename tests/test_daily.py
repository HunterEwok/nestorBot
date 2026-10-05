"""Themes and the wisdom of the day: what may leave for the web model, and what may be posted.
The digest's period: its bounds and its name."""
import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nestor.bot import daily, prompts


def test_parse_themes_keeps_only_the_fixed_list():
    answer = 'Темы: {"themes": ["Деньги", "учеба", "секретный план Пети", "деньги", "работа", "космос"]}'
    assert daily.parse_themes(answer) == ["деньги", "учёба", "работа"]
    assert daily.parse_themes('{"themes": "деньги"}') == []
    assert daily.parse_themes("нет json") == []


@pytest.mark.parametrize("url, ok", [
    ("https://ru.wikiquote.org/wiki/Лев_Толстой", True),
    ("http://example.com/a", True),
    ("http://192.168.0.1/admin", False),
    ("https://127.0.0.1:8642/v1/models", False),
    ("http://localhost/", False),
    ("http://router.lan/", False),
    ("https://user:pass@example.com/", False),
    ("javascript:alert(1)", False),
    ("https://intranet/", False),
    (None, False),
])
def test_is_public_url(url, ok):
    assert daily.is_public_url(url) is ok


def test_parse_wisdom():
    answer = json.dumps({"quote": "«Счастлив тот, кто счастлив у себя дома.»", "author": "— Лев Толстой",
                         "source_url": "https://ru.wikiquote.org/wiki/Лев_Толстой", "theme": "дом"},
                        ensure_ascii=False)
    w = daily.parse_wisdom(answer, ["дом", "семья"])
    assert w == daily.Wisdom("Счастлив тот, кто счастлив у себя дома.", "Лев Толстой",
                             "https://ru.wikiquote.org/wiki/Лев_Толстой", "дом")
    assert daily.parse_wisdom(answer, ["семья"]).theme is None
    bad = json.loads(answer) | {"source_url": "http://192.168.0.1/"}
    assert daily.parse_wisdom(json.dumps(bad), ["дом"]) is None
    assert daily.parse_wisdom(json.dumps({"quote": "Да.", "author": "Я", "source_url": "https://a.b/"}), []) is None

    text = daily.format_wisdom(w)
    assert text.startswith(f"<b>{prompts.WISDOM_HEADER}</b>")
    assert "<blockquote>«Счастлив тот, кто счастлив у себя дома.»\n— <i>Лев Толстой</i></blockquote>" in text
    assert "К сегодняшним разговорам: дом." in text
    assert text.endswith('<a href="https://ru.wikiquote.org/wiki/Лев_Толстой">Источник</a> (ru.wikiquote.org)')
    odd = daily.format_wisdom(daily.Wisdom("a < b & c", "Автор <i>", "https://a.example/?x=1&y=\"2\"", None))
    assert "«a &lt; b &amp; c»" in odd and "Автор &lt;i&gt;" in odd and 'href="https://a.example/?x=1&amp;y=&quot;2&quot;"' in odd
    assert daily.is_repeat(w, [{"quote": "Счастлив тот, кто счастлив у себя дома", "author": "Толстой"}])
    assert not daily.is_repeat(w, [{"quote": "Всё смешалось в доме Облонских.", "author": "Толстой"}])


def test_wisdom_request_carries_only_themes_and_recent_quotes():
    text = daily.wisdom_request(["деньги", "работа"], [{"quote": "Время — деньги.", "author": "Франклин"}],
                                date(2026, 10, 3))
    assert text.splitlines() == ["Дата: 3 октября 2026.", "Темы сегодняшних разговоров: деньги, работа.",
                                 "Недавние цитаты, их не повторять:", "- Франклин: «Время — деньги.»"]
    assert "выбери тему сам" in daily.wisdom_request([], [], date(2026, 10, 3))


def test_the_digest_names_its_period():
    day = date(2026, 10, 5)
    assert daily.digest_period(day, 1) == "4 октября"
    assert daily.digest_header(day, 1) == "☀️ **Дайджест за вчера, 4 октября**"     # as it always was
    assert daily.digest_period(day, 30) == "30 дней, 5 сентября — 4 октября"
    assert daily.digest_header(day, 30) == "☀️ **Дайджест за 30 дней, 5 сентября — 4 октября**"
    assert daily.digest_period(day, 21) == "21 день, 14 сентября — 4 октября"
    assert daily.digest_period(day, 2) == "2 дня, 3 октября — 4 октября"
    assert daily.digest_period(date(2027, 1, 3), 7) == "7 дней, 27 декабря — 2 января"
    assert daily.digest_period(date(2027, 3, 1), 90) == "90 дней, 1 декабря — 28 февраля"


def test_the_digest_period_runs_from_local_midnight_to_local_midnight():
    moscow, berlin = ZoneInfo("Europe/Moscow"), ZoneInfo("Europe/Berlin")

    def utc(bounds):
        return tuple(t.astimezone(UTC) for t in bounds)

    assert utc(daily.digest_bounds(date(2026, 10, 5), 30, moscow)) == (
        datetime(2026, 9, 4, 21, tzinfo=UTC), datetime(2026, 10, 4, 21, tzinfo=UTC))
    # A zone with clock changes: the day the clocks go back has 25 hours, the one they go forward 23.
    since, until = daily.digest_bounds(date(2026, 10, 26), 1, berlin)
    assert (since.utcoffset(), until.utcoffset()) == (timedelta(hours=2), timedelta(hours=1))
    assert utc((since, until)) == (datetime(2026, 10, 24, 22, tzinfo=UTC),
                                   datetime(2026, 10, 25, 23, tzinfo=UTC))
    since, until = utc(daily.digest_bounds(date(2026, 3, 30), 1, berlin))
    assert until - since == timedelta(hours=23)
    since, until = utc(daily.digest_bounds(date(2026, 11, 10), 30, berlin))
    assert (since.astimezone(berlin).hour, until.astimezone(berlin).hour) == (0, 0)
    assert until - since == timedelta(days=30, hours=1)
