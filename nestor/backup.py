"""Backups: a nightly pg_dump of the archive from the database on the router to the Pi's microSD, and the
restore from such a dump after a disaster.

    python -m nestor.backup [--force]                          # the unit; --force dumps even if tonight's is done
    python -m nestor.backup restore --database NAME [--optouts PATH] < nestor-YYYYMMDDTHHMMSSZ.dump
    python -m nestor.backup forget --since YYYYMMDDTHHMMSSZ      # after the restore and the import

The unit (deploy/systemd/nestor-backup.service) runs as its own Linux user with the database role
nestor_backup, which can only read. It dumps at BACKUP_AT (04:30, after the 03:30 purge) and once more
at 13:30, which does something only if the night failed. Every dump is written to a partial file, read
back completely by pg_restore, and only then renamed, so a file named nestor-*.dump is a good copy.
Rotation keeps 7 daily and 4 weekly copies, none older than 35 days (nestor.promises), but always the
newest 2: 40 failed nights keep the last two good ones. It runs on every run, before the dump (so a night
that fails lets what expired go too, and a full card frees it before the space is checked) and after it,
and only while the clock can be trusted to say what is old.

The unit's state is in /var/lib/nestor-backup: dumps/ (0700, nobody else reads the archive),
status.json (0644: the watchdog reads it), last-error.txt (0600: the tail of pg_dump's stderr; the
only place that text goes, never the journal or the status) and the lock.

The restore runs as user nestor with the dump on stdin, into an empty database that bootstrap created.
A copy brings back what was forgotten since it was made, so after loading it applies the migrations,
replays every opt-out the watchdog recorded (--optouts) and purges what has outlived the retention. A
/forget_me made after the watchdog last wrote that list is found after the import, by `forget`.

At import only the standard library and nestor.promises: the backup user needs nothing else to start.
The settings bring nestor.config, the restore asyncpg and the rest of nestor.
"""
from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from .promises import BACKUP_KEEP_LAST, BACKUP_MAX_AGE_DAYS

log = logging.getLogger("nestor.backup")

DUMP_RE = re.compile(r"^nestor-(\d{8}T\d{6}Z)\.dump$")
# Tables whose data must be in the dump: a dump without them is not a backup of this archive.
REQUIRED_DATA = ("public chats", "public users", "public messages", "public bot_requests", "public schema_migrations")
RETRY_CODES = frozenset({"unreachable", "lock_timeout", "too_many_connections", "starting_up", "timeout"})
EX_TEMPFAIL = 75                    # another run holds the lock
EX_IOERR = 74                       # the card takes nothing, not even the status: the unit fails, the copies' age speaks
MIB = 1024 * 1024
ERROR_TAIL = 4096                   # bytes of stderr kept in last-error.txt
VERIFY_TIMEOUT_S = 900              # reading a dump back, from the card
PG_LIB = Path("/usr/lib/postgresql")
TIMESYNC_FLAG = Path("/run/systemd/timesync/synchronized")
ENV_FILE = "/opt/nestor/.env"       # restore runs by hand as nestor, without the unit's EnvironmentFile
CLOCK_SLACK = timedelta(minutes=5)  # a newer dump than now + this: the clock went backwards

# What stderr of pg_dump and pg_restore says, by cause, first match wins. The disk comes before the full
# disk: "could not write to output file: Input/output error" is a dying card, not a full one.
ERRORS = (
    ("hba_rejected", ("no pg_hba.conf entry",)),
    ("unreachable", ("Connection refused", "could not connect", "timeout expired", "No route to host",
                     "could not translate host name", "server closed the connection")),
    ("too_many_connections", ("too many connections",)),
    ("starting_up", ("the database system is starting up", "the database system is shutting down")),
    ("auth_failed", ("password authentication failed", "no password supplied")),
    ("version_mismatch", ("server version mismatch",)),
    ("permission_denied", ("permission denied",)),
    ("lock_timeout", ("could not obtain lock", "lock timeout")),
    ("disk_error", ("Read-only file system", "Input/output error")),
    ("no_space", ("No space left on device", "could not write to output file")),
    ("compress_unsupported", ("does not support compression",)),
)

# The keys of status.json, which only the watchdog reads. `ok` and `error` are the verdict of the last finished
# run, `last_ok_*` the last good dump; `shrank_pct` and `shrank_since` (when the dump it is compared with was
# made) are added beside the warning "shrank" and nowhere else.
STATUS_KEYS = ("version", "running", "ok", "error", "attempts", "started_at", "finished_at", "file", "size",
               "duration_s", "server_version", "client_version", "free_mb", "kept", "deleted", "oldest_dump_at",
               "rotated", "warnings", "failed_nights", "last_ok_at", "last_ok_file", "last_ok_size")
SHRANK_KEYS = ("shrank_pct", "shrank_since")


class BackupError(Exception):
    """A run or a restore that failed. `code` is what the status and the watchdog say; `stderr` is what the
    tool printed, for last-error.txt only: it never reaches a log, a status or the terminal."""

    def __init__(self, code: str, returncode: int | None = None, stderr: str = "", *, loaded: bool = False):
        super().__init__(code)
        self.code, self.returncode, self.stderr = code, returncode, stderr
        self.loaded = loaded        # a restore that failed after the copy was in the database

    @property
    def lines(self) -> int:
        return len(self.stderr.splitlines())


class BackupBusy(Exception):
    """Another run holds the lock."""


@dataclass(frozen=True)
class BackupConfig:
    database_url: str                   # BACKUP_DATABASE_URL: no password, that is what the pgpass file is for
    tz: ZoneInfo                        # TIMEZONE
    at: dtime = dtime(4, 30)            # BACKUP_AT: the scheduled slot; must match the timer
    state_dir: Path = Path("/var/lib/nestor-backup")
    pg_bindir: Path | None = None       # BACKUP_PG_BINDIR; None: the newest /usr/lib/postgresql/*/bin
    compress: str = "zstd:3"            # BACKUP_COMPRESS; gzip:6 where pg_dump has no zstd
    min_free_mb: int = 1024             # BACKUP_MIN_FREE_MB, on top of twice the last dump
    timeout_s: int = 600                # BACKUP_TIMEOUT_MIN, per attempt
    attempts: int = 6                   # BACKUP_ATTEMPTS
    retry_delay_s: int = 300            # BACKUP_RETRY_MIN

    @property
    def dumps_dir(self) -> Path:
        return self.state_dir / "dumps"

    @property
    def status_file(self) -> Path:
        return self.state_dir / "status.json"

    @property
    def error_file(self) -> Path:
        return self.state_dir / "last-error.txt"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "lock"


def _password_of(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.password is not None:
        return unquote(parts.password)
    for pair in parts.query.split("&"):
        if pair.lower().startswith("password="):
            return unquote(pair.split("=", 1)[1])
    return None


def _without_password(url: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    query = "&".join(p for p in parts.query.split("&") if not p.lower().startswith("password="))
    return parts._replace(netloc=f"{parts.username}@{host}" if parts.username else host, query=query).geturl()


def load_config() -> BackupConfig:
    """From the environment (the unit's EnvironmentFile), the way nestor.config reads it. No .env file:
    the unit has none, and the pgpass file is the only place the password is."""
    from .config import _env, _int, _parse_time, _timezone

    url = _env("BACKUP_DATABASE_URL")
    if not url:
        raise SystemExit("missing required environment variable BACKUP_DATABASE_URL")
    if urlsplit(url).scheme not in ("postgresql", "postgres"):
        raise SystemExit("BACKUP_DATABASE_URL must be a postgresql:// URL")
    if _password_of(url) is not None:     # the value is not echoed: it would be the password
        raise SystemExit("BACKUP_DATABASE_URL must not contain a password: it goes into the pgpass file")
    compress = _env("BACKUP_COMPRESS", "zstd:3")
    if not re.fullmatch(r"[a-z0-9:=,]{1,40}", compress):
        raise SystemExit(f"BACKUP_COMPRESS must look like zstd:3 or gzip:6, got {compress!r}")

    def positive(name: str, default: int) -> int:
        value = _int(name, default)
        if value < 1:
            raise SystemExit(f"{name} must be at least 1, got {value}")
        return value

    bindir = _env("BACKUP_PG_BINDIR")
    return BackupConfig(
        database_url=url,
        tz=ZoneInfo(_timezone()),
        at=_parse_time("BACKUP_AT", "04:30"),
        pg_bindir=Path(bindir) if bindir else None,
        compress=compress,
        min_free_mb=positive("BACKUP_MIN_FREE_MB", 1024),
        timeout_s=positive("BACKUP_TIMEOUT_MIN", 10) * 60,
        attempts=positive("BACKUP_ATTEMPTS", 6),
        retry_delay_s=positive("BACKUP_RETRY_MIN", 5) * 60,
    )


def find_bindir(configured: Path | None, root: Path = PG_LIB) -> Path:
    """The directory with pg_dump and pg_restore: the configured one, else the newest installed major."""
    if configured is not None:
        candidates = [configured]
    else:
        majors = sorted((p for p in root.glob("[0-9]*") if p.name.isdigit()), key=lambda p: int(p.name),
                        reverse=True)
        candidates = [p / "bin" for p in majors]
    for directory in candidates:
        if all(os.access(directory / tool, os.X_OK) for tool in ("pg_dump", "pg_restore")):
            return directory
    raise BackupError("client_missing")


def client_version(bindir: Path, run=subprocess.run) -> str:
    done = run([str(bindir / "pg_dump"), "--version"], capture_output=True, text=True, timeout=30)
    found = re.search(r"\(PostgreSQL\) (\S+)", done.stdout or "")
    if done.returncode != 0 or not found:
        raise BackupError("client_missing", done.returncode)
    return found.group(1)


def dump_name(at: datetime) -> str:
    return f"nestor-{at.astimezone(UTC):%Y%m%dT%H%M%SZ}.dump"


def list_dumps(directory: Path) -> list[tuple[datetime, Path]]:
    """The finished dumps, oldest first. Partial files and anything else in the directory are not dumps."""
    found = []
    for path in directory.iterdir() if directory.is_dir() else ():
        match = DUMP_RE.match(path.name)
        if match and path.is_file():
            found.append((datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), path))
    return sorted(found)


def plan_rotation(taken: list[datetime], now: datetime, *, daily: int = 7, weekly: int = 4,
                  max_age: timedelta = timedelta(days=BACKUP_MAX_AGE_DAYS),
                  keep_last: int = BACKUP_KEEP_LAST) -> set[datetime]:
    """Which dumps (by time) to keep: the newest of each of the last `daily` UTC days; of each of the `weekly`
    ISO weeks before this one, the newest that is older than those days, so a Wednesday keeps 7 + 4 = 11;
    nothing older than `max_age`; but always the newest `keep_last`, however old."""
    taken = sorted(t.astimezone(UTC) for t in taken)
    today = now.astimezone(UTC).date()
    first_daily = today - timedelta(days=daily - 1)
    newest_of_day = {t.date(): t for t in taken}          # ascending, so the last one of a day wins
    keep = {t for day, t in newest_of_day.items() if first_daily <= day <= today}
    this_monday = today - timedelta(days=today.weekday())
    for weeks_back in range(1, weekly + 1):
        monday = this_monday - timedelta(weeks=weeks_back)
        week = [t for t in taken if monday <= t.date() < min(monday + timedelta(days=7), first_daily)]
        if week:
            keep.add(week[-1])
    keep = {t for t in keep if now - t <= max_age}
    return keep | set(taken[-keep_last:])


def clock_synced(run=subprocess.run) -> bool:
    if TIMESYNC_FLAG.exists():
        return True
    try:
        done = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True, text=True,
                   timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and done.stdout.strip() == "yes"


def due(status: dict | None, now: datetime, tz: ZoneInfo, at: dtime) -> bool:
    """False if a dump succeeded since the latest slot `at` before now: the 13:30 run after a good night, a
    run by hand at 15:00 and then the next 04:30 is still due (the slot did not move)."""
    local = now.astimezone(tz)
    slot = datetime.combine(local.date(), at, tz)
    if slot > now:
        slot = datetime.combine(local.date() - timedelta(days=1), at, tz)
    try:
        return datetime.fromisoformat(status["last_ok_at"]) < slot
    except (TypeError, KeyError, ValueError):
        return True


def classify(stderr: str) -> str:
    for code, needles in ERRORS:
        if any(needle in stderr for needle in needles):
            return code
    return "failed"


def check_space(directory: Path, need: int) -> int:
    """The free bytes under `directory`; BackupError("low_space") if they are fewer than `need`."""
    free = shutil.disk_usage(directory).free
    if free < need:
        raise BackupError("low_space")
    return free


def run_pg_dump(cfg: BackupConfig, bindir: Path, target: Path, run=subprocess.run) -> None:
    # pg_dump creates the file with the umask's mode; one that exists keeps its own.
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    argv = [str(bindir / "pg_dump"), "--format=custom", f"--compress={cfg.compress}", "--lock-wait-timeout=60s",
            "--no-password", "-f", str(target), cfg.database_url]
    env = {**os.environ, "PGCONNECT_TIMEOUT": "10", "PGAPPNAME": "nestor-backup"}
    try:
        done = run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   text=True, errors="replace", timeout=cfg.timeout_s)
    except subprocess.TimeoutExpired:
        raise BackupError("timeout") from None
    if done.returncode != 0:
        raise BackupError(classify(done.stderr), done.returncode, done.stderr)


def verify(bindir: Path, path: Path, run=subprocess.run) -> str:
    """Reads the whole dump back; returns the server version it was made from. BackupError("verify_failed")
    for a file that does not list, lacks the tables of the archive, or does not read to the end: a dump cut
    short still lists, its table of contents is at the start."""
    def call(*args: str) -> subprocess.CompletedProcess:
        try:
            return run([str(bindir / "pg_restore"), *args], stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, errors="replace", timeout=VERIFY_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise BackupError("verify_failed") from None

    listing = call("--list", str(path))
    if listing.returncode != 0:
        raise BackupError("verify_failed", listing.returncode, listing.stderr)
    missing = [t for t in REQUIRED_DATA if f"TABLE DATA {t} " not in listing.stdout]
    version = re.search(r"Dumped from database version: (\S+)", listing.stdout)
    if missing or not version:
        raise BackupError("verify_failed", None, ("no data for: " + ", ".join(missing)) if missing else "no version")
    read = call("--file=/dev/null", str(path))
    if read.returncode != 0:
        raise BackupError("verify_failed", read.returncode, read.stderr)
    return version.group(1)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_status(path: Path, status: dict) -> None:
    """All or nothing, and on the card before it returns. 0644 whatever the umask: the watchdog is another user."""
    tmp = path.with_name(path.name + ".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644), "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o644)
        json.dump(status, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def load_status(path: Path) -> dict | None:
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None


def _write_error(path: Path, text: str) -> None:
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(text.encode("utf-8", "replace")[-ERROR_TAIL:])


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _os_code(e: OSError) -> str:
    return {errno.EROFS: "disk_error", errno.EIO: "disk_error", errno.ENOSPC: "no_space"}.get(e.errno, "failed")


def _acquire(lock_file: Path) -> int:
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise BackupBusy from None
    return fd


def backup(cfg: BackupConfig, now: datetime | None = None, *, force: bool = False, run=subprocess.run,
           sleep=time.sleep) -> dict | None:
    """One run: dump, verify, rotate, write the status. Returns the status written, None if nothing was due.
    A failed dump is a status with ok false, not an exception; BackupBusy if another run holds the lock."""
    now = now or datetime.now(UTC)
    cfg.state_dir.mkdir(mode=0o711, parents=True, exist_ok=True)
    lock = _acquire(cfg.lock_file)
    try:
        previous = load_status(cfg.status_file)
        if not force and not due(previous, now, cfg.tz, cfg.at):
            log.info("backup not needed")
            return None
        return _backup(cfg, now, previous or {}, run, sleep)
    finally:
        os.close(lock)


def _rotate(directory: Path, now: datetime, warnings: list[str]) -> int:
    """Deletes the dumps plan_rotation does not keep; how many. A card that refuses leaves them, which is
    a warning ("rotation_failed"), not a failed backup: the dumps that are there are good."""
    deleted = 0
    try:
        dumps = list_dumps(directory)
        keep = plan_rotation([t for t, _ in dumps], now)
        for at, path in dumps:
            if at not in keep:
                path.unlink()
                deleted += 1
                log.info("dump deleted", extra={"file": path.name})
    except OSError:
        if "rotation_failed" not in warnings:
            warnings.append("rotation_failed")
    return deleted


def _backup(cfg: BackupConfig, now: datetime, previous: dict, run, sleep) -> dict:
    began = time.monotonic()
    status = {"version": 1, "running": False, "ok": None, "error": None, "attempts": 0, "started_at": None,
              "finished_at": None, "file": None, "size": None, "duration_s": None, "server_version": None,
              "client_version": None, "free_mb": None, "kept": None, "deleted": None, "oldest_dump_at": None,
              "rotated": False, "warnings": [], "failed_nights": 0, "last_ok_at": None, "last_ok_file": None,
              "last_ok_size": None}
    status.update({k: previous[k] for k in (*STATUS_KEYS, *SHRANK_KEYS) if k in previous})
    # While it runs, the watchdog sees the last verdict and its warnings, so a retry of a failed night does
    # not flap; only `running` and `started_at` are new. A run that dies leaves `running` set, which is how
    # it is found.
    status.update(running=True, started_at=_iso(now))
    try:
        write_status(cfg.status_file, status)
    except OSError as e:    # a full card: the rotation below frees what expired, the space check says the rest
        log.warning("backup status not written", extra={"error": _os_code(e)})
    # What the finished status says about this run only: a run that fails early leaves them empty.
    status.update(file=None, size=None, server_version=None, client_version=None, free_mb=None)
    for key in SHRANK_KEYS:
        status.pop(key, None)
    last_size = previous["last_ok_size"] if isinstance(previous.get("last_ok_size"), int) else 0
    attempts, failure, warnings, deleted, trusted = 0, None, [], 0, False
    try:
        cfg.dumps_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(cfg.dumps_dir, 0o700)
        for stale in cfg.dumps_dir.glob(".*.partial"):
            stale.unlink()
        # Only a trustworthy clock may decide what is old. Before the dump, so a night that fails still
        # lets the expired copies go (the newest two stay) and a full card frees them before the check.
        newest = list_dumps(cfg.dumps_dir)[-1:]
        if not clock_synced(run):
            warnings.append("clock_unsynced")
        elif newest and newest[0][0] > now + CLOCK_SLACK:
            warnings.append("clock_backwards")
        else:
            trusted = True
            deleted += _rotate(cfg.dumps_dir, now, warnings)
        bindir = find_bindir(cfg.pg_bindir)
        status["client_version"] = client_version(bindir, run)
        status["free_mb"] = check_space(cfg.dumps_dir, cfg.min_free_mb * MIB + 2 * last_size) // MIB
        target = cfg.dumps_dir / dump_name(now)
        partial = cfg.dumps_dir / f".{target.name}.partial"
        while True:
            attempts += 1
            try:
                run_pg_dump(cfg, bindir, partial, run)
                status["server_version"] = verify(bindir, partial, run)
                with open(partial, "rb") as f:
                    os.fsync(f.fileno())
                os.replace(partial, target)
                _fsync_dir(cfg.dumps_dir)
                break
            except BackupError as e:
                partial.unlink(missing_ok=True)
                if e.code not in RETRY_CODES or attempts >= cfg.attempts:
                    raise
                log.warning("backup attempt failed", extra={"error": e.code, "attempt": attempts})
                sleep(cfg.retry_delay_s)
        status["size"] = target.stat().st_size
        if last_size and status["size"] < last_size / 2:
            warnings.append("shrank")
            status["shrank_pct"] = round(100 * (1 - status["size"] / last_size))
            status["shrank_since"] = previous.get("last_ok_at")
        if trusted:     # the new one is the newest of the two that always stay
            deleted += _rotate(cfg.dumps_dir, now, warnings)
    except BackupError as e:
        failure = e
    except OSError as e:     # the card: read-only, full, dying; the class and the errno, never the path
        failure = BackupError(_os_code(e), None, f"{type(e).__name__} errno={errno.errorcode.get(e.errno, e.errno)}")
    elapsed = time.monotonic() - began
    finished = now + timedelta(seconds=elapsed)
    try:
        dumps = list_dumps(cfg.dumps_dir)
    except OSError:
        dumps = []
    status.update(running=False, attempts=attempts, finished_at=_iso(finished), duration_s=round(elapsed, 1),
                  kept=len(dumps), deleted=deleted, oldest_dump_at=_iso(dumps[0][0]) if dumps else None,
                  rotated=trusted and "rotation_failed" not in warnings, warnings=warnings)
    if failure is None:
        name = target.name
        status.update(ok=True, error=None, file=name, failed_nights=0, last_ok_at=_iso(finished),
                      last_ok_file=name, last_ok_size=status["size"])
        cfg.error_file.unlink(missing_ok=True)
        log.info("backup done", extra={"file": name, "size": status["size"], "duration_s": status["duration_s"],
                                       "attempts": attempts, "kept": status["kept"], "deleted": status["deleted"],
                                       "warnings": warnings})
    else:
        # The distinct local days with a failed run since the last good dump: a second failure the same
        # day (the 13:30 slot, a run by hand) is the same night.
        today = finished.astimezone(cfg.tz).date()
        try:
            last_failed = datetime.fromisoformat(previous["finished_at"]).astimezone(cfg.tz).date() \
                if previous.get("ok") is False else None
        except (KeyError, TypeError, ValueError):
            last_failed = None
        nights = int(previous.get("failed_nights") or 0) + (0 if last_failed == today else 1)
        status.update(ok=False, error=failure.code, failed_nights=nights)
        try:
            if failure.stderr:
                _write_error(cfg.error_file, failure.stderr)
            else:
                cfg.error_file.unlink(missing_ok=True)
        except OSError:     # the status below still says what failed
            pass
        log.error("backup failed", extra={"error": failure.code, "returncode": failure.returncode,
                                          "attempt": attempts})
    write_status(cfg.status_file, status)
    return status


def _load_optouts(path: Path | None) -> list[int]:
    """The ids the watchdog recorded as opted out (a JSON list), read before anything is restored."""
    if path is None:
        log.warning("restore without opt-outs: whoever used /forget_me since this copy was made is back")
        return []
    try:
        ids = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BackupError("optouts_unreadable") from None
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise BackupError("optouts_unreadable")
    return sorted(set(ids))


def _settings() -> tuple[str, int]:
    """DATABASE_URL and the bot's id (the number before the colon in BOT_TOKEN), from the environment or
    /opt/nestor/.env, as the collector's unit has them: the restore and `forget` run by hand."""
    from dotenv import dotenv_values

    fallback = dotenv_values(ENV_FILE)

    def setting(name: str) -> str:
        return os.environ.get(name) or fallback.get(name) or ""

    if not setting("DATABASE_URL"):
        raise SystemExit("missing DATABASE_URL: in the environment or in " + ENV_FILE)
    try:
        bot_id = int(setting("BOT_TOKEN").split(":", 1)[0])
    except ValueError:
        raise SystemExit("BOT_TOKEN is missing or not a bot token: in the environment or in " + ENV_FILE) from None
    return setting("DATABASE_URL"), bot_id


async def restore(source: BinaryIO, database: str, *, optouts: Path | None) -> dict:
    """Restores the dump on `source` (a file or a pipe: it needs a file descriptor) into the empty
    `database`, then migrates, replays the opt-outs and purges. Counts only. A database that already holds
    the archive is refused: nothing is overwritten. DATABASE_URL and BOT_TOKEN come from the environment or
    /opt/nestor/.env, as the collector's unit has them.

    With the clocks of the Pi and the router apart, the purge is left out ("purge_refused" in the
    result) and the collector's nightly run does it once they agree. A failure after the copy is
    loaded is BackupError(loaded=True): that database is not empty any more."""
    import asyncpg

    from . import forget, retention
    from .bootstrap import with_database
    from .collector import store
    from .config import _env
    from .migrate import migrate

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", database):
        raise BackupError("bad_database")
    dsn, bot_id = _settings()
    ids = [i for i in _load_optouts(optouts) if i not in forget.SHARED_IDS]     # not one person: never forgotten
    bindir = find_bindir(Path(_env("BACKUP_PG_BINDIR")) if _env("BACKUP_PG_BINDIR") else None)
    url = with_database(dsn, database)
    try:
        conn = await asyncpg.connect(url, timeout=10)
    except asyncpg.InvalidCatalogNameError:
        raise BackupError("no_database") from None
    try:
        if await conn.fetchval("SELECT to_regclass('public.schema_migrations') IS NOT NULL"):
            raise BackupError("not_empty")
    finally:
        await conn.close()

    env = {**os.environ, "PGCONNECT_TIMEOUT": "10", "PGAPPNAME": "nestor-restore"}
    if _password_of(url) is not None:
        env["PGPASSWORD"] = _password_of(url)           # the child's environment only, never its arguments
    argv = [str(bindir / "pg_restore"), f"--dbname={_without_password(url)}", "--no-password", "--exit-on-error",
            "--single-transaction"]
    done = await asyncio.to_thread(subprocess.run, argv, env=env, stdin=source, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True, errors="replace")
    if done.returncode != 0:
        # Its stderr can quote the rows it choked on: the code and the number of lines are all that is shown.
        role_missing = re.search(r'role "[^"]*" does not exist', done.stderr)
        raise BackupError("role_missing" if role_missing else classify(done.stderr), done.returncode, done.stderr)

    # From here on the copy is in the database, and a second restore would refuse it: a failure says so.
    try:
        # One connection without the pool's 30 s per query, as nestor.migrate has it: a backfill takes
        # what it takes. The migrations come first: forget() is written for the schema of this code, and
        # the copy can be weeks older than the newest migration.
        conn = await asyncpg.connect(url, timeout=10)
        try:
            await store.init_connection(conn)
            await migrate(conn)
            for user_id in ids:
                async with conn.transaction():
                    await forget.forget(conn, user_id, bot_id)
            db_now, cutoff = await retention.db_times(conn)
            # The clocks are checked as on any night: a new router's clock can be anywhere, and a cutoff
            # in the future would empty the archive. The volume guard is not: what the cutoff reaches
            # should have left before the dump was made.
            try:
                await retention.check_guards(conn, retention.utcnow(), db_now, cutoff)
                refused = None
            except retention.RetentionRefused as e:
                refused = e.code
                log.warning("restore not purged", extra={"error": e.code})
        finally:
            await conn.close()
        pool = await store.create_pool(url)
        try:
            purged = None if refused else (await retention.purge(pool, cutoff, pause_s=0)).get("messages", 0)
            async with pool.acquire() as conn:
                counts = dict(await conn.fetchrow(
                    """SELECT (SELECT count(*) FROM messages) AS messages, (SELECT count(*) FROM users) AS users,
                              (SELECT count(*) FROM chats) AS chats, (SELECT count(*) FROM bot_requests) AS bot_requests,
                              (SELECT max(version) FROM schema_migrations) AS last_migration"""))
        finally:
            await pool.close()
    except Exception as e:  # the class only: its text may hold a row
        raise BackupError(type(e).__name__, loaded=True) from e
    # Not purged: the collector's nightly run purges once the clocks agree, with the same guards.
    return {**counts, "forgotten": len(ids), "purged": purged} | ({"purge_refused": refused} if refused else {})


async def forget_since(since: datetime) -> list[int]:
    """After a restore and the import that follows it. Whoever used /forget_me after the watchdog last wrote
    optouts.json is in neither it nor the copy, and the import has just brought their messages back, with
    the command itself. Every sender of /forget_me since `since` (when the copy was made) is forgotten
    again: forgetting someone twice changes nothing. Telegram holds an update for up to a day, so a command
    the bot carried out after the copy may have been sent a day before it. The archive does not know the
    bot's name: a /forget_me@another_bot counts too. Returns their ids."""
    import asyncpg

    from . import forget

    dsn, bot_id = _settings()
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        ids = [r["user_id"] for r in await conn.fetch(
            """SELECT DISTINCT user_id FROM messages
               WHERE sent_at >= $1 AND text ~* '^\\s*/forget_me(@\\w+)?(\\s|$)' AND user_id <> ALL($2::bigint[])
               ORDER BY user_id""", since - timedelta(days=1), sorted(forget.SHARED_IDS))]
        for user_id in ids:
            async with conn.transaction():
                await forget.forget(conn, user_id, bot_id)
    finally:
        await conn.close()
    return ids


def _copy_time(value: str) -> datetime:
    """--since: 20261005T013000Z as in the copy's file name (the whole name will do), or an ISO time with a zone."""
    match = DUMP_RE.match(value)
    when = datetime.fromisoformat(match.group(1) if match else value)
    if when.tzinfo is None:
        raise ValueError("no time zone")
    return when


def main(argv: list[str] | None = None) -> int:
    from .config import _env
    from .logs import setup_logging

    parser = argparse.ArgumentParser(prog="python -m nestor.backup", description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true", help="dump now, even if tonight's backup is done")
    commands = parser.add_subparsers(dest="command")
    back = commands.add_parser("restore", help="restore the dump on stdin into an empty database (as user nestor)")
    back.add_argument("--database", required=True, help="the empty database bootstrap created")
    back.add_argument("--optouts", type=Path, metavar="PATH",
                      help="the watchdog's optouts.json: every id in it is forgotten again")
    again = commands.add_parser("forget", help="after the restore and the import: forget again whoever sent "
                                               "/forget_me since the copy was made (as user nestor)")
    again.add_argument("--since", required=True, type=_copy_time, metavar="TIME",
                       help="when the restored copy was made, as in its file name: 20261005T013000Z")
    args = parser.parse_args(argv)
    setup_logging(_env("LOG_LEVEL", "INFO"))

    if args.command == "restore":
        if sys.stdin.buffer.isatty():      # pg_restore would wait for a dump typed at the terminal
            print("restore failed no_dump_on_stdin")
            return 1
        try:
            done = asyncio.run(restore(sys.stdin.buffer, args.database, optouts=args.optouts))
        except BackupError as e:
            print(f"restore failed {e.code}" + (f" ({e.lines} lines of stderr)" if e.stderr else "")
                  + (": the copy is in the database, unfinished; drop it, run bootstrap, restore again"
                     if e.loaded else ""))
            return 1
        except Exception as e:  # noqa: BLE001 - the class only: its text may hold a row
            print(f"restore failed {type(e).__name__}")
            return 1
        print("restore done", json.dumps(done, ensure_ascii=False))
        return 0
    if args.command == "forget":
        try:
            ids = asyncio.run(forget_since(args.since))
        except Exception as e:  # noqa: BLE001 - the class only: its text may hold a row
            print(f"forget failed {type(e).__name__}")
            return 1
        print("forgotten", len(ids))
        return 0

    try:
        status = backup(load_config(), force=args.force)
    except BackupBusy:
        log.warning("backup already running")
        return EX_TEMPFAIL
    except OSError as e:     # the lock or the final status: nothing can be recorded on this card
        log.error("backup status not written", extra={"error": _os_code(e)})
        return EX_IOERR
    return 0 if status is None or status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
