"""Themes and the wisdom of the day: what may leave for the web model, and what may be posted."""
import json
from datetime import date

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
    assert text.startswith(prompts.WISDOM_HEADER) and "«Счастлив тот, кто счастлив у себя дома.»\n— Лев Толстой" in text
    assert "К сегодняшним разговорам: дом." in text and text.endswith("Источник: https://ru.wikiquote.org/wiki/Лев_Толстой")
    assert daily.is_repeat(w, [{"quote": "Счастлив тот, кто счастлив у себя дома", "author": "Толстой"}])
    assert not daily.is_repeat(w, [{"quote": "Всё смешалось в доме Облонских.", "author": "Толстой"}])


def test_wisdom_request_carries_only_themes_and_recent_quotes():
    text = daily.wisdom_request(["деньги", "работа"], [{"quote": "Время — деньги.", "author": "Франклин"}],
                                date(2026, 10, 3))
    assert text.splitlines() == ["Дата: 3 октября 2026.", "Темы сегодняшних разговоров: деньги, работа.",
                                 "Недавние цитаты, их не повторять:", "- Франклин: «Время — деньги.»"]
    assert "выбери тему сам" in daily.wisdom_request([], [], date(2026, 10, 3))
