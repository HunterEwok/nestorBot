"""Settings from the environment, as systemd's EnvironmentFile hands them over."""
from datetime import time

import pytest

from nestor import config

NAMES = ("ALLOWED_CHAT_IDS", "MODEL_ROUTES", "MUSIC_DAILY_LIMIT", "MUSIC_PLAYER_APP", "NEWS_CHAT_IDS", "NEWS_TIME",
         "NEWS_MAX_TOPICS", "NEWS_CONTACT_EMAIL", "NEWS_TELEGRAM_CHANNELS", "WISDOM_TIME", "DIGEST_TIME", "TIMEZONE")


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
                                                                               "Europe/Tallinn")
    env.setenv("NEWS_MAX_TOPICS", "десять")
    with pytest.raises(SystemExit, match="NEWS_MAX_TOPICS must be a whole number"):
        config.load_settings()
    env.setenv("NEWS_MAX_TOPICS", "10")
    env.setenv("MODEL_ROUTES", "theses:web")
    with pytest.raises(SystemExit, match="MODEL_ROUTES"):
        config.load_settings()
