"""The alert engine: which problems are announced and resolved, and the one message a run sends.

A check reports findings (problems it sees now) and events (things that happened). A problem is
announced once it failed `confirm` runs in a row, and announced again only when it gets worse
(warn -> crit) or its repeat_token changes (a backup that fails another night). It is resolved,
silently, after `clear` good runs; a check that could not look (`unknown`) changes nothing. An
event is one line per key and cooldown; what happens within the cooldown is counted into the next.

Everything to say waits in the outbox. A run sends at most one message of at most MAX_LINES lines,
and at most MAX_PER_HOUR messages go out in an hour; a problem that opened and closed while it
waited becomes one line «Было: …». In quiet hours (ALERT_QUIET) and while muted messages arrive
without sound; muted, new problems are held until it ends. A crit open for over an hour rings once
even in quiet hours.

The state lives in the watchdog's StateDirectory, so it survives database outages and reboots. Each
run also publishes it with its snapshot in ops_state('watchdog'): a card that cannot be written does
not make every run repeat what the runs before it said.
"""
from __future__ import annotations

import html
import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..bot import prompts
from ..bot.daily import day_name

if TYPE_CHECKING:
    from .checks import CheckResult

log = logging.getLogger("nestor.watchdog")

MAX_LINES = 25
MAX_CHARS = 3800        # of the lines in one message: Telegram takes 4096, the headers fit in the rest
MAX_PER_HOUR = 6
QUIET_RING_AFTER = timedelta(minutes=60)
LEVELS = {"warn": 1, "crit": 2}
STATE_VERSION = 1
_ORDER = {"problem": 0, "event": 1, "resolved": 2, "was": 2}


@dataclass
class Problem:
    key: str
    check: str
    level: str                      # warn | crit
    line: str                       # Telegram HTML, as the check wrote it last
    label: str
    since: datetime
    confirm: int = 1
    clear: int = 1
    token: str | None = None        # the finding's repeat_token now
    fails: int = 0                  # failing runs in a row
    oks: int = 0                    # good runs in a row
    announced: str | None = None    # the level it was announced at; None: not (yet)
    announced_token: str | None = None
    delivered: bool = False         # an announcement of it reached the chat
    rung: bool = False              # one at its level reached it with sound (or the quiet-hours ring is queued)
    crit_since: datetime | None = None    # when it first was crit


@dataclass
class AlertState:
    problems: dict[str, Problem] = field(default_factory=dict)
    events: dict[str, dict] = field(default_factory=dict)    # key -> pending, sent_at, line, cooldown_s
    outbox: list[dict] = field(default_factory=list)          # lines waiting to be sent
    sent_times: list[datetime] = field(default_factory=list)  # messages of the last hour
    last_daily: date | None = None
    baselines: dict[str, int] = field(default_factory=dict)  # counters as last seen (NRestarts)
    mute_until: datetime | None = None
    ran: dict[str, datetime] = field(default_factory=dict)   # when each check last ran
    statuses: dict[str, str] = field(default_factory=dict)   # and what it said: ok | problems | unknown | error
    memo: dict[str, dict] = field(default_factory=dict)      # per check: facts kept between runs
    seq: int = 0
    saved_at: datetime | None = None


@dataclass
class Outgoing:
    text: str
    silent: bool
    items: list[dict]               # the outbox lines it carries: gone from the outbox once delivered


# ---- formatting ----

def fmt_since(at: datetime, now: datetime, tz: ZoneInfo) -> str:
    """14:05 today, otherwise «3 октября 14:05», local time."""
    local = at.astimezone(tz)
    if local.date() == now.astimezone(tz).date():
        return f"{local:%H:%M}"
    return f"{day_name(local.date())} {local:%H:%M}"


def fmt_duration(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} ч" + (f" {minutes} мин" if minutes else "")
    return f"{hours // 24} дн."


def fmt_size(size: int) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} ГБ".replace(".", ",")
    return f"{size // 1024 ** 2} МБ"


def label_for(key: str) -> str:
    return prompts.ALERT_LABELS.get(key.split(".", 1)[0].split(":", 1)[0], html.escape(key))


def in_quiet(local: time, quiet: tuple[time, time] | None) -> bool:
    if quiet is None:
        return False
    start, end = quiet
    return (start <= local or local < end) if start > end else (start <= local < end)


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# ---- the state file ----

def _encode(state: AlertState) -> dict:
    data = asdict(state)
    for key, p in data["problems"].items():
        since, crit_since = state.problems[key].since, state.problems[key].crit_since
        p.update(since=_iso(since), crit_since=_iso(crit_since) if crit_since else None)
    data["sent_times"] = [_iso(t) for t in state.sent_times]
    data["last_daily"] = state.last_daily.isoformat() if state.last_daily else None
    data["mute_until"] = _iso(state.mute_until) if state.mute_until else None
    data["ran"] = {k: _iso(v) for k, v in state.ran.items()}
    data["saved_at"] = _iso(state.saved_at) if state.saved_at else None
    return {"version": STATE_VERSION, **data}


def _decode(data: dict) -> AlertState:
    if data.get("version") != STATE_VERSION:
        raise ValueError("state version")
    known = {f.name for f in fields(Problem)}
    return AlertState(
        problems={k: Problem(**{**{n: v for n, v in p.items() if n in known}, "since": _when(p["since"]),
                                "crit_since": _when(p.get("crit_since"))})
                  for k, p in data["problems"].items()},
        events=dict(data["events"]), outbox=list(data["outbox"]),
        sent_times=[_when(t) for t in data["sent_times"]],
        last_daily=date.fromisoformat(data["last_daily"]) if data["last_daily"] else None,
        baselines=dict(data["baselines"]), mute_until=_when(data["mute_until"]),
        ran={k: _when(v) for k, v in data["ran"].items()}, statuses=dict(data["statuses"]),
        memo=dict(data["memo"]), seq=int(data["seq"]), saved_at=_when(data["saved_at"]))


def load_state(path: Path) -> AlertState:
    """The saved state; a fresh one if there is none. A file that cannot be read starts fresh too (with a
    warning): open problems are then announced again, which is better than never."""
    try:
        return _decode(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return AlertState()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("watchdog state unreadable, starting fresh", extra={"error": type(e).__name__})
        return AlertState()


def write_private(path: Path, text: str) -> None:
    """All or nothing, 0600, on the card before it returns."""
    tmp = path.with_name(path.name + ".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_state(path: Path, state: AlertState, now: datetime) -> None:
    state.saved_at = now
    write_private(path, json.dumps(_encode(state), ensure_ascii=False, indent=1) + "\n")


def adopt(state: AlertState, snapshot: dict | None) -> AlertState:
    """The state the last run published with its snapshot, when that run is newer than the state on the
    card: it could not save it (a read-only or full card), or the file was lost. Otherwise every run would
    say again what the runs since then said (the summary, the events, «снова в порядке») and count the
    hour's messages from a stale list."""
    if not snapshot:
        return state
    checked = _when(snapshot.get("checked_at"))
    if checked is None or (state.saved_at is not None and state.saved_at >= checked):
        return state
    try:
        newer = _decode(snapshot["state"])
    except (KeyError, ValueError, TypeError, AttributeError) as e:
        log.warning("watchdog snapshot state unreadable", extra={"error": type(e).__name__})
        return state
    newer.saved_at = checked
    return newer


# ---- evaluation ----

def _push(state: AlertState, item: dict) -> None:
    state.seq += 1
    state.outbox.append({**item, "n": state.seq})


def _announce(state: AlertState, p: Problem, now: datetime) -> None:
    # A line about this problem that has not gone out yet says something older: this one replaces it.
    state.outbox = [i for i in state.outbox if not (i["kind"] == "problem" and i["key"] == p.key)]
    _push(state, {"kind": "problem", "key": p.key, "level": p.level, "line": p.line, "at": _iso(now)})
    if p.announced is not None and LEVELS[p.level] > LEVELS[p.announced]:
        p.rung = False              # it rang as a warning: as a crit it has not
    p.announced, p.announced_token = p.level, p.token


def _close(state: AlertState, p: Problem, now: datetime, tz: ZoneInfo) -> None:
    del state.problems[p.key]
    if p.announced is None:
        return                      # never announced: nobody needs to hear it ended
    waiting = [i for i in state.outbox if i["kind"] == "problem" and i["key"] == p.key]
    state.outbox = [i for i in state.outbox if not (i["kind"] == "problem" and i["key"] == p.key)]
    if waiting and not p.delivered:
        line = prompts.ALERT_WAS.format(line=p.line, start=fmt_since(p.since, now, tz), end=fmt_since(now, now, tz))
        _push(state, {"kind": "was", "key": p.key, "line": line})
    else:
        _push(state, {"kind": "resolved", "key": p.key,
                      "line": prompts.ALERT_OK.format(label=p.label, duration=fmt_duration(now - p.since))})


def evaluate(state: AlertState, results: list[CheckResult], now: datetime, *, tz: ZoneInfo,
             quiet: tuple[time, time] | None) -> tuple[AlertState, Outgoing | None]:
    """Applies one run's results to the state; returns it and the message to send now, if any."""
    muted = state.mute_until is not None and now < state.mute_until
    for r in results:
        state.statuses[r.name] = r.status
        if r.status == "unknown":
            continue                # it looks again on the next run, whatever its interval
        state.ran[r.name] = now
        seen = set()
        for f in r.findings:
            seen.add(f.key)
            p = state.problems.get(f.key)
            if p is None:
                p = state.problems[f.key] = Problem(key=f.key, check=r.name, level=f.level, line=f.line,
                                                    label=f.label or label_for(f.key), since=now)
            p.level, p.line, p.confirm, p.clear, p.token = f.level, f.line, f.confirm, f.clear, f.repeat_token
            p.check = r.name
            if f.level == "crit" and p.crit_since is None:
                p.crit_since = now
            p.fails, p.oks = p.fails + 1, 0
        if r.status == "error":
            continue                # the check's own problems: as unknown
        for p in [p for p in state.problems.values() if p.check == r.name and p.key not in seen]:
            if any(p.key.startswith(prefix) for prefix in r.unknown):
                continue
            p.fails, p.oks = 0, p.oks + 1
            if p.oks >= p.clear:
                _close(state, p, now, tz)
        for e in r.events:
            ev = state.events.setdefault(e.key, {"pending": 0, "sent_at": None})
            ev.update(pending=ev["pending"] + e.count, line=e.line, cooldown_s=int(e.cooldown.total_seconds()))

    if not muted:
        for p in state.problems.values():
            if p.announced is None:
                if p.fails >= p.confirm:
                    _announce(state, p, now)
            elif p.fails > 0 and (LEVELS[p.level] > LEVELS[p.announced] or p.token != p.announced_token):
                _announce(state, p, now)
    for key, ev in list(state.events.items()):
        sent = _when(ev["sent_at"])
        ready = sent is None or now - sent >= timedelta(seconds=ev["cooldown_s"])
        if ev["pending"] and ready:
            _push(state, {"kind": "event", "key": key, "line": ev["line"].replace("{count}", str(ev["pending"]))})
            ev.update(pending=0, sent_at=_iso(now))
        elif not ev["pending"] and ready:
            del state.events[key]
    if not muted and in_quiet(now.astimezone(tz).time(), quiet):
        for p in state.problems.values():
            if p.announced == "crit" and p.level == "crit" and not p.rung \
                    and now - (p.crit_since or p.since) >= QUIET_RING_AFTER:
                waiting = [i for i in state.outbox if i["kind"] == "problem" and i["key"] == p.key]
                if waiting:
                    waiting[-1]["force"] = True
                else:
                    _push(state, {"kind": "problem", "key": p.key, "level": p.level, "line": p.line,
                                  "at": _iso(now), "force": True})
                p.rung = True
    state.sent_times = [t for t in state.sent_times if now - t < timedelta(hours=1)]
    return state, compose(state, now, tz=tz, quiet=quiet)


def compose(state: AlertState, now: datetime, *, tz: ZoneInfo, quiet: tuple[time, time] | None) -> Outgoing | None:
    """The message for what waits in the outbox, or None: nothing waits or the hour's messages are used."""
    if not state.outbox or len([t for t in state.sent_times if now - t < timedelta(hours=1)]) >= MAX_PER_HOUR:
        return None
    waiting = sorted(state.outbox, key=lambda i: (_ORDER[i["kind"]], -LEVELS.get(i.get("level"), 0), i["n"]))
    chosen, size = [], 0
    for item in waiting if len(waiting) <= MAX_LINES else waiting[:MAX_LINES - 1]:
        size += len(item["line"]) + 4                   # the mark, a space, the newline
        if chosen and size > MAX_CHARS:
            break                                       # the rest goes with the next message
        chosen.append(item)
    lines = []
    for kind, header in (("problem", prompts.ALERT_NEW), ("event", prompts.ALERT_EVENTS),
                         ("resolved", prompts.ALERT_RESOLVED)):
        part = [i for i in chosen if _ORDER[i["kind"]] == _ORDER[kind]]
        if part:
            lines.append(("\n" if lines else "") + header)
            lines += [f"{_mark(i)} {i['line']}" for i in part]
    if len(chosen) < len(waiting):
        lines.append(prompts.ALERT_MORE.format(count=len(waiting) - len(chosen)))
    muted = state.mute_until is not None and now < state.mute_until
    loud = any(i["kind"] in ("problem", "event") for i in chosen) and not muted \
        and not in_quiet(now.astimezone(tz).time(), quiet)
    return Outgoing("\n".join(lines), silent=not (loud or any(i.get("force") for i in chosen)), items=chosen)


def _mark(item: dict) -> str:
    kind = item["kind"]
    return prompts.ALERT_MARKS[item["level"] if kind == "problem" else "event" if kind == "event" else "resolved"]


def delivered(state: AlertState, out: Outgoing, now: datetime) -> None:
    """The message went out: its lines leave the outbox and count toward the hour."""
    gone = {i["n"] for i in out.items}
    state.outbox = [i for i in state.outbox if i["n"] not in gone]
    state.sent_times.append(now)
    for i in out.items:
        p = state.problems.get(i["key"]) if i["kind"] == "problem" else None
        if p is not None:
            p.delivered = True
            p.rung = p.rung or not out.silent


def daily_due(state: AlertState, now: datetime, tz: ZoneInfo, at: time | None) -> bool:
    local = now.astimezone(tz)
    return at is not None and local.time() >= at and state.last_daily != local.date()


# ---- the snapshot in ops_state('watchdog') and /status ----

def confirmed(state: AlertState) -> list[Problem]:
    """Open problems that are real: announced, or confirmed and held by the mute."""
    found = [p for p in state.problems.values() if p.announced is not None or p.fails >= p.confirm]
    return sorted(found, key=lambda p: (-LEVELS[p.level], p.since))


def snapshot(state: AlertState, now: datetime) -> dict:
    """What /status shows, and the whole state for the next run if this one cannot save it (adopt)."""
    return {"checked_at": _iso(now),
            "problems": [{"key": p.key, "level": p.level, "line": p.line, "since": _iso(p.since), "check": p.check,
                          "announced": p.announced is not None} for p in confirmed(state)],
            "checks": dict(sorted(state.statuses.items())),
            "muted_until": _iso(state.mute_until) if state.mute_until and state.mute_until > now else None,
            "state": _encode(state)}


def render_status(snap: dict, now: datetime, tz: ZoneInfo) -> str:
    """/status and --dry-run: the open problems of the last run, or that all is well."""
    lines = [prompts.STATUS_HEADER.format(time=fmt_since(_when(snap["checked_at"]), now, tz))]
    problems = snap.get("problems") or []
    lines += [f"{prompts.ALERT_MARKS.get(p['level'], '•')} {p['line']}" for p in problems]
    if not problems:
        lines.append(prompts.STATUS_ALL_OK)
    unchecked = sorted({label_for(name) for name, status in (snap.get("checks") or {}).items()
                        if status in ("unknown", "error")})
    if unchecked:
        lines.append(prompts.STATUS_UNCHECKED.format(names=", ".join(unchecked)))
    if snap.get("muted_until"):
        lines.append(prompts.STATUS_MUTED.format(time=fmt_since(_when(snap["muted_until"]), now, tz)))
    return "\n".join(lines)
