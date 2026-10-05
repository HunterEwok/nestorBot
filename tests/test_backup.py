"""Stage 4 backups: the nightly pg_dump to the card (rotation, verification, the status the watchdog reads,
what never reaches a log) and the restore after a disaster (opt-outs replayed, expired rows purged).

The first half needs no database: pg_dump and pg_restore are replaced by a stand-in or by a script. The
second half runs the real binaries against the throwaway cluster, as the unit would."""
import asyncio
import errno
import fcntl
import json
import logging
import os
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from nestor import backup as bk
from nestor import forget
from nestor.bootstrap import (
    bootstrap,
    ensure_backup_role,
    scram_verifier,
    with_database,
)
from nestor.migrate import migrate, migration_files
from nestor.promises import BACKUP_KEEP_LAST, BACKUP_MAX_AGE_DAYS

from .conftest import ADMIN_URL, DB
from .factories import CHAT_ID
from .test_answerer import BOT_ID

MSK = ZoneInfo("Europe/Moscow")
NIGHT = datetime(2026, 10, 5, 4, 30, 5, tzinfo=MSK)        # a Monday: the 04:30 run, a few seconds late
CANARY = "CANARY-7f3a секретный текст"


@pytest.fixture(autouse=True)
def no_timesync_flag(monkeypatch):
    """Tools(synced=...) alone says whether the clock is synced, whatever this machine's timesyncd says."""
    monkeypatch.setattr(bk, "TIMESYNC_FLAG", Path("/nonexistent/timesync/synchronized"))


def utc(day: int, hour: int = 1, minute: int = 30, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def nights(count: int, last: datetime) -> list[datetime]:
    """One dump a night, ending at `last`."""
    return [last - timedelta(days=n) for n in range(count - 1, -1, -1)]


# --- the names, the rotation, the schedule ---------------------------------------------------------------

def test_dump_names_round_trip_and_foreign_files_are_ignored(tmp_path):
    assert bk.dump_name(NIGHT) == "nestor-20261005T013005Z.dump"          # 04:30 Moscow is 01:30 UTC
    for name in ("nestor-20261003T013000Z.dump", "nestor-20261005T013000Z.dump"):
        (tmp_path / name).write_bytes(b"x")
    for name in (".nestor-20261006T013000Z.dump.partial", "nestor-20261006T013000Z.dump.bak", "nestor-2026.dump",
                 "status.json", "xnestor-20261006T013000Z.dump"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "nestor-20261007T013000Z.dump").mkdir()                   # a directory is not a dump
    assert [(at, p.name) for at, p in bk.list_dumps(tmp_path)] == [
        (utc(3), "nestor-20261003T013000Z.dump"), (utc(5), "nestor-20261005T013000Z.dump")]
    assert bk.list_dumps(tmp_path / "missing") == []
    assert bk.dump_name(utc(5)) == "nestor-20261005T013000Z.dump" and bk.DUMP_RE.match(bk.dump_name(NIGHT))


def test_rotation_keeps_seven_daily_and_four_weekly():
    last = utc(7)                                                          # a Wednesday
    assert last.weekday() == 2
    kept = bk.plan_rotation(nights(60, last), last)
    daily = {last - timedelta(days=d) for d in range(7)}
    # Of the weeks before this one, the newest dump that is older than those 7 days: last Wednesday, then
    # the Sundays. Not last week's Sunday, which the daily rule already keeps.
    weekly = {last - timedelta(days=d) for d in (7, 10, 17, 24)}
    assert kept == daily | weekly and len(kept) == 11


def test_rotation_keeps_the_newest_dump_of_a_day_with_manual_runs():
    last = utc(7, 1, 30)
    taken = [last - timedelta(days=n) + timedelta(hours=h, minutes=m) for n in range(10) for h, m in ((0, 0), (8, 30), (13, 30))]
    kept = bk.plan_rotation(taken, utc(7, 15, 0), keep_last=1)        # the night's 04:30, a run at 13:00, one at 18:30 Moscow
    for day in range(1, 8):                                          # the 7 daily days
        assert utc(day, 15, 0) in kept and utc(day, 1, 30) not in kept and utc(day, 10, 0) not in kept
    assert utc(30, 15, 0, month=9) in kept and utc(30, 10, 0, month=9) not in kept     # a week's newest, too
    assert len(kept) == 8


@pytest.mark.parametrize("outages", [False, True])
def test_a_year_of_nights_keeps_at_most_11_and_forgets_within_29_days(outages):
    down = {n + k for n in range(10, 400, 23) for k in range(3)} if outages else set()
    taken: list[datetime] = []
    first = utc(1, month=1)
    for n in range(400):
        now = first + timedelta(days=n)
        # A night without a new dump still rotates: what expired goes, the newest two stay.
        taken = sorted(bk.plan_rotation(taken if n in down else [*taken, now], now))
        assert len(taken) <= 11
        assert now - taken[0] <= timedelta(days=BACKUP_MAX_AGE_DAYS)
        if not outages and n >= 40:
            assert len(taken) == 11 and now - taken[0] < timedelta(days=29)
    assert BACKUP_MAX_AGE_DAYS == 35


def test_forty_failed_nights_keep_the_last_two_good_dumps():
    last_good = utc(1)
    good = nights(30, last_good)
    for failed in (36, 40, 400):                       # nights without a new dump: everything is old, two stay
        assert bk.plan_rotation(good, last_good + timedelta(days=failed)) == set(good[-2:])
    assert bk.plan_rotation(good, last_good + timedelta(days=3)) != set(good[-2:])      # still inside 35 days
    assert bk.plan_rotation([], last_good) == set() and BACKUP_KEEP_LAST == 2
    assert bk.plan_rotation(good[-1:], last_good + timedelta(days=90)) == set(good[-1:])   # one is all there is


def test_due_runs_once_per_slot_and_catches_up():
    def at(day, hour, minute=0):
        return datetime(2026, 10, day, hour, minute, tzinfo=MSK)

    def ok(when):
        return {"last_ok_at": when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}

    slot = dtime(4, 30)
    assert bk.due(None, at(5, 4, 30), MSK, slot) and bk.due({}, at(5, 4, 30), MSK, slot)
    assert bk.due({"last_ok_at": "garbage"}, at(5, 4, 30), MSK, slot)
    night = ok(at(4, 4, 36))
    assert bk.due(night, at(5, 4, 30), MSK, slot)                  # tonight's 04:30: yesterday's dump is not tonight's
    assert not bk.due(night, at(4, 13, 30), MSK, slot)             # ... and the catch-up the day before found it done
    assert not bk.due(night, at(5, 2, 0), MSK, slot)               # before the slot: yesterday's slot, done
    failed = ok(at(3, 4, 36))
    assert bk.due(failed, at(4, 13, 30), MSK, slot)                # the night failed: the 13:30 run goes
    assert not bk.due(ok(at(5, 4, 36)), at(5, 13, 30), MSK, slot)  # the night worked: it skips
    manual = ok(at(5, 15, 0))
    assert not bk.due(manual, at(5, 18, 0), MSK, slot)             # a manual run counts for today ...
    assert bk.due(manual, at(6, 4, 30), MSK, slot)                 # ... and does not move tomorrow's slot


def test_classify_pg_dump_errors():
    strings = {
        'pg_dump: error: connection to server at "192.168.0.1", port 5432 failed: Connection refused\n\tIs the server '
        'running on that host and accepting TCP/IP connections?': "unreachable",
        'pg_dump: error: connection to server at "192.168.0.1", port 5432 failed: timeout expired': "unreachable",
        'pg_dump: error: connection to server at "192.168.0.1", port 5432 failed: No route to host': "unreachable",
        'pg_dump: error: could not translate host name "router" to address: Name or service not known': "unreachable",
        "pg_dump: error: query failed: server closed the connection unexpectedly": "unreachable",
        'pg_dump: error: connection to server at "192.168.0.1", port 5432 failed: FATAL:  too many connections '
        'for role "nestor_backup"': "too_many_connections",
        'pg_dump: error: connection to server at "192.168.0.1", port 5432 failed: FATAL:  the database system is '
        'starting up': "starting_up",
        "FATAL:  the database system is shutting down": "starting_up",
        'pg_dump: error: connection to server at "192.168.0.1", port 5432 failed: FATAL:  password authentication '
        'failed for user "nestor_backup"': "auth_failed",
        "pg_dump: error: connection to server failed: fe_sendauth: no password supplied": "auth_failed",
        'pg_dump: error: connection to server at "192.168.0.1", port 5432 failed: FATAL:  no pg_hba.conf entry for '
        'host "192.168.0.2", user "nestor_backup", database "nestor", no encryption': "hba_rejected",
        "pg_dump: error: aborting because of server version mismatch\npg_dump: detail: server version: 18.1; "
        "pg_dump version: 17.6": "version_mismatch",
        "pg_dump: error: query failed: ERROR:  permission denied for table messages": "permission_denied",
        'pg_dump: error: could not obtain lock on relation "public.messages"': "lock_timeout",
        "pg_dump: error: query failed: ERROR:  canceling statement due to lock timeout": "lock_timeout",
        'pg_dump: error: could not open output file "/var/lib/nestor-backup/dumps/x": Read-only file system':
            "disk_error",
        "pg_dump: error: could not write to output file: Input/output error": "disk_error",     # not "no_space"
        "pg_dump: error: could not write to output file: No space left on device": "no_space",
        "pg_dump: error: could not write to output file: something else": "no_space",
        "pg_dump: error: this build does not support compression with ZSTD": "compress_unsupported",
        "pg_dump: error: something nobody has seen": "failed",
        "": "failed",
    }
    for stderr, code in strings.items():
        assert bk.classify(stderr) == code, stderr
    assert {"timeout"} <= bk.RETRY_CODES and "auth_failed" not in bk.RETRY_CODES and "disk_error" not in bk.RETRY_CODES


# --- the settings -----------------------------------------------------------------------------------------

NAMES = ("BACKUP_DATABASE_URL", "TIMEZONE", "BACKUP_AT", "BACKUP_PG_BINDIR", "BACKUP_COMPRESS", "BACKUP_MIN_FREE_MB",
         "BACKUP_TIMEOUT_MIN", "BACKUP_ATTEMPTS", "BACKUP_RETRY_MIN")
URL = "postgresql://nestor_backup@192.168.0.1:5432/nestor?sslmode=prefer&connect_timeout=10"


@pytest.fixture
def env(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_config_defaults_and_trailing_comments(env):
    env.setenv("BACKUP_DATABASE_URL", URL + "   # без пароля: он в pgpass")
    cfg = bk.load_config()
    assert cfg == bk.BackupConfig(database_url=URL, tz=MSK)
    assert (cfg.at, cfg.compress, cfg.min_free_mb, cfg.timeout_s, cfg.attempts, cfg.retry_delay_s, cfg.pg_bindir) == (
        dtime(4, 30), "zstd:3", 1024, 600, 6, 300, None)
    assert cfg.dumps_dir == Path("/var/lib/nestor-backup/dumps")
    assert cfg.status_file == Path("/var/lib/nestor-backup/status.json")
    assert cfg.error_file == Path("/var/lib/nestor-backup/last-error.txt")
    assert cfg.lock_file == Path("/var/lib/nestor-backup/lock")
    env.setenv("TIMEZONE", "Europe/Tallinn   # время сервера")
    env.setenv("BACKUP_AT", "05:15   # слот")
    env.setenv("BACKUP_PG_BINDIR", "/usr/lib/postgresql/18/bin   # клиент 18")
    env.setenv("BACKUP_COMPRESS", "gzip:6   # без zstd")
    env.setenv("BACKUP_MIN_FREE_MB", "2048")
    env.setenv("BACKUP_TIMEOUT_MIN", "20")
    env.setenv("BACKUP_ATTEMPTS", "3")
    env.setenv("BACKUP_RETRY_MIN", "1   # минута")
    cfg = bk.load_config()
    assert (cfg.tz, cfg.at, cfg.pg_bindir, cfg.compress, cfg.min_free_mb, cfg.timeout_s, cfg.attempts,
            cfg.retry_delay_s) == (ZoneInfo("Europe/Tallinn"), dtime(5, 15), Path("/usr/lib/postgresql/18/bin"),
                                   "gzip:6", 2048, 1200, 3, 60)
    env.setenv("BACKUP_AT", "в полпятого")
    with pytest.raises(SystemExit, match="BACKUP_AT must look like 21:00"):
        bk.load_config()
    env.setenv("BACKUP_AT", "04:30")
    env.setenv("BACKUP_ATTEMPTS", "0")
    with pytest.raises(SystemExit, match="BACKUP_ATTEMPTS must be at least 1"):
        bk.load_config()
    env.setenv("BACKUP_ATTEMPTS", "6")
    env.setenv("BACKUP_COMPRESS", "zstd:3 --evil")
    with pytest.raises(SystemExit, match="BACKUP_COMPRESS"):
        bk.load_config()
    env.delenv("BACKUP_DATABASE_URL")
    env.setenv("BACKUP_COMPRESS", "zstd:3")
    with pytest.raises(SystemExit, match="missing required environment variable BACKUP_DATABASE_URL"):
        bk.load_config()


def test_url_with_password_is_refused(env):
    for url in ("postgresql://nestor_backup:hunter2@192.168.0.1:5432/nestor",
                "postgresql://nestor_backup@192.168.0.1:5432/nestor?sslmode=prefer&password=hunter2",
                "postgres://nestor_backup:@192.168.0.1/nestor"):
        env.setenv("BACKUP_DATABASE_URL", url)
        with pytest.raises(SystemExit) as e:
            bk.load_config()
        assert "must not contain a password" in str(e.value) and "hunter2" not in str(e.value)
    env.setenv("BACKUP_DATABASE_URL", "host=192.168.0.1 dbname=nestor password=hunter2")
    with pytest.raises(SystemExit) as e:
        bk.load_config()
    assert "postgresql:// URL" in str(e.value) and "hunter2" not in str(e.value)
    assert bk._without_password("postgresql://nestor:p%40ss@db:5432/x?sslmode=require&password=p%40ss") == (
        "postgresql://nestor@db:5432/x?sslmode=require")
    assert bk._password_of("postgresql://nestor:p%40ss@db:5432/x") == "p@ss" and bk._password_of(URL) is None


# --- the unit files agree with the code ---------------------------------------------------------------------------

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "systemd"


def _env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE as systemd's EnvironmentFile reads it: lines starting with # or ; are comments."""
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and line[0] not in "#;")


def test_the_unit_files_agree_with_the_code(env):
    example = _env_file(DEPLOY / "nestor-backup.env.example")
    assert set(example) == {"BACKUP_DATABASE_URL", "TIMEZONE", "BACKUP_AT"}      # the rest is optional, commented out
    for name, value in example.items():
        env.setenv(name, value)
    cfg = bk.load_config()                                      # no password in it, and every setting parses
    assert cfg == bk.BackupConfig(database_url=example["BACKUP_DATABASE_URL"], tz=MSK)
    assert cfg.database_url.startswith("postgresql://nestor_backup@") and "sslmode=prefer" in cfg.database_url
    service = (DEPLOY / "nestor-backup.service").read_text()
    assert "ExecStart=/opt/nestor/.venv/bin/python -m nestor.backup\n" in service
    assert "User=nestor-backup" in service and "EnvironmentFile=/etc/nestor-backup/backup.env" in service
    assert "Environment=PGPASSFILE=/etc/nestor-backup/pgpass" in service and "UMask=0077" in service
    assert f"StateDirectory={cfg.state_dir.name}\n" in service and cfg.state_dir == Path("/var/lib/nestor-backup")
    timer = (DEPLOY / "nestor-backup.timer").read_text()
    # The first slot is BACKUP_AT; the second is the catch-up that `due` turns into a no-op after a good night.
    assert f"OnCalendar=*-*-* {cfg.at:%H:%M}:00 Europe/Moscow\n" in timer and "Persistent=true" in timer
    assert "OnCalendar=*-*-* 13:30:00 Europe/Moscow" in timer and "Europe/Moscow" in example["TIMEZONE"]


# --- the pieces: client, space, status, clock ------------------------------------------------------------

def _tools_dir(path: Path, *majors: str) -> Path:
    for major in majors:
        bindir = path / major / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        for tool in ("pg_dump", "pg_restore"):
            (bindir / tool).write_text("#!/bin/sh\n")
            (bindir / tool).chmod(0o755)
    return path


def test_the_newest_installed_client_is_found_by_its_number(tmp_path):
    root = _tools_dir(tmp_path / "lib", "9", "16", "18")
    (root / "17" / "bin").mkdir(parents=True)                                    # a major without the tools
    (root / "17" / "bin" / "pg_dump").write_text("#!/bin/sh\n")
    (root / "17" / "bin" / "pg_dump").chmod(0o755)
    (root / "19.bak").mkdir()
    assert bk.find_bindir(None, root) == root / "18" / "bin"                    # 18 before 9: by number, not by name
    assert bk.find_bindir(root / "16" / "bin", root) == root / "16" / "bin"     # configured wins
    with pytest.raises(bk.BackupError) as e:
        bk.find_bindir(root / "17" / "bin", root)                               # configured, but not usable
    assert e.value.code == "client_missing"
    with pytest.raises(bk.BackupError) as e:
        bk.find_bindir(None, tmp_path / "nothing")
    assert e.value.code == "client_missing"


def test_client_version_is_read_from_pg_dump(tmp_path):
    def says(out, rc=0):
        return lambda argv, **kw: subprocess.CompletedProcess(argv, rc, out, "")

    assert bk.client_version(tmp_path, says("pg_dump (PostgreSQL) 18.6 (Debian 18.6-1.pgdg130+1)\n")) == "18.6"
    assert bk.client_version(tmp_path, says("pg_dump (PostgreSQL) 16.14 (Ubuntu 16.14-0ubuntu0.24.04.1)\n")) == "16.14"
    for bad in (says("not a version"), says("pg_dump (PostgreSQL) 18.6\n", rc=1)):
        with pytest.raises(bk.BackupError) as e:
            bk.client_version(tmp_path, bad)
        assert e.value.code == "client_missing"


def test_low_space_is_checked_before_the_dump(tmp_path):
    assert bk.check_space(tmp_path, 1) > 1
    with pytest.raises(bk.BackupError) as e:
        bk.check_space(tmp_path, 1 << 62)
    assert e.value.code == "low_space"


def test_the_unit_needs_no_third_party_package_to_start():
    """The backup user imports nestor.backup before anything else is installed for it to use."""
    code = "import sys, nestor.backup; print(sorted({'asyncpg', 'dotenv', 'aiogram'} & set(sys.modules)))"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[]"


def test_the_status_is_written_whole_and_readable_by_others(tmp_path):
    path = tmp_path / "status.json"
    assert bk.load_status(path) is None
    old = os.umask(0o077)                                    # the unit's UMask: the watchdog is another user
    try:
        bk.write_status(path, {"version": 1, "error": "довод"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 and bk.load_status(path) == {"version": 1, "error": "довод"}
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]          # no temporary file left
    path.write_text("{cut off")
    assert bk.load_status(path) is None
    path.write_text("[1, 2]")
    assert bk.load_status(path) is None


def test_clock_is_synced_by_the_flag_or_by_timedatectl(tmp_path, monkeypatch):
    flag = tmp_path / "synchronized"
    monkeypatch.setattr(bk, "TIMESYNC_FLAG", flag)

    def timedatectl(out="yes\n", rc=0):
        return lambda argv, **kw: subprocess.CompletedProcess(argv, rc, out, "")

    def missing(argv, **kw):
        raise FileNotFoundError("timedatectl")

    assert not bk.clock_synced(timedatectl("no\n")) and not bk.clock_synced(missing)
    assert not bk.clock_synced(timedatectl("yes\n", rc=1))
    assert bk.clock_synced(timedatectl("yes\n"))
    flag.touch()
    assert bk.clock_synced(missing)


def test_scram_verifier_format():
    # RFC 7677's example: password "pencil", salt W22ZaJ0SNY7soEsUEjb6gQ==, 4096 rounds. The message the client
    # proves with (p=...) and the server proves with (v=...) are both checked against what the verifier holds.
    import base64
    import hashlib
    import hmac

    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    verifier = scram_verifier("pencil", salt=salt)
    head, rest = verifier.split("$", 1)
    params, keys = rest.split("$")
    assert (head, params.split(":")[0], params.split(":")[1]) == ("SCRAM-SHA-256", "4096", "W22ZaJ0SNY7soEsUEjb6gQ==")
    stored, server = (base64.b64decode(k) for k in keys.split(":"))
    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth = (f"n=user,r=rOprNGfwEbeRWgbNEkqO,r={nonce},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,c=biws,r={nonce}").encode()
    signature = hmac.digest(stored, auth, "sha256")
    # ClientKey = ClientProof xor ClientSignature must hash to StoredKey
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    assert hashlib.sha256(bytes(a ^ b for a, b in zip(proof, signature, strict=True))).digest() == stored
    assert base64.b64encode(hmac.digest(server, auth, "sha256")).decode() == "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    # Fresh salt each time, the rounds as asked, and other spellings of the same password agree.
    assert scram_verifier("x") != scram_verifier("x")
    assert scram_verifier("x", iterations=8192).startswith("SCRAM-SHA-256$8192:")
    assert scram_verifier("a b­c", salt=salt) == scram_verifier("a bc", salt=salt)
    assert scram_verifier("ﬁ", salt=salt) == scram_verifier("fi", salt=salt)
    assert scram_verifier("\u00ad", salt=salt) != scram_verifier("", salt=salt)     # nothing left: hashed as it is


# --- a run, with pg_dump and pg_restore stood in for --------------------------------------------------------

TOC = "\n".join([";", ";     Dumped from database version: 18.6 (Debian 18.6-1.pgdg130+1)", ";",
                 *(f"{i}; 0 1{i} TABLE DATA {t} nestor" for i, t in enumerate(bk.REQUIRED_DATA))]) + "\n"


class Tools:
    """What `run` sees: pg_dump writes `size` bytes where it is told to, or fails as scripted; pg_restore lists a
    table of contents and reads it back; timedatectl says whether the clock is synced."""

    def __init__(self, *failures: str | BaseException, size: int = 1000, synced: bool = True, toc: str = TOC):
        self.failures, self.size, self.synced, self.toc = list(failures), size, synced, toc
        self.dumps = 0
        self.statuses: list[dict] = []          # what status.json said when each pg_dump started

    def __call__(self, argv, **kw):
        tool = Path(argv[0]).name
        if tool == "timedatectl":
            return subprocess.CompletedProcess(argv, 0, "yes\n" if self.synced else "no\n", "")
        if argv[1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "pg_dump (PostgreSQL) 18.6 (Debian 18.6-1.pgdg130+1)\n", "")
        if tool == "pg_restore":
            return subprocess.CompletedProcess(argv, 0, self.toc if argv[1] == "--list" else "", "")
        self.dumps += 1
        target = Path(argv[argv.index("-f") + 1])
        status = target.parent.parent / "status.json"
        self.statuses.append(json.loads(status.read_text()) if status.exists() else {})
        failure = self.failures.pop(0) if self.failures else None
        if isinstance(failure, BaseException):
            raise failure
        if failure:
            return subprocess.CompletedProcess(argv, 1, "", failure)
        target.write_bytes(b"x" * self.size)
        return subprocess.CompletedProcess(argv, 0, "", "")


def cfg_in(tmp_path: Path, **kw) -> bk.BackupConfig:
    return bk.BackupConfig(database_url=URL, tz=MSK, state_dir=tmp_path / "state",
                           pg_bindir=_tools_dir(tmp_path / "lib", "18") / "18" / "bin",
                           **{"min_free_mb": 1, "retry_delay_s": 0, **kw})


def status_of(cfg: bk.BackupConfig) -> dict:
    return json.loads(cfg.status_file.read_text())


def old_dumps(cfg: bk.BackupConfig, times: list[datetime]) -> None:
    cfg.dumps_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for t in times:
        (cfg.dumps_dir / bk.dump_name(t)).write_bytes(b"old")


def test_a_good_night_makes_a_dump_and_a_status(tmp_path):
    cfg, tools = cfg_in(tmp_path), Tools(size=4321)
    status = bk.backup(cfg, NIGHT, run=tools)
    assert status == status_of(cfg)
    name = "nestor-20261005T013005Z.dump"
    assert [p.name for p in cfg.dumps_dir.iterdir()] == [name] and (cfg.dumps_dir / name).stat().st_size == 4321
    assert status["running"] is False and status["ok"] is True and status["error"] is None
    assert (status["file"], status["size"], status["attempts"], status["server_version"], status["client_version"]) == (
        name, 4321, 1, "18.6", "18.6")
    assert (status["kept"], status["deleted"], status["rotated"], status["warnings"], status["failed_nights"]) == (
        1, 0, True, [], 0)
    assert (status["last_ok_file"], status["last_ok_size"], status["last_ok_at"]) == (name, 4321, status["finished_at"])
    assert status["started_at"] == "2026-10-05T01:30:05Z" and status["oldest_dump_at"] == "2026-10-05T01:30:05Z"
    assert status["free_mb"] > 0 and status["duration_s"] >= 0
    # The same night again: not due, nothing runs. By hand with --force it does.
    assert bk.backup(cfg, NIGHT + timedelta(hours=9), run=tools) is None and tools.dumps == 1
    assert bk.backup(cfg, NIGHT + timedelta(hours=10), force=True, run=tools)["ok"] and tools.dumps == 2


def test_status_contract_keys(tmp_path, monkeypatch):
    cfg = cfg_in(tmp_path)
    keys = {"version", "running", "ok", "error", "attempts", "started_at", "finished_at", "file", "size", "duration_s",
            "server_version", "client_version", "free_mb", "kept", "deleted", "oldest_dump_at", "rotated", "warnings",
            "failed_nights", "last_ok_at", "last_ok_file", "last_ok_size"}
    assert set(bk.STATUS_KEYS) == keys
    good = bk.backup(cfg, NIGHT, run=Tools(size=1000))
    assert set(good) == keys and set(status_of(cfg)) == keys
    failed = bk.backup(cfg, NIGHT + timedelta(days=1), run=Tools("pg_dump: error: password authentication failed"))
    assert set(failed) == keys and set(status_of(cfg)) == keys
    assert failed["version"] == good["version"] == 1
    # `shrank_pct` and `shrank_since` (when the copy it is compared with was made) exist only beside the warning.
    shrank = bk.backup(cfg, NIGHT + timedelta(days=2), run=Tools(size=300))
    assert shrank["warnings"] == ["shrank"] and shrank["shrank_pct"] == 70 and set(shrank) == keys | set(bk.SHRANK_KEYS)
    assert shrank["shrank_since"] == good["last_ok_at"]
    assert not set(bk.SHRANK_KEYS) & set(bk.backup(cfg, NIGHT + timedelta(days=3), run=Tools(size=300)))


def test_running_status_carries_the_last_verdict_so_a_retry_does_not_flap(tmp_path):
    cfg, tools = cfg_in(tmp_path), Tools()
    first = bk.backup(cfg, NIGHT, run=tools)
    failing = Tools("pg_dump: error: password authentication failed")
    bk.backup(cfg, NIGHT + timedelta(days=1), run=failing)
    seen = Tools()
    bk.backup(cfg, NIGHT + timedelta(days=1, hours=9), run=seen)           # the 13:30 slot, after the failed night
    during = seen.statuses[0]
    assert during["running"] is True and during["started_at"] == "2026-10-06T10:30:05Z"
    assert (during["ok"], during["error"], during["failed_nights"]) == (False, "auth_failed", 1)
    assert (during["last_ok_at"], during["last_ok_file"], during["last_ok_size"]) == (
        first["last_ok_at"], first["last_ok_file"], first["last_ok_size"])
    assert status_of(cfg)["running"] is False and status_of(cfg)["ok"] is True
    # The warnings of the last run stay while the next one runs, and go with its verdict.
    shrank = bk.backup(cfg, NIGHT + timedelta(days=2), run=Tools(size=100))
    seen = Tools(size=100)
    done = bk.backup(cfg, NIGHT + timedelta(days=3), run=seen)
    during = seen.statuses[0]
    assert (during["running"], during["warnings"], during["shrank_pct"], during["shrank_since"]) == (
        True, shrank["warnings"], 90, shrank["shrank_since"])
    assert shrank["warnings"] == ["shrank"] and done["warnings"] == [] and not set(bk.SHRANK_KEYS) & set(done)


def test_a_failed_night_keeps_the_old_dumps_and_counts_distinct_days(tmp_path):
    cfg = cfg_in(tmp_path, attempts=2)
    first = bk.backup(cfg, NIGHT, run=Tools())
    sleeps: list[float] = []
    down = "pg_dump: error: connection to server at \"192.168.0.1\", port 5432 failed: Connection refused"
    # The night: two attempts, a pause between them, a status with the code only.
    tools = Tools(down, down)
    failed = bk.backup(cfg, NIGHT + timedelta(days=1), run=tools, sleep=sleeps.append)
    assert (failed["ok"], failed["error"], failed["attempts"], failed["failed_nights"], failed["running"]) == (
        False, "unreachable", 2, 1, False)
    assert sleeps == [cfg.retry_delay_s] and tools.dumps == 2
    assert (failed["last_ok_at"], failed["last_ok_file"]) == (first["last_ok_at"], first["last_ok_file"])
    assert (failed["file"], failed["size"], failed["kept"]) == (None, None, 1)
    assert [p.name for p in cfg.dumps_dir.iterdir()] == [first["file"]]            # old dump kept, no partial left
    # The catch-up the same day fails too: the same night. The next day is the second.
    assert bk.backup(cfg, NIGHT + timedelta(days=1, hours=9), run=Tools(down, down), sleep=sleeps.append)[
        "failed_nights"] == 1
    assert bk.backup(cfg, NIGHT + timedelta(days=2), run=Tools(down, down), sleep=sleeps.append)["failed_nights"] == 2
    good = bk.backup(cfg, NIGHT + timedelta(days=3), run=Tools())
    assert (good["ok"], good["failed_nights"]) == (True, 0)


def test_a_run_that_fails_early_reports_nothing_of_the_last_one_but_its_good_dump(tmp_path):
    cfg = cfg_in(tmp_path)
    good = bk.backup(cfg, NIGHT, run=Tools())
    assert good["client_version"] and good["free_mb"] and good["server_version"]
    failed = bk.backup(replace(cfg, pg_bindir=tmp_path / "uninstalled"), NIGHT + timedelta(days=1), run=Tools())
    assert (failed["ok"], failed["error"], failed["attempts"], failed["failed_nights"]) == (False, "client_missing", 0, 1)
    assert (failed["file"], failed["size"], failed["server_version"], failed["client_version"], failed["free_mb"]) == (
        None, None, None, None, None)
    assert (failed["last_ok_file"], failed["last_ok_size"], failed["kept"]) == (good["file"], good["size"], 1)


def test_a_dump_is_retried_only_for_causes_that_pass(tmp_path):
    cfg = cfg_in(tmp_path, attempts=3)
    lock = "pg_dump: error: could not obtain lock on relation \"public.messages\""
    tools, sleeps = Tools(lock, "pg_dump: error: query failed: server closed the connection unexpectedly"), []
    done = bk.backup(cfg, NIGHT, run=tools, sleep=sleeps.append)
    assert (done["ok"], done["attempts"], tools.dumps, len(sleeps)) == (True, 3, 3, 2)
    for code, stderr in (("auth_failed", "FATAL:  password authentication failed"),
                         ("compress_unsupported", "pg_dump: error: this build does not support compression with ZSTD"),
                         ("permission_denied", "ERROR:  permission denied for table messages")):
        tools = Tools(stderr, stderr, stderr)
        failed = bk.backup(cfg, NIGHT + timedelta(days=1), run=tools, force=True)
        assert (failed["error"], failed["attempts"], tools.dumps) == (code, 1, 1)
    timeouts = Tools(subprocess.TimeoutExpired("pg_dump", 600), subprocess.TimeoutExpired("pg_dump", 600),
                     subprocess.TimeoutExpired("pg_dump", 600))
    failed = bk.backup(cfg, NIGHT + timedelta(days=1), run=timeouts, force=True)
    assert (failed["error"], failed["attempts"]) == ("timeout", 3)


def test_a_dump_that_does_not_verify_is_not_kept_and_not_retried(tmp_path):
    cfg = cfg_in(tmp_path, attempts=3)
    no_messages = TOC.replace("TABLE DATA public messages", "TABLE DATA public elsewhere")
    tools = Tools(toc=no_messages)
    failed = bk.backup(cfg, NIGHT, run=tools)
    assert (failed["error"], failed["attempts"], tools.dumps, failed["kept"]) == ("verify_failed", 1, 1, 0)
    assert list(cfg.dumps_dir.iterdir()) == []                                 # the partial file is gone, nothing renamed
    assert "public messages" in cfg.error_file.read_text()                     # which table, for whoever looks
    versionless = Tools(toc=TOC.replace("Dumped from database version", "Dumped from"))
    assert bk.backup(cfg, NIGHT, run=versionless, force=True)["error"] == "verify_failed"


def test_low_space_skips_the_dump(tmp_path):
    cfg, tools = cfg_in(tmp_path, min_free_mb=1 << 40), Tools()
    failed = bk.backup(cfg, NIGHT, run=tools)
    assert (failed["error"], failed["ok"], tools.dumps, failed["attempts"]) == ("low_space", False, 0, 0)
    assert list(cfg.dumps_dir.iterdir()) == [] and not cfg.error_file.exists()


def _free(monkeypatch, free) -> None:
    """The card's free bytes as `free(dumps_dir)` says."""
    monkeypatch.setattr(bk.shutil, "disk_usage", lambda path: SimpleNamespace(total=64 << 30, used=0, free=free(path)))


def _last_dump(cfg: bk.BackupConfig, size: int) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    bk.write_status(cfg.status_file, {"version": 1, "ok": True, "last_ok_size": size})


def test_the_space_needed_is_twice_the_last_dump_on_top(tmp_path, monkeypatch):
    cfg, tools = cfg_in(tmp_path), Tools()
    _last_dump(cfg, 900 * bk.MIB)
    _free(monkeypatch, lambda path: 1200 * bk.MIB)                    # a 900 MB dump, 1.2 GB free
    failed = bk.backup(cfg, NIGHT, run=tools, force=True)
    assert (failed["error"], failed["ok"], tools.dumps, failed["attempts"]) == ("low_space", False, 0, 0)
    _free(monkeypatch, lambda path: (2 * 900 + 1) * bk.MIB)           # min_free_mb=1 on top of twice that
    assert bk.backup(cfg, NIGHT, run=tools, force=True)["ok"] and tools.dumps == 1


def test_a_full_card_lets_the_expired_copies_go_before_it_checks_the_space(tmp_path, monkeypatch):
    cfg = cfg_in(tmp_path)
    old_dumps(cfg, nights(11, utc(20, month=8)))                       # all far older than 35 days
    _last_dump(cfg, 10 * bk.MIB)
    _free(monkeypatch, lambda path: (120 - 10 * len(bk.list_dumps(path))) * bk.MIB)
    # 11 copies leave 10 MB, 21 are needed: without the old copies gone the card stays full every night.
    tools = Tools()
    done = bk.backup(cfg, NIGHT, run=tools)
    assert (done["ok"], tools.dumps, done["deleted"], done["kept"], done["rotated"]) == (True, 1, 10, 2, True)
    # Still short of space: the run fails, and what expired goes all the same.
    old_dumps(cfg, nights(9, utc(11, month=8)))
    _free(monkeypatch, lambda path: 0)
    tools = Tools()
    failed = bk.backup(cfg, NIGHT + timedelta(days=1), run=tools)
    assert (failed["error"], tools.dumps, failed["deleted"], failed["kept"], failed["rotated"]) == (
        "low_space", 0, 9, 2, True)


def test_a_card_too_full_for_the_status_still_lets_the_expired_copies_go(tmp_path, monkeypatch, caplog):
    cfg = cfg_in(tmp_path)
    old_dumps(cfg, nights(11, utc(20, month=8)))
    _last_dump(cfg, 10 * bk.MIB)
    _free(monkeypatch, lambda path: (120 - 10 * len(bk.list_dumps(path))) * bk.MIB)
    # Not a byte left: the status that says it runs cannot be written. The run goes on, the old copies go.
    write, written = bk.write_status, []

    def full_at_first(path, status):
        written.append(status["running"])
        if len(written) == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        write(path, status)

    monkeypatch.setattr(bk, "write_status", full_at_first)
    tools = Tools()
    done = bk.backup(cfg, NIGHT, run=tools)
    assert written == [True, False] and (done["ok"], tools.dumps, done["deleted"], done["kept"]) == (True, 1, 10, 2)
    assert status_of(cfg) == done and "backup status not written" in caplog.text
    # Nothing at all can be written: one line in the journal and an exit code of its own, no traceback.
    def full(path, status):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(bk, "write_status", full)
    monkeypatch.setattr("nestor.logs.setup_logging", lambda level: None)
    monkeypatch.setattr(bk, "load_config", lambda: cfg)
    caplog.clear()
    assert bk.main(["--force"]) == bk.EX_IOERR and "backup status not written" in caplog.text


def test_failed_nights_let_expired_copies_go_and_keep_the_last_two_good_ones(tmp_path):
    cfg = cfg_in(tmp_path, attempts=1)
    first = NIGHT - timedelta(days=70)
    for n in range(30):
        assert bk.backup(cfg, first + timedelta(days=n), run=Tools())["ok"]
    good = [at for at, _ in bk.list_dumps(cfg.dumps_dir)]
    assert len(good) == 11
    down = "pg_dump: error: connection to server at \"192.168.0.1\", port 5432 failed: Connection refused"
    deleted = 0
    for n in range(30, 70):                                            # 40 nights, the 04:30 run and the 13:30 one
        for hours in (0, 9):
            now = first + timedelta(days=n, hours=hours)
            status = bk.backup(cfg, now, run=Tools(down))
            assert (status["ok"], status["error"], status["rotated"]) == (False, "unreachable", True)
            deleted += status["deleted"]
            kept = [at for at, _ in bk.list_dumps(cfg.dumps_dir)]
            expired = [at for at in kept if now - at > timedelta(days=BACKUP_MAX_AGE_DAYS)]
            assert set(expired) <= set(good[-2:]) and kept[-2:] == good[-2:], n
    assert [at for at, _ in bk.list_dumps(cfg.dumps_dir)] == good[-2:] and deleted == 9
    assert (status["kept"], status["failed_nights"], status["last_ok_file"]) == (2, 40, bk.dump_name(good[-1]))


def test_a_failed_run_deletes_nothing_by_a_clock_it_cannot_trust(tmp_path):
    cfg = cfg_in(tmp_path)
    old_dumps(cfg, nights(30, utc(20, month=8)))
    auth = "FATAL:  password authentication failed for user \"nestor_backup\""
    unsynced = bk.backup(cfg, NIGHT, run=Tools(auth, synced=False))
    assert (unsynced["error"], unsynced["warnings"], unsynced["rotated"], unsynced["deleted"], unsynced["kept"]) == (
        "auth_failed", ["clock_unsynced"], False, 0, 30)
    old_dumps(cfg, [NIGHT.astimezone(UTC) + timedelta(days=2)])          # a dump from the future
    backward = bk.backup(cfg, NIGHT + timedelta(days=1), run=Tools(auth))
    assert (backward["warnings"], backward["rotated"], backward["deleted"], backward["kept"]) == (
        ["clock_backwards"], False, 0, 31)


def _undeletable(monkeypatch, name: str) -> None:
    """One dump the card refuses to delete (chattr +i, an owner mixed up by a copy by hand)."""
    unlink = Path.unlink

    def refuse(self, *args, **kw):
        if self.name == name:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
        return unlink(self, *args, **kw)

    monkeypatch.setattr(Path, "unlink", refuse)


def test_an_old_copy_the_card_will_not_delete_is_a_warning_not_a_failed_backup(tmp_path, monkeypatch):
    cfg = cfg_in(tmp_path)
    old = nights(10, utc(20, month=8))                                # all far older than 35 days
    old_dumps(cfg, old)
    _undeletable(monkeypatch, bk.dump_name(old[0]))
    tools = Tools()
    done = bk.backup(cfg, NIGHT, run=tools)
    # The rotation runs before the dump and after it: neither one stops the dump. The oldest copy refuses
    # first, so the rest of that rotation waits for the next night.
    assert (done["ok"], tools.dumps, done["warnings"], done["rotated"], done["deleted"], done["kept"]) == (
        True, 1, ["rotation_failed"], False, 0, 11)
    assert (cfg.dumps_dir / done["file"]).exists()
    # A night the dump fails too: the error is the dump's, not the rotation's.
    tools = Tools("pg_dump: error: connection to server at \"192.168.0.1\", port 5432 failed: Connection refused")
    failed = bk.backup(replace(cfg, attempts=1), NIGHT + timedelta(days=1), run=tools)
    assert (failed["ok"], failed["error"], tools.dumps, failed["warnings"]) == (
        False, "unreachable", 1, ["rotation_failed"])


def test_a_dying_card_is_named_without_its_path(tmp_path):
    cfg = cfg_in(tmp_path)
    failed = bk.backup(cfg, NIGHT, run=Tools(OSError(errno.EROFS, "Read-only file system", str(cfg.dumps_dir))))
    assert (failed["ok"], failed["error"]) == (False, "disk_error")
    assert cfg.error_file.read_text() == "OSError errno=EROFS" and str(tmp_path) not in json.dumps(failed)
    full = bk.backup(cfg, NIGHT, run=Tools(OSError(errno.ENOSPC, "No space left on device")), force=True)
    assert full["error"] == "no_space"
    other = bk.backup(cfg, NIGHT, run=Tools(OSError(errno.EACCES, "Permission denied")), force=True)
    assert other["error"] == "failed" and cfg.error_file.read_text() == "PermissionError errno=EACCES"


def test_a_run_that_dies_leaves_running_set(tmp_path):
    cfg = cfg_in(tmp_path)
    with pytest.raises(RuntimeError):
        bk.backup(cfg, NIGHT, run=Tools(RuntimeError("bug")))
    assert status_of(cfg)["running"] is True                                   # which is how the watchdog finds it
    assert [p.name for p in cfg.dumps_dir.glob(".*")] == [".nestor-20261005T013005Z.dump.partial"]
    bk.backup(cfg, NIGHT + timedelta(minutes=5), run=Tools(), force=True)      # the next run clears the leftover
    assert not list(cfg.dumps_dir.glob(".*")) and status_of(cfg)["running"] is False


def test_rotation_is_skipped_on_unsynced_or_backward_clock(tmp_path):
    cfg = cfg_in(tmp_path)
    old_dumps(cfg, nights(30, utc(4)))                                         # a month of nights before tonight
    # The clock is not synced: the dump is made, nothing is judged old by it.
    unsynced = bk.backup(cfg, NIGHT, run=Tools(synced=False))
    assert (unsynced["ok"], unsynced["warnings"], unsynced["rotated"], unsynced["deleted"], unsynced["kept"]) == (
        True, ["clock_unsynced"], False, 0, 31)
    # A dump from the future: the clock went backwards, or this one is wrong.
    old_dumps(cfg, [NIGHT.astimezone(UTC) + timedelta(days=2)])
    backward = bk.backup(cfg, NIGHT + timedelta(days=1), run=Tools())
    assert (backward["warnings"], backward["rotated"], backward["deleted"], backward["kept"]) == (
        ["clock_backwards"], False, 0, 33)
    # A few minutes of difference is not backwards.
    slack = cfg_in(tmp_path / "slack")
    old_dumps(slack, [NIGHT.astimezone(UTC) + timedelta(minutes=4)])
    assert bk.backup(slack, NIGHT, run=Tools())["warnings"] == []
    # Trusted clock, no future dump: the rotation runs and says what it deleted.
    cfg2 = cfg_in(tmp_path / "second")
    old_dumps(cfg2, nights(30, utc(4)))
    done = bk.backup(cfg2, NIGHT, run=Tools())
    assert (done["warnings"], done["rotated"], done["kept"], done["deleted"]) == ([], True, 11, 20)
    assert len(list(cfg2.dumps_dir.iterdir())) == 11 and (cfg2.dumps_dir / done["file"]).exists()
    assert done["oldest_dump_at"] == "2026-09-13T01:30:00Z"                   # the Sunday of the 4th week back


def test_the_newest_two_dumps_survive_however_old_when_a_new_one_comes(tmp_path):
    cfg = cfg_in(tmp_path)
    old_dumps(cfg, nights(10, utc(20, month=8)))                               # all of them far older than 35 days
    done = bk.backup(cfg, NIGHT, run=Tools())
    assert done["kept"] == 2 and done["deleted"] == 9 and done["rotated"] is True     # the new one and the last good one
    assert sorted(p.name for p in cfg.dumps_dir.iterdir()) == ["nestor-20260820T013000Z.dump", done["file"]]


def test_a_dump_that_shrank_is_a_warning_not_a_failure(tmp_path):
    cfg = cfg_in(tmp_path)
    bk.backup(cfg, NIGHT, run=Tools(size=1000))
    half = bk.backup(cfg, NIGHT + timedelta(days=1), run=Tools(size=500))      # exactly half: not under it
    assert half["warnings"] == [] and half["ok"]
    smaller = bk.backup(cfg, NIGHT + timedelta(days=2), run=Tools(size=100))
    assert (smaller["ok"], smaller["warnings"], smaller["shrank_pct"]) == (True, ["shrank"], 80)
    assert bk.backup(cfg, NIGHT + timedelta(days=3), run=Tools(size=100))["warnings"] == []


def test_nothing_is_due_after_a_good_night_and_the_lock_is_free_afterwards(tmp_path):
    cfg, tools = cfg_in(tmp_path), Tools()
    bk.backup(cfg, NIGHT, run=tools)
    assert bk.backup(cfg, NIGHT + timedelta(hours=9), run=tools) is None
    fd = os.open(cfg.lock_file, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)                             # not held by the finished runs
    os.close(fd)


def test_a_second_run_while_the_first_holds_the_lock_exits_75(tmp_path, monkeypatch, caplog):
    cfg, tools = cfg_in(tmp_path), Tools()
    cfg.state_dir.mkdir(parents=True)
    held = os.open(cfg.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        with pytest.raises(bk.BackupBusy):
            bk.backup(cfg, NIGHT, run=tools)
        assert tools.dumps == 0 and not cfg.status_file.exists()                # it did not touch anything
        monkeypatch.setattr("nestor.logs.setup_logging", lambda level: None)
        monkeypatch.setattr(bk, "load_config", lambda: cfg)
        with caplog.at_level(logging.INFO, logger="nestor.backup"):
            assert bk.main([]) == bk.EX_TEMPFAIL == 75
        assert "backup already running" in caplog.text
    finally:
        os.close(held)
    assert bk.backup(cfg, NIGHT, run=tools)["ok"]                              # the lock was the only thing in the way


# --- what a failed tool printed goes to one file --------------------------------------------------------------

def _script_bin(tmp_path: Path, stderr: str, record: Path | None = None) -> Path:
    """pg_dump and pg_restore that fail the way a real one does, with `stderr` on the way; with `record`, they
    first write their arguments and whether PGPASSWORD was set into that file."""
    bindir = tmp_path / "script-bin"
    bindir.mkdir(parents=True)
    spy = f'printf "%s\\n" "$@" "PGPASSWORD=${{PGPASSWORD-unset}}" > "{record}"' if record else ""
    for tool in ("pg_dump", "pg_restore"):
        (bindir / tool).write_text(f"""#!/bin/sh
if [ "$1" = "--version" ]; then echo "pg_dump (PostgreSQL) 18.6 (Debian 18.6-1.pgdg130+1)"; exit 0; fi
{spy}
cat >&2 <<'END_OF_STDERR'
{stderr}
END_OF_STDERR
exit 1
""")
        (bindir / tool).chmod(0o755)
    return bindir


def test_pg_dump_stderr_never_reaches_logs_or_status(tmp_path, monkeypatch, caplog, capfd):
    stderr = "\n".join(['pg_dump: error: query failed: ERROR:  permission denied for table messages',
                        f'pg_dump: detail: Query was: COPY ... "{CANARY}"'] + ["x" * 100] * 80 + [f"last line {CANARY}"])
    cfg = replace(cfg_in(tmp_path), pg_bindir=_script_bin(tmp_path, stderr))
    monkeypatch.setattr("nestor.logs.setup_logging", lambda level: None)
    monkeypatch.setattr(bk, "load_config", lambda: cfg)
    with caplog.at_level(logging.DEBUG):
        assert bk.main(["--force"]) == 1
    out, err = capfd.readouterr()
    everything = out + err + caplog.text + repr([r.__dict__ for r in caplog.records]) + cfg.status_file.read_text()
    assert "CANARY" not in everything and "secret" not in everything.lower() and "Query was" not in everything
    status = status_of(cfg)
    assert (status["ok"], status["error"], status["running"]) == (False, "permission_denied", False)
    failed = [r for r in caplog.records if r.getMessage() == "backup failed"]
    assert len(failed) == 1 and (failed[0].error, failed[0].returncode, failed[0].attempt) == ("permission_denied", 1, 1)
    # Only the tail of it, in a file nobody else can read.
    text = cfg.error_file.read_bytes()
    assert stat.S_IMODE(cfg.error_file.stat().st_mode) == 0o600
    assert len(text) == bk.ERROR_TAIL and text.endswith(f"last line {CANARY}\n".encode()) and b"permission denied" not in text
    # A good night removes it: it describes a failure that is over.
    bk.backup(cfg_in(tmp_path), NIGHT + timedelta(days=1), run=Tools(), force=True)
    assert not cfg.error_file.exists()


def test_main_exit_codes(tmp_path, monkeypatch):
    cfg = cfg_in(tmp_path)
    monkeypatch.setattr("nestor.logs.setup_logging", lambda level: None)
    monkeypatch.setattr(bk, "load_config", lambda: cfg)
    bk.backup(cfg, datetime.now(UTC), force=True, run=Tools())
    assert bk.main([]) == 0                                                    # done a moment ago: not due, success
    failing = replace(cfg, pg_bindir=_script_bin(tmp_path, "FATAL:  password authentication failed"))
    monkeypatch.setattr(bk, "load_config", lambda: failing)
    assert bk.main([]) == 0 and bk.main(["--force"]) == 1


# --- the real binaries against the throwaway cluster -----------------------------------------------------------

@pytest.fixture
async def pg_bin(conn):
    major = int(await conn.fetchval("SHOW server_version_num")) // 10000
    installed = sorted((p for p in bk.PG_LIB.glob("[0-9]*") if p.name.isdigit() and int(p.name) >= major
                        and (p / "bin" / "pg_dump").exists() and (p / "bin" / "pg_restore").exists()),
                       key=lambda p: int(p.name), reverse=True)
    if not installed:
        pytest.skip(f"no pg_dump {major} or newer installed")
    return installed[0] / "bin"


@pytest.fixture
def cfg(tmp_path, pg_bin, monkeypatch):
    """The unit's view of the test database: the role nestor_backup, its password only in a pgpass file."""
    host = urlsplit(ADMIN_URL)
    pgpass = tmp_path / "pgpass"
    pgpass.write_text(f"{host.hostname}:{host.port}:{DB}:nestor_backup:backup\n")
    pgpass.chmod(0o600)
    monkeypatch.setenv("PGPASSFILE", str(pgpass))
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setattr(bk, "clock_synced", lambda run=None: True)
    # With the example's libpq parameters (keepalives, timeouts): this pg_dump has to accept all of them.
    query = _env_file(DEPLOY / "nestor-backup.env.example")["BACKUP_DATABASE_URL"].split("?", 1)[1]
    return bk.BackupConfig(database_url=f"postgresql://nestor_backup@{host.hostname}:{host.port}/{DB}?{query}", tz=MSK,
                           state_dir=tmp_path / "state", pg_bindir=pg_bin, min_free_mb=1, attempts=2, retry_delay_s=0)


async def _fill(conn, rows: int = 300) -> None:
    await conn.execute("INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup')", CHAT_ID)
    await conn.execute("INSERT INTO users (user_id, first_name) VALUES (111, 'Аня')")
    await conn.execute(
        """INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw)
           SELECT $1, g, 111, now(), repeat(md5(g::text || random()::text), 20), '{}' FROM generate_series(1, $2) g""",
        CHAT_ID, rows)


def _toc(pg_bin: Path, path: Path) -> str:
    return subprocess.run([str(pg_bin / "pg_restore"), "--list", str(path)], capture_output=True, text=True,
                          check=True).stdout


def _lists(pg_bin: Path, path: Path) -> bool:
    return subprocess.run([str(pg_bin / "pg_restore"), "--list", str(path)], capture_output=True,
                          check=False).returncode == 0


def _sql(pg_bin: Path, path: Path) -> str:
    """Everything in the dump, as the SQL a restore would run."""
    return subprocess.run([str(pg_bin / "pg_restore"), "--file=-", str(path)], capture_output=True, text=True,
                          check=True).stdout


def _pipe(path: Path) -> subprocess.Popen:
    return subprocess.Popen(["cat", str(path)], stdout=subprocess.PIPE)       # as `sudo cat dump | python -m ...`


async def test_backup_writes_a_private_verified_dump(conn, cfg, pg_bin):
    await _fill(conn)
    status = bk.backup(cfg, NIGHT)
    assert status["ok"] is True and status["running"] is False and status["error"] is None
    dump = cfg.dumps_dir / status["file"]
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600 and stat.S_IMODE(cfg.dumps_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(cfg.status_file.stat().st_mode) == 0o644
    assert [p.name for p in cfg.dumps_dir.iterdir()] == [status["file"]]       # no partial left
    toc = _toc(pg_bin, dump)
    assert all(f"TABLE DATA {t} " in toc for t in bk.REQUIRED_DATA)
    assert "Compression: zstd" in toc                                          # the default, with this pg_dump too
    assert status == status_of(cfg) and status["size"] == dump.stat().st_size
    assert status["server_version"].split(".")[0] == str(int(await conn.fetchval("SHOW server_version_num")) // 10000)
    assert not cfg.error_file.exists()


async def test_the_dump_can_be_made_without_zstd_too(conn, cfg, pg_bin):
    await _fill(conn, 50)
    status = bk.backup(replace(cfg, compress="gzip:6"), NIGHT)
    assert status["ok"] and "Compression: gzip" in _toc(pg_bin, cfg.dumps_dir / status["file"])
    refused = bk.backup(replace(cfg, compress="nonsense:1"), NIGHT, force=True)
    assert (refused["ok"], refused["error"], refused["attempts"]) == (False, "failed", 1)


async def test_running_flag_is_written_before_the_dump(conn, cfg):
    await _fill(conn, 50)
    first = bk.backup(cfg, NIGHT)
    seen = []

    def spy(argv, **kw):
        if Path(argv[0]).name == "pg_dump" and "-f" in argv:
            seen.append(status_of(cfg))
        return subprocess.run(argv, check=False, **kw)

    second = bk.backup(cfg, NIGHT + timedelta(days=1), run=spy)
    assert second["ok"] and len(seen) == 1
    assert seen[0]["running"] is True and seen[0]["started_at"] == "2026-10-06T01:30:05Z"
    assert (seen[0]["last_ok_at"], seen[0]["last_ok_file"], seen[0]["last_ok_size"]) == (
        first["last_ok_at"], first["last_ok_file"], first["last_ok_size"])
    assert status_of(cfg)["running"] is False


async def test_truncated_dump_fails_verification_although_list_passes(conn, cfg, pg_bin, tmp_path):
    await _fill(conn, 2000)
    good = cfg.dumps_dir / bk.backup(cfg, NIGHT)["file"]
    cut = tmp_path / "cut.dump"
    cut.write_bytes(good.read_bytes()[: good.stat().st_size * 3 // 4])
    assert _lists(pg_bin, cut)                         # the table of contents is at the start of the file
    with pytest.raises(bk.BackupError) as e:
        bk.verify(pg_bin, cut)
    assert (e.value.code, e.value.returncode) == ("verify_failed", 1)
    assert bk.verify(pg_bin, good).split(".")[0].isdigit()

    def truncating(argv, **kw):                       # the card loses the end of the file while pg_dump writes it
        done = subprocess.run(argv, check=False, **kw)
        if Path(argv[0]).name == "pg_dump" and "-f" in argv:
            target = Path(argv[argv.index("-f") + 1])
            target.write_bytes(target.read_bytes()[: target.stat().st_size * 3 // 4])
        return done

    failed = bk.backup(cfg, NIGHT + timedelta(days=1), run=truncating)
    assert (failed["ok"], failed["error"], failed["attempts"]) == (False, "verify_failed", 1)
    assert [p.name for p in cfg.dumps_dir.iterdir()] == [good.name]            # the bad one never became a dump
    assert failed["last_ok_file"] == good.name


async def test_failed_run_keeps_old_dumps_and_carries_last_ok(conn, cfg, monkeypatch):
    await _fill(conn, 50)
    first = bk.backup(cfg, NIGHT)
    host = urlsplit(ADMIN_URL)
    nobody = replace(cfg, database_url=f"postgresql://nestor_backup@{host.hostname}:1/{DB}")
    sleeps: list[float] = []
    monkeypatch.setattr(bk.time, "sleep", sleeps.append)
    failed = bk.backup(nobody, NIGHT + timedelta(days=1), sleep=sleeps.append)
    assert (failed["ok"], failed["error"], failed["attempts"], failed["failed_nights"]) == (False, "unreachable", 2, 1)
    assert sleeps == [0] and (failed["last_ok_at"], failed["last_ok_file"]) == (first["last_ok_at"], first["file"])
    assert [p.name for p in cfg.dumps_dir.iterdir()] == [first["file"]]
    monkeypatch.setattr("nestor.logs.setup_logging", lambda level: None)
    monkeypatch.setattr(bk, "load_config", lambda: replace(nobody, retry_delay_s=0, attempts=1))
    assert bk.main(["--force"]) == 1


async def test_a_dump_by_the_wrong_password_is_an_auth_failure(conn, cfg, tmp_path, monkeypatch):
    pgpass = tmp_path / "pgpass"
    pgpass.write_text(pgpass.read_text().replace(":backup\n", ":not-the-password\n"))
    failed = bk.backup(cfg, NIGHT)
    assert (failed["ok"], failed["error"], failed["attempts"]) == (False, "auth_failed", 1)
    assert cfg.error_file.exists() and stat.S_IMODE(cfg.error_file.stat().st_mode) == 0o600


# --- the restore -----------------------------------------------------------------------------------------------

RESTORED = "nestor_restored_test"
OLDER = "nestor_older_test"                 # an archive one migration behind the code


async def _drop(name: str) -> None:
    admin = await asyncpg.connect(ADMIN_URL)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    finally:
        await admin.close()


@pytest.fixture
async def fresh(database):
    """What `bootstrap database` leaves on a new cluster: the database and the roles, no tables."""
    await _drop(RESTORED)
    await bootstrap(ADMIN_URL, RESTORED, "collector", "reader")
    yield RESTORED
    await _drop(RESTORED)


@pytest.fixture
def restore_env(database, pg_bin, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", database["collector"])
    monkeypatch.setenv("BOT_TOKEN", f"{BOT_ID}:AAA-not-a-token")
    monkeypatch.setenv("BACKUP_PG_BINDIR", str(pg_bin))
    monkeypatch.setattr(bk, "ENV_FILE", str(tmp_path / "no-such.env"))       # whatever this machine has in /opt
    monkeypatch.setattr("nestor.logs.setup_logging", lambda level: None)


async def _restored(database, name: str = RESTORED):
    return await asyncpg.connect(with_database(database["collector"], name))


async def test_restore_into_a_fresh_database_replays_optouts_and_purges(
        conn, cfg, pg_bin, fresh, restore_env, database, tmp_path, caplog):
    X, Y, ANON = 777, 888, 1087968824
    await conn.execute("INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup')", CHAT_ID)
    for uid, name, is_bot in ((X, "Икс", False), (Y, "Игрек", False), (BOT_ID, "Нестор", True),
                              (ANON, "Group", True)):
        await conn.execute("INSERT INTO users (user_id, first_name, username, is_bot) VALUES ($1, $2, $3, $4)",
                           uid, name, f"u{uid}", is_bot)

    async def message(mid, uid, text, *, stored="1 day", reply_to=None, quote=None):
        raw = {"message_id": mid, "text": text}
        if quote:
            raw["quote"] = {"text": quote}
        await conn.execute(
            """INSERT INTO messages (chat_id, message_id, user_id, sent_at, reply_to_id, text, raw, stored_at)
               VALUES ($1, $2, $3, now(), $4, $5, $6, now() - $7::text::interval)""",
            CHAT_ID, mid, uid, reply_to, text, raw, stored)

    await message(1, X, "слова человека, который потом ушёл")
    await message(2, Y, "обычное сообщение")
    await message(3, Y, "ответ с цитатой", reply_to=1, quote="слова человека")
    await message(4, Y, "это попало в архив 13 месяцев назад", stored="13 months")
    await message(5, ANON, "объявление анонимного админа")
    await message(900, BOT_ID, "ответ бота, где процитирован человек", reply_to=2)
    await conn.execute(
        """INSERT INTO bot_requests (chat_id, trigger_msg_id, user_id, scenario, question, answer, status, answer_msg_ids,
                                     quoted_user_ids, oldest_source_at)
           VALUES ($1, 2, $2, 'other', 'вопрос', 'ответ бота', 'ok', '{900}', ARRAY[$3]::bigint[], now())""",
        CHAT_ID, Y, X)

    status = bk.backup(cfg, NIGHT)
    dump = cfg.dumps_dir / status["file"]
    assert "слова человека, который потом ушёл" in _sql(pg_bin, dump)         # the copy has what is forgotten next

    # After the copy: X says /forget_me. The watchdog has recorded the id.
    async with conn.transaction():
        await forget.forget(conn, X, BOT_ID)
    optouts = tmp_path / "optouts.json"
    optouts.write_text(json.dumps([X, ANON]))       # an anonymous admin's, from before /forget_me refused it

    pipe = _pipe(dump)
    try:
        with caplog.at_level(logging.INFO):
            done = await bk.restore(pipe.stdout, fresh, optouts=optouts)
    finally:
        pipe.stdout.close()
        pipe.wait()
    assert done == {"messages": 3, "users": 4, "chats": 1, "bot_requests": 1, "forgotten": 1, "purged": 1,
                    "last_migration": "0008_retention_and_ops"}

    db = await _restored(database)
    try:
        assert await db.fetchval("SELECT count(*) FROM messages WHERE user_id = $1", X) == 0
        user = await db.fetchrow("SELECT opted_out, username, first_name FROM users WHERE user_id = $1", X)
        assert (user["opted_out"], user["username"], user["first_name"]) == (True, None, None)
        assert sorted(r[0] for r in await db.fetch("SELECT message_id FROM messages")) == [2, 3, 5]
        assert await db.fetchval("SELECT opted_out FROM users WHERE user_id = $1", ANON) is False
        assert await db.fetchval("SELECT count(*) FROM messages WHERE raw::text LIKE '%слова человека%' "
                                 "OR text LIKE '%слова человека%'") == 0       # nor the quote of them in Y's reply
        assert await db.fetchval("SELECT answer FROM bot_requests") is None   # the answer that quoted X is scrubbed
        assert await db.fetchval("SELECT count(*) FROM messages WHERE message_id = 900") == 0
        assert await db.fetchval("SELECT count(*) FROM messages WHERE message_id = 4") == 0   # outlived 12 months
        assert await db.fetchval("SELECT count(*) FROM schema_migrations") == 8
        # What the dump held is still there, as the owner sees it, and the reader's views came along.
        assert await db.fetchval("SELECT count(*) FROM reader.messages") == 3
    finally:
        await db.close()
    assert "CANARY" not in caplog.text and "слова" not in caplog.text and "token" not in caplog.text.lower()


async def test_restore_leaves_the_purge_to_the_night_when_the_clocks_disagree(
        conn, cfg, fresh, restore_env, database, tmp_path, monkeypatch, caplog):
    await _fill(conn, 3)
    await conn.execute("INSERT INTO users (user_id, first_name) VALUES (222, 'Боря')")
    await conn.execute("""INSERT INTO messages (chat_id, message_id, user_id, sent_at, raw, stored_at)
                          VALUES ($1, 100, 222, now(), '{}', now() - interval '13 months')""", CHAT_ID)
    dump = cfg.dumps_dir / bk.backup(cfg, NIGHT)["file"]
    optouts = tmp_path / "optouts.json"
    optouts.write_text("[111]")
    # The Pi's clock an hour off the router's: which one is wrong, nobody knows, so nothing is purged by it.
    monkeypatch.setattr("nestor.retention.utcnow", lambda: datetime.now(UTC) + timedelta(hours=1))
    with dump.open("rb") as source, caplog.at_level(logging.WARNING, logger="nestor.backup"):
        done = await bk.restore(source, fresh, optouts=optouts)
    assert (done["purged"], done["purge_refused"], done["forgotten"], done["messages"]) == (None, "clock_skew", 1, 1)
    assert [(r.getMessage(), r.error) for r in caplog.records if r.name == "nestor.backup"] == [
        ("restore not purged", "clock_skew")]
    db = await _restored(database)
    try:
        assert await db.fetchval("SELECT message_id FROM messages") == 100                     # still here, for the night
        assert await db.fetchval("SELECT opted_out FROM users WHERE user_id = 111") is True     # the rest was done
        assert await db.fetchval("SELECT max(version) FROM schema_migrations") == "0008_retention_and_ops"
    finally:
        await db.close()


async def test_a_restore_that_fails_after_loading_says_the_database_is_not_empty(
        conn, cfg, fresh, restore_env, database, tmp_path, monkeypatch, capfd):
    await _fill(conn, 5)
    dump = cfg.dumps_dir / bk.backup(cfg, NIGHT)["file"]
    optouts = tmp_path / "optouts.json"
    optouts.write_text("[111]")

    async def broken(conn, user_id, bot_id):
        raise RuntimeError(f"row {CANARY}")

    monkeypatch.setattr("nestor.forget.forget", broken)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=dump.open("rb")))
    assert await asyncio.to_thread(bk.main, ["restore", "--database", fresh, "--optouts", str(optouts)]) == 1
    out, err = capfd.readouterr()
    assert out == ("restore failed RuntimeError: the copy is in the database, unfinished; drop it, run bootstrap, "
                   "restore again\n") and "CANARY" not in out + err
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=dump.open("rb")))
    assert await asyncio.to_thread(bk.main, ["restore", "--database", fresh]) == 1
    assert capfd.readouterr().out == "restore failed not_empty\n"          # which is why it said so


async def test_restore_brings_an_older_copy_up_to_the_newest_migration(cfg, fresh, restore_env, database, tmp_path):
    """A copy made before the newest migration: loaded as it was, migrated, and only then forgotten by today's code."""
    *before, newest = migration_files()
    older_dir = tmp_path / "migrations"
    older_dir.mkdir()
    for path in before:
        (older_dir / path.name).symlink_to(path)
    await _drop(OLDER)
    await bootstrap(ADMIN_URL, OLDER, "collector", "reader")
    await ensure_backup_role(ADMIN_URL, OLDER, "backup")
    try:
        owner = await asyncpg.connect(with_database(database["collector"], OLDER))
        try:
            await migrate(owner, older_dir)
            await owner.execute("INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup')", CHAT_ID)
            await owner.execute("INSERT INTO users (user_id, first_name) VALUES (111, 'Аня'), (222, 'Боря')")
            await owner.execute("""INSERT INTO messages (chat_id, message_id, user_id, sent_at, raw)
                                   VALUES ($1, 1, 111, now(), '{}'), ($1, 2, 222, now(), '{}')""", CHAT_ID)
        finally:
            await owner.close()
        pgpass = tmp_path / "pgpass"
        host = urlsplit(ADMIN_URL)
        pgpass.write_text(pgpass.read_text() + f"{host.hostname}:{host.port}:{OLDER}:nestor_backup:backup\n")
        status = bk.backup(replace(cfg, database_url=cfg.database_url.replace(f"/{DB}?", f"/{OLDER}?")), NIGHT)
        assert status["ok"], status["error"]
        optouts = tmp_path / "optouts.json"
        optouts.write_text("[111]")
        with (cfg.dumps_dir / status["file"]).open("rb") as source:
            done = await bk.restore(source, fresh, optouts=optouts)
    finally:
        await _drop(OLDER)
    assert (done["last_migration"], done["messages"], done["forgotten"]) == (newest.stem, 1, 1)
    db = await _restored(database)
    try:
        assert await db.fetchval("SELECT count(*) FROM schema_migrations") == len(before) + 1
        assert await db.fetchval("SELECT user_id FROM messages") == 222
    finally:
        await db.close()


async def test_restore_without_optouts_says_so(conn, cfg, fresh, restore_env, database, caplog):
    await _fill(conn, 20)
    dump = cfg.dumps_dir / bk.backup(cfg, NIGHT)["file"]
    with dump.open("rb") as source, caplog.at_level(logging.WARNING, logger="nestor.backup"):
        done = await bk.restore(source, fresh, optouts=None)
    assert (done["messages"], done["forgotten"], done["purged"]) == (20, 0, 0)
    assert any("without opt-outs" in r.getMessage() for r in caplog.records)


async def test_restore_refuses_a_database_that_is_not_empty(conn, cfg, fresh, restore_env, database):
    await _fill(conn, 20)
    dump = cfg.dumps_dir / bk.backup(cfg, NIGHT)["file"]
    with dump.open("rb") as source:
        await bk.restore(source, fresh, optouts=None)
    db = await _restored(database)
    try:
        await db.execute("DELETE FROM messages WHERE message_id <= 5")
    finally:
        await db.close()
    with dump.open("rb") as source, pytest.raises(bk.BackupError) as e:
        await bk.restore(source, fresh, optouts=None)
    assert e.value.code == "not_empty"
    db = await _restored(database)
    try:
        assert await db.fetchval("SELECT count(*) FROM messages") == 15      # nothing was overwritten
    finally:
        await db.close()
    with dump.open("rb") as source, pytest.raises(bk.BackupError) as e:
        await bk.restore(source, "nestor_there_is_no_such_database", optouts=None)
    assert e.value.code == "no_database"
    with dump.open("rb") as source, pytest.raises(bk.BackupError) as e:
        await bk.restore(source, "x; DROP DATABASE nestor_test", optouts=None)
    assert e.value.code == "bad_database"


async def test_restore_reads_the_optouts_before_it_touches_the_database(conn, cfg, fresh, restore_env, database, tmp_path):
    await _fill(conn, 5)
    dump = cfg.dumps_dir / bk.backup(cfg, NIGHT)["file"]
    for content in (None, "not json", '{"a": 1}', '["1", 2]', "[true]"):
        path = tmp_path / "optouts.json"
        path.unlink(missing_ok=True)
        if content is not None:
            path.write_text(content)
        with dump.open("rb") as source, pytest.raises(bk.BackupError) as e:
            await bk.restore(source, fresh, optouts=path)
        assert e.value.code == "optouts_unreadable"
    db = await _restored(database)
    try:
        assert await db.fetchval("SELECT to_regclass('public.schema_migrations') IS NULL")      # still empty
    finally:
        await db.close()


async def test_restore_needs_the_roles_the_dump_names(conn, cfg, fresh, restore_env, database):
    await _fill(conn, 5)
    dump = cfg.dumps_dir / bk.backup(cfg, NIGHT)["file"]
    admin = await asyncpg.connect(ADMIN_URL)
    try:
        # A cluster rebuilt by a bootstrap that did not see BACKUP_PASSWORD: the grants in the dump name nestor_backup.
        await admin.execute("ALTER ROLE nestor_backup RENAME TO nestor_backup_away")
        try:
            with dump.open("rb") as source, pytest.raises(bk.BackupError) as e:
                await bk.restore(source, fresh, optouts=None)
        finally:
            await admin.execute("ALTER ROLE nestor_backup_away RENAME TO nestor_backup")
    finally:
        await admin.close()
    assert e.value.code == "role_missing" and e.value.lines >= 1
    db = await _restored(database)
    try:
        assert await db.fetchval("SELECT to_regclass('public.schema_migrations') IS NULL")      # one transaction: nothing stayed
    finally:
        await db.close()


async def test_pg_restore_stderr_canary_not_printed(conn, cfg, fresh, restore_env, database, tmp_path, monkeypatch,
                                                    capfd, caplog):
    await _fill(conn, 5)
    dump = cfg.dumps_dir / bk.backup(cfg, NIGHT)["file"]
    stderr = "\n".join(['pg_restore: error: could not execute query: ERROR:  permission denied for table messages',
                        f'CONTEXT:  COPY messages, line 3: "{CANARY}"', "pg_restore: error: ..."])
    monkeypatch.setenv("BACKUP_PG_BINDIR", str(_script_bin(tmp_path, stderr)))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=dump.open("rb")))
    with caplog.at_level(logging.DEBUG):
        code = await asyncio.to_thread(bk.main, ["restore", "--database", fresh])
    out, err = capfd.readouterr()
    assert code == 1 and out == "restore failed permission_denied (3 lines of stderr)\n"
    assert "CANARY" not in out + err + caplog.text + repr([r.__dict__ for r in caplog.records])


async def test_restore_gives_pg_restore_the_password_in_its_environment_only(fresh, restore_env, database, tmp_path,
                                                                             monkeypatch):
    # Arguments are in /proc/<pid>/cmdline and in ps for every user of the Pi; the environment is not.
    url = urlsplit(database["collector"])
    password = url.password
    empty = tmp_path / "empty.dump"
    empty.write_bytes(b"")
    for n, dsn in enumerate((database["collector"],
                             url._replace(netloc=f"nestor@{url.hostname}:{url.port}", query=f"password={password}")
                             .geturl())):
        record = tmp_path / f"argv-{n}.txt"
        monkeypatch.setenv("BACKUP_PG_BINDIR", str(_script_bin(tmp_path / str(n), "pg_restore: error: stop", record)))
        monkeypatch.setenv("DATABASE_URL", dsn)
        with empty.open("rb") as source, pytest.raises(bk.BackupError):
            await bk.restore(source, fresh, optouts=None)
        lines = record.read_text().splitlines()
        assert f"--dbname=postgresql://nestor@{url.hostname}:{url.port}/{fresh}" in lines, lines
        assert lines[-1] == f"PGPASSWORD={password}" and not any(password in line for line in lines[:-1])


def test_restore_does_not_wait_for_a_dump_typed_at_the_terminal(monkeypatch, capfd):
    monkeypatch.setattr("nestor.logs.setup_logging", lambda level: None)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=SimpleNamespace(isatty=lambda: True)))
    assert bk.main(["restore", "--database", "nestor"]) == 1
    assert capfd.readouterr().out == "restore failed no_dump_on_stdin\n"


async def test_main_restores_and_prints_counts_only(conn, cfg, fresh, restore_env, database, monkeypatch, capfd):
    await _fill(conn, 7)
    dump = cfg.dumps_dir / bk.backup(cfg, NIGHT)["file"]
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=dump.open("rb")))
    assert await asyncio.to_thread(bk.main, ["restore", "--database", fresh]) == 0
    out = capfd.readouterr().out
    assert out.startswith("restore done ") and json.loads(out.removeprefix("restore done ")) == {
        "messages": 7, "users": 1, "chats": 1, "bot_requests": 0, "last_migration": "0008_retention_and_ops",
        "forgotten": 0, "purged": 0}
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=dump.open("rb")))
    assert await asyncio.to_thread(bk.main, ["restore", "--database", fresh]) == 1       # now it is not empty
    assert capfd.readouterr().out == "restore failed not_empty\n"


async def test_forget_after_the_import_finds_who_used_forget_me_since_the_copy(conn, restore_env, capfd):
    """A /forget_me after the watchdog last wrote optouts.json is in neither it nor the copy. The import after
    the restore brings it back with its author's messages, and `forget` finds it there."""
    X, Y, Z, W, ANON = 777, 888, 999, 555, 1087968824
    made = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)        # when the copy was made
    await conn.execute("INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup')", CHAT_ID)
    for uid in (X, Y, Z, W, ANON):
        await conn.execute("INSERT INTO users (user_id, first_name) VALUES ($1, 'Имя')", uid)
    for mid, uid, text, sent in (
            (1, X, "слова Икса", made + timedelta(hours=1)),
            (2, X, "/forget_me", made + timedelta(hours=2)),
            (3, Y, "/Forget_me@ewok_nestor_bot", made - timedelta(hours=23)),  # sent before, carried out after
            (4, Z, "/forget_me", made - timedelta(days=2)),     # the copy has it: forgotten then, or refused
            (5, W, "напиши /forget_me, если хочешь", made + timedelta(hours=1)),
            (6, ANON, "/forget_me", made + timedelta(hours=1))):    # many people: never forgotten as one
        await conn.execute("INSERT INTO messages (chat_id, message_id, user_id, sent_at, text, raw) "
                           "VALUES ($1, $2, $3, $4, $5, $6)", CHAT_ID, mid, uid, sent, text,
                           {"message_id": mid, "text": text})

    since = f"nestor-{made:%Y%m%dT%H%M%SZ}.dump"
    assert await asyncio.to_thread(bk.main, ["forget", "--since", since]) == 0
    assert capfd.readouterr().out == "forgotten 2\n"
    assert [r[0] for r in await conn.fetch("SELECT user_id FROM users WHERE opted_out ORDER BY 1")] == [X, Y]
    assert [r[0] for r in await conn.fetch("SELECT message_id FROM messages ORDER BY 1")] == [4, 5, 6]
    with pytest.raises(SystemExit):
        bk.main(["forget", "--since", "2026-10-05T01:30"])         # a time without a zone is refused
