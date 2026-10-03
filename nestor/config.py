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
        )
    except KeyError as e:
        raise SystemExit(f"missing required environment variable {e.args[0]}") from None
