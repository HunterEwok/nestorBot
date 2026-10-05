"""What the watchdog asks the Pi and Hermes: systemctl, the clock's sync, the card, the Hermes gateway.
Every probe returns codes and numbers, never a server's text."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from ..backup import TIMESYNC_FLAG
from ..config import Settings

HERMES_TIMEOUT_S = 10
# A news source's status may be "error: <what a server said>"; only these leave the database as they are.
_SAFE_STATUS = re.compile(r"^(ok|skipped|timeout|http_\d{3})$")
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


class ProbeError(Exception):
    """A probe that could not be made (systemctl missing or refusing); the code only."""


def hermes_profiles(settings: Settings) -> list[tuple[str, str, str]]:
    """(profile, url, key) of every Hermes profile the collector is configured with."""
    found = (("group", settings.hermes_url, settings.hermes_api_key),
             ("deep", settings.hermes_deep_url, settings.hermes_deep_api_key),
             ("web", settings.hermes_web_url, settings.hermes_web_api_key),
             ("smart", settings.hermes_smart_url, settings.hermes_smart_api_key),
             ("news", settings.hermes_news_url, settings.hermes_news_api_key))
    return [(name, url, key) for name, url, key in found if url]


async def probe_hermes(session: aiohttp.ClientSession, profiles: list[tuple[str, str, str]],
                       timeout_s: float = HERMES_TIMEOUT_S) -> dict[str, str]:
    """profile -> ok | http_NNN | refused | timeout | error, from GET {url}/models. The body is not read:
    nothing Hermes says reaches a log or an alert."""
    async def one(name: str, url: str, key: str) -> tuple[str, str]:
        try:
            async with session.get(url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"},
                                   timeout=aiohttp.ClientTimeout(total=timeout_s), allow_redirects=False) as resp:
                return name, "ok" if resp.status == 200 else f"http_{resp.status}"
        except TimeoutError:
            return name, "timeout"
        except (aiohttp.ClientConnectorError, ConnectionError):
            return name, "refused"
        except (aiohttp.ClientError, OSError):
            return name, "error"

    return dict(await asyncio.gather(*(one(*p) for p in profiles)))


async def run_systemctl(*args: str) -> str:
    """stdout of `systemctl <args>`; ProbeError when it cannot run or fails."""
    try:
        proc = await asyncio.create_subprocess_exec("systemctl", *args, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except (TimeoutError, OSError) as e:
        raise ProbeError(type(e).__name__) from None
    if proc.returncode != 0:
        raise ProbeError(f"systemctl_exit_{proc.returncode}")
    return out.decode("utf-8", "replace")


UNIT_PROPERTIES = ("ActiveState", "SubState", "Result", "NRestarts", "ExecMainStatus", "MemoryPeak")


def parse_show(text: str) -> dict[str, str]:
    """`systemctl show -p A,B` output (A=value lines) as a dict."""
    found = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep:
            found[name.strip()] = value.strip()
    return found


def as_int(value: str | None) -> int | None:
    """A number systemctl printed; None for [not set], infinity and the like."""
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def read_json(path: Path) -> dict | None:
    """The JSON object in `path`; None if the file is not there. Anything else unreadable raises."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError("not a JSON object")
    return data


def safe_status(status: str | None) -> str:
    return status if status is not None and _SAFE_STATUS.match(status) else "error"


def safe_code(value: object) -> str:
    """An error code from a status the watchdog did not write (nestor.backup's file, maintenance_runs, the
    collector's heartbeat): shown only when it looks like one, so a stray server reply never reaches a text."""
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else "error"


async def clock_synced() -> bool:
    """As nestor.backup sees it: systemd-timesyncd's flag file, or timedatectl's NTPSynchronized."""
    if TIMESYNC_FLAG.exists():
        return True
    try:
        proc = await asyncio.create_subprocess_exec("timedatectl", "show", "-p", "NTPSynchronized", "--value",
                                                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except (TimeoutError, OSError):
        return False
    return proc.returncode == 0 and out.decode().strip() == "yes"


def _readable(path: Path) -> bool:
    return os.access(path, os.R_OK)


@dataclass(frozen=True)
class Host:
    """The Pi as the checks see it; tests replace what they need."""
    synced: Callable[[], Awaitable[bool]] = clock_synced
    disk_usage: Callable[[str], tuple] = field(default=shutil.disk_usage)
    readable: Callable[[Path], bool] = _readable      # as the user the watchdog runs as (nestor)
