from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    # Profile `deep`: the archive tools on a bigger model, for disputes and «нестор, подумай». Empty: `group`.
    hermes_deep_url: str | None = None
    hermes_deep_api_key: str = ""
    # Profile `news`: no tools, a cheap model, sorts news items. Empty: `web`.
    hermes_news_url: str | None = None
    hermes_news_api_key: str = ""
    model_routes: dict[str, str] = field(default_factory=dict)   # MODEL_ROUTES, see nestor.bot.routing
    music_daily_limit: int = 10
    # Short name of the player Mini App (BotFather /newapp, page in deploy/player): songs get a button
    # that plays the video inside Telegram on a computer too. Empty: no button.
    music_player_app: str = ""
    # Groups with the wisdom of the day; group admins can switch it with /wisdom on|off.
    wisdom_chat_ids: frozenset[int] = frozenset()
    wisdom_time: time = time(21, 0)
    digest_time: time = time(9, 0)      # the digest itself is switched on by group admins: /digest on
    # Groups with the evening news digest; group admins can switch it with /news on|off.
    news_chat_ids: frozenset[int] = frozenset()
    news_time: time = time(20, 0)
    news_max_topics: int = 10
    news_contact_email: str = ""        # for NCBI's email= and the User-Agent of the news fetcher
    news_telegram_channels: tuple[str, ...] = ("psychedelicsocietyvienna",)
    # Groups where the bot puts reactions on emotional messages; group admins switch it with /reactions on|off.
    reaction_chat_ids: frozenset[int] = frozenset()
    bot_keywords: tuple[str, ...] = ("нестор", "бот")
    rate_limit: int = 5
    rate_window_min: int = 10
    daily_token_limit: int = 250_000
    timezone: str = "Europe/Moscow"
    # The nightly purge of what entered the archive more than 12 months ago (nestor.retention), local time.
    retention_time: time = time(3, 30)
    # Alerts of nestor.watchdog go to this group (it must be in ALLOWED_CHAT_IDS); None: no alerts.
    alert_chat_id: int | None = None
    alert_daily_at: time | None = time(9, 30)           # the silent daily summary, local time; None: off
    alert_quiet: tuple[time, time] | None = (time(23), time(8))   # alerts arrive without sound; None: never quiet
    alert_disk_min_gb: int = 5          # free space on the Pi's card below this is a warning
    alert_max_members: int = 2          # the alert group with more members (the bot counts) gets no summary
    backup_status_file: str = ""        # nestor.backup's status.json; empty: backups are not watched


_COMMENT = re.compile(r"(?:^|\s)#.*\Z", re.S)


def _env(name: str, default: str = "") -> str:
    """A setting without a trailing «# comment»: systemd's EnvironmentFile keeps it in the value."""
    value = _COMMENT.sub("", os.environ.get(name, "")).strip()
    return value or default


def _int(name: str, default: int) -> int:
    value = _env(name, str(default))
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, got {value!r}") from None


def _parse_ids(value: str) -> frozenset[int]:
    return frozenset(int(part) for part in value.replace(" ", "").split(",") if part)


def _parse_time(name: str, default: str) -> time:
    value = _env(name, default)
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"{name} must look like 21:00, got {value!r}") from None


_OFF = ("off", "выкл", "0", "нет", "no")      # as the group commands take it


def _parse_id(name: str) -> int | None:
    value = _env(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"{name} must be a chat id like -1001234567890, got {value!r}") from None


def _parse_optional_time(name: str, default: str) -> time | None:
    """A local time, or None for off (off, выкл, 0, нет, no)."""
    if _env(name, default).lower() in _OFF:
        return None
    return _parse_time(name, default)


def _parse_quiet(name: str, default: str) -> tuple[time, time] | None:
    """HH:MM-HH:MM (it may cross midnight), or None for off."""
    value = _env(name, default)
    if value.lower() in _OFF:
        return None
    try:
        start, end = (time.fromisoformat(part.strip()) for part in value.split("-"))
    except ValueError:
        raise SystemExit(f"{name} must look like 23:00-08:00 or off, got {value!r}") from None
    if start == end:
        raise SystemExit(f"{name} must look like 23:00-08:00 or off, got {value!r}")
    return start, end


def check_alert_chat(settings: Settings) -> str | None:
    """Why ALERT_CHAT_ID cannot work, or None. The bot leaves every group not in ALLOWED_CHAT_IDS."""
    if settings.alert_chat_id is not None and settings.alert_chat_id not in settings.allowed_chat_ids:
        return "ALERT_CHAT_ID must be one of ALLOWED_CHAT_IDS: the bot leaves every other group"
    return None


def _timezone() -> str:
    value = _env("TIMEZONE", "Europe/Moscow")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):   # unknown name, a path, a directory
        raise SystemExit(f"TIMEZONE must be an IANA name like Europe/Moscow, got {value!r}") from None
    return value


def _routes() -> dict[str, str]:
    from .bot.routing import parse_routes

    try:
        return parse_routes(_env("MODEL_ROUTES"))
    except ValueError as e:
        raise SystemExit(f"MODEL_ROUTES: {e}") from None


def _channels() -> tuple[str, ...]:
    from .news.sources import parse_channels

    try:
        return parse_channels(_env("NEWS_TELEGRAM_CHANNELS", "psychedelicsocietyvienna"))
    except ValueError as e:
        raise SystemExit(f"NEWS_TELEGRAM_CHANNELS: {e}") from None


def _player_app() -> str:
    value = _env("MUSIC_PLAYER_APP")
    if value and not re.fullmatch(r"[A-Za-z0-9_]{3,30}", value):
        raise SystemExit("MUSIC_PLAYER_APP: the Mini App short name from BotFather, 3-30 letters, digits or _")
    return value


def load_settings() -> Settings:
    load_dotenv()
    try:
        return Settings(
            bot_token=os.environ["BOT_TOKEN"],
            database_url=os.environ["DATABASE_URL"],
            allowed_chat_ids=_parse_ids(_env("ALLOWED_CHAT_IDS")),
            log_level=_env("LOG_LEVEL", "INFO"),
            hermes_url=os.environ.get("HERMES_URL") or None,
            hermes_api_key=os.environ.get("HERMES_API_KEY", ""),
            hermes_model=_env("HERMES_MODEL", "hermes-agent"),
            hermes_web_url=os.environ.get("HERMES_WEB_URL") or None,
            hermes_web_api_key=os.environ.get("HERMES_WEB_API_KEY", ""),
            hermes_smart_url=os.environ.get("HERMES_SMART_URL") or None,
            hermes_smart_api_key=os.environ.get("HERMES_SMART_API_KEY", ""),
            hermes_deep_url=os.environ.get("HERMES_DEEP_URL") or None,
            hermes_deep_api_key=os.environ.get("HERMES_DEEP_API_KEY", ""),
            hermes_news_url=os.environ.get("HERMES_NEWS_URL") or None,
            hermes_news_api_key=os.environ.get("HERMES_NEWS_API_KEY", ""),
            model_routes=_routes(),
            music_daily_limit=_int("MUSIC_DAILY_LIMIT", 10),
            music_player_app=_player_app(),
            wisdom_chat_ids=_parse_ids(_env("WISDOM_CHAT_IDS")),
            wisdom_time=_parse_time("WISDOM_TIME", "21:00"),
            digest_time=_parse_time("DIGEST_TIME", "09:00"),
            news_chat_ids=_parse_ids(_env("NEWS_CHAT_IDS")),
            news_time=_parse_time("NEWS_TIME", "20:00"),
            news_max_topics=_int("NEWS_MAX_TOPICS", 10),
            news_contact_email=_env("NEWS_CONTACT_EMAIL"),
            news_telegram_channels=_channels(),
            reaction_chat_ids=_parse_ids(_env("REACTION_CHAT_IDS")),
            bot_keywords=tuple(w.strip().lower() for w in _env("BOT_KEYWORDS", "нестор,бот").split(",")
                               if w.strip()),
            rate_limit=_int("RATE_LIMIT_PER_USER", 5),
            rate_window_min=_int("RATE_LIMIT_WINDOW_MIN", 10),
            daily_token_limit=_int("DAILY_TOKEN_LIMIT", 250_000),
            timezone=_timezone(),
            retention_time=_parse_time("RETENTION_TIME", "03:30"),
            alert_chat_id=_parse_id("ALERT_CHAT_ID"),
            alert_daily_at=_parse_optional_time("ALERT_DAILY_AT", "09:30"),
            alert_quiet=_parse_quiet("ALERT_QUIET", "23:00-08:00"),
            alert_disk_min_gb=_int("ALERT_DISK_MIN_GB", 5),
            alert_max_members=_int("ALERT_MAX_MEMBERS", 2),
            backup_status_file=_env("BACKUP_STATUS_FILE"),
        )
    except KeyError as e:
        raise SystemExit(f"missing required environment variable {e.args[0]}") from None
