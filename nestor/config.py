from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    allowed_chat_ids: frozenset[int]
    log_level: str = "INFO"
    # Stage 3: answering. Without HERMES_URL the service only archives.
    hermes_url: str | None = None
    hermes_api_key: str = ""
    hermes_model: str = "hermes-agent"
    # Separate Hermes profile with web search and NO archive access. Empty: no internet.
    hermes_web_url: str | None = None
    hermes_web_api_key: str = ""
    # Profile `smart`: a bigger model with web search, also without the archive, for songs and
    # the wisdom of the day. Empty: those use the `web` profile.
    hermes_smart_url: str | None = None
    hermes_smart_api_key: str = ""
    music_daily_limit: int = 10
    # Groups with the wisdom of the day; group admins can switch it with /wisdom on|off.
    wisdom_chat_ids: frozenset[int] = frozenset()
    wisdom_time: time = time(21, 0)
    digest_time: time = time(9, 0)      # the digest itself is switched on by group admins: /digest on
    bot_keywords: tuple[str, ...] = ("нестор", "бот")
    rate_limit: int = 5
    rate_window_min: int = 10
    daily_token_limit: int = 250_000
    timezone: str = "Europe/Tallinn"


def _parse_ids(value: str) -> frozenset[int]:
    return frozenset(int(part) for part in value.replace(" ", "").split(",") if part)


def _parse_time(name: str, default: str) -> time:
    value = os.environ.get(name) or default
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"{name} must look like 21:00, got {value!r}") from None


def load_settings() -> Settings:
    load_dotenv()
    try:
        return Settings(
            bot_token=os.environ["BOT_TOKEN"],
            database_url=os.environ["DATABASE_URL"],
            allowed_chat_ids=_parse_ids(os.environ.get("ALLOWED_CHAT_IDS", "")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            hermes_url=os.environ.get("HERMES_URL") or None,
            hermes_api_key=os.environ.get("HERMES_API_KEY", ""),
            hermes_model=os.environ.get("HERMES_MODEL", "hermes-agent"),
            hermes_web_url=os.environ.get("HERMES_WEB_URL") or None,
            hermes_web_api_key=os.environ.get("HERMES_WEB_API_KEY", ""),
            hermes_smart_url=os.environ.get("HERMES_SMART_URL") or None,
            hermes_smart_api_key=os.environ.get("HERMES_SMART_API_KEY", ""),
            music_daily_limit=int(os.environ.get("MUSIC_DAILY_LIMIT", 10)),
            wisdom_chat_ids=_parse_ids(os.environ.get("WISDOM_CHAT_IDS", "")),
            wisdom_time=_parse_time("WISDOM_TIME", "21:00"),
            digest_time=_parse_time("DIGEST_TIME", "09:00"),
            bot_keywords=tuple(w.strip().lower() for w in os.environ.get("BOT_KEYWORDS", "нестор,бот").split(",")
                               if w.strip()),
            rate_limit=int(os.environ.get("RATE_LIMIT_PER_USER", 5)),
            rate_window_min=int(os.environ.get("RATE_LIMIT_WINDOW_MIN", 10)),
            daily_token_limit=int(os.environ.get("DAILY_TOKEN_LIMIT", 250_000)),
            timezone=os.environ.get("TIMEZONE", "Europe/Tallinn"),
        )
    except KeyError as e:
        raise SystemExit(f"missing required environment variable {e.args[0]}") from None
