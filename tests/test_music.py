"""Songs: parsing the model's answer, link validation and the card. No network."""
import json

import pytest

from nestor.bot import music, prompts

from .test_answerer import FakeChecker

SPOTIFY = "4" * 22


@pytest.mark.parametrize("url, vid", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=x", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ", None),
    ("https://www.youtube.com/watch?v=short", None),
    ("javascript:alert(1)", None),
    (None, None),
])
def test_youtube_id(url, vid):
    assert music.youtube_id(url) == vid


def test_spotify_and_apple_links():
    assert music.spotify_track_id(f"https://open.spotify.com/intl-ru/track/{SPOTIFY}?si=1") == SPOTIFY
    assert music.spotify_track_id(f"https://open.spotify.com/album/{SPOTIFY}") is None
    assert music.spotify_track_id(f"http://open.spotify.com/track/{SPOTIFY}") is None
    assert (music.apple_track_url("https://music.apple.com/ru/album/gruppa-krovi/123?i=456&uo=4")
            == "https://music.apple.com/ru/album/gruppa-krovi/123?i=456")
    assert music.apple_track_url("https://music.apple.com.evil.example/ru/album/x/1") is None


def test_parse_pick_question_and_error():
    answer = "Нашёл:\n```json\n" + json.dumps({
        "artist": "Кино", "title": "Группа крови",
        "youtube": ["https://www.youtube.com/watch?v=AAAAAAAAAAA", "https://evil.example/x",
                    "https://youtu.be/AAAAAAAAAAA", "https://youtu.be/BBBBBBBBBBB"],
        "spotify": f"https://open.spotify.com/track/{SPOTIFY}"}, ensure_ascii=False) + "\n```"
    pick = music.parse(answer)
    assert pick == music.Pick("Кино", "Группа крови", ("AAAAAAAAAAA", "BBBBBBBBBBB"), SPOTIFY)
    assert music.parse('{"question": "Какую версию?"}') == music.Say("Какую версию?")
    assert music.parse('{"error": "такой песни нет"}') == music.Say("Не нашёл: такой песни нет")
    assert music.parse('{"artist": "Кино"}') == music.Say(prompts.MUSIC_NOT_FOUND)
    assert music.parse("просто текст") is None


@pytest.mark.parametrize("question, needs", [
    ("включи эту песню", True),
    ("включи", True),
    ("поставь её, пожалуйста", True),
    ("включи Кино — Группа крови", False),
    ("найди песню про белые розы", False),
])
def test_needs_reply_text(question, needs):
    assert music.needs_reply_text(question) is needs


async def test_card_prefers_verified_matching_video_and_direct_links():
    pick = music.Pick("Кино", "Группа <крови>", ("AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"), SPOTIFY)
    checker = FakeChecker(videos={"AAAAAAAAAAA": "Какой-то кавер", "BBBBBBBBBBB": "КИНО - Группа <крови> (Official)"},
                          spotify={SPOTIFY}, apple="https://music.apple.com/ru/album/x/1?i=2")
    card = await music.build_card(pick, checker)
    assert card.preview_url == "https://www.youtube.com/watch?v=BBBBBBBBBBB"
    assert "<b>Кино — Группа &lt;крови&gt;</b>" in card.html and prompts.MUSIC_PLAY in card.html
    buttons = [b for row in card.keyboard.inline_keyboard for b in row]
    assert [(b.text, b.url) for b in buttons] == [
        ("Apple Music", "https://music.apple.com/ru/album/x/1?i=2"),
        ("YouTube Music", "https://music.youtube.com/watch?v=BBBBBBBBBBB"),
        ("Spotify", f"https://open.spotify.com/track/{SPOTIFY}"),
        ("🔎 Яндекс Музыка", "https://music.yandex.ru/search?text=%D0%9A%D0%B8%D0%BD%D0%BE%20%D0%93%D1%80%D1%83%D0%BF"
                            "%D0%BF%D0%B0%20%3C%D0%BA%D1%80%D0%BE%D0%B2%D0%B8%3E"),
    ]


async def test_card_without_verified_links_uses_searches():
    pick = music.Pick("", "Хочешь", ("AAAAAAAAAAA",), SPOTIFY)
    card = await music.build_card(pick, FakeChecker())
    assert card.preview_url is None and prompts.MUSIC_NO_PLAYER in card.html
    urls = [b.url for row in card.keyboard.inline_keyboard for b in row]
    assert urls == ["https://music.apple.com/ru/search?term=%D0%A5%D0%BE%D1%87%D0%B5%D1%88%D1%8C",
                    "https://music.youtube.com/search?q=%D0%A5%D0%BE%D1%87%D0%B5%D1%88%D1%8C",
                    "https://open.spotify.com/search/%D0%A5%D0%BE%D1%87%D0%B5%D1%88%D1%8C",
                    "https://music.yandex.ru/search?text=%D0%A5%D0%BE%D1%87%D0%B5%D1%88%D1%8C"]
    assert all(b.text.startswith("🔎") for row in card.keyboard.inline_keyboard for b in row)
