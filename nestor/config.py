from __future__ import annotations

import os
from dataclasses import dataclass

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
    bot_keywords: tuple[str, ...] = ("нестор", "бот")
    rate_limit: int = 5
    rate_window_min: int = 10
    daily_token_limit: int = 250_000
    timezone: str = "Europe/Tallinn"


def _parse_ids(value: str) -> frozenset[int]:
    return frozenset(int(part) for part in value.replace(" ", "").split(",") if part)


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
            bot_keywords=tuple(w.strip().lower() for w in os.environ.get("BOT_KEYWORDS", "нестор,бот").split(",")
                               if w.strip()),
            rate_limit=int(os.environ.get("RATE_LIMIT_PER_USER", 5)),
            rate_window_min=int(os.environ.get("RATE_LIMIT_WINDOW_MIN", 10)),
            daily_token_limit=int(os.environ.get("DAILY_TOKEN_LIMIT", 250_000)),
            timezone=os.environ.get("TIMEZONE", "Europe/Tallinn"),
        )
    except KeyError as e:
        raise SystemExit(f"missing required environment variable {e.args[0]}") from None
