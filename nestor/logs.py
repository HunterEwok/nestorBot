"""Structured JSON logging. Callers pass ids and counters, never message text.

What the code does not control is sanitized here, in one place, for every logger:
- an exception is logged by its frames and class names (plus the SQLSTATE of a PostgreSQL error),
  never by its text: asyncpg puts the failing row (DETAIL) or a query argument into it, aiogram the
  request URL with the bot token;
- an exception passed as a format argument (aiogram's «Failed to fetch updates - %s: %s») or as an
  extra field becomes its class name; asyncio's report of a failed task keeps only its first line,
  the rest is reprs of the task and of its exception;
- in the final line, bot tokens, the passwords of database URLs, bearer keys and key=/password=
  values are blanked out, whoever wrote them;
- an exception nobody caught (one that ends `python -m nestor.migrate`, or a thread) goes the same way
  instead of Python's own report on stderr, which has its text; so does a record that cannot be formatted.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import traceback
from datetime import datetime, timezone

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}

# Applied to the JSON line. A value runs to whitespace or a closing quote and takes escapes whole
# (\" \\ \n), so a match never ends inside one and the line stays JSON.
_REDACT = (
    (re.compile(r"(?<![0-9])[0-9]{5,}:[A-Za-z0-9_-]{30,}"), "<bot-token>"),    # also in .../bot<token>/getMe
    (re.compile(r'(postgres(?:ql)?://)(?:[^@\s"\\]|\\.)+@'), r"\1<redacted>@"),
    (re.compile(r'(?i)bearer\s+(?:[^\s"\\]|\\.)+'), "Bearer <redacted>"),
    (re.compile(r'(?i)(api[_-]?key|password)=(?:[^\s"\\&]|\\.)+'), r"\1=<redacted>"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACT:
        text = pattern.sub(replacement, text)
    return text


def _name(e: BaseException) -> str:
    cls = type(e)
    return cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"


def _describe(e: BaseException) -> str:
    sqlstate = getattr(e, "sqlstate", None)       # asyncpg.PostgresError: the code says what happened
    return _name(e) + (f" (sqlstate={sqlstate})" if isinstance(sqlstate, str) else "")


def safe_exc(exc_info) -> str:
    """A traceback as Python prints it, with each exception's class name in place of its text."""
    chain, e = [], exc_info[1]
    while e is not None and all(e is not seen for seen in chain):
        chain.append(e)
        if e.__cause__ is not None:                 # raise ... from cause
            e = e.__cause__
        else:                                       # raised while handling another, unless `from None`
            e = None if e.__suppress_context__ else e.__context__
    parts = []
    for i, e in enumerate(reversed(chain)):
        if i:
            parts.append("\nThe above exception was the direct cause of the following exception:\n\n"
                         if e.__cause__ is not None else
                         "\nDuring handling of the above exception, another exception occurred:\n\n")
        if e.__traceback__ is not None:
            parts.append("Traceback (most recent call last):\n" + "".join(traceback.format_tb(e.__traceback__)))
        parts.append(_describe(e) + "\n")
    return "".join(parts).rstrip("\n")


class ArgsFilter(logging.Filter):
    """On the handler, so for every logger: exceptions among a record's arguments become class names
    (aiogram passes them, asyncio too); asyncio's multi-line reports keep their first line."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_name(a) if isinstance(a, BaseException) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _name(a) if isinstance(a, BaseException) else a for k, a in record.args.items()}
        if record.name.startswith("asyncio") and isinstance(record.msg, str) and "\n" in record.msg:
            record.msg, record.args = record.msg.split("\n", 1)[0], ()
        return True


def _default(value: object) -> str:
    return _name(value) if isinstance(value, BaseException) else str(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS})
        if record.exc_info and record.exc_info[1] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, ensure_ascii=False, default=_default))

    def formatException(self, ei) -> str:
        return safe_exc(ei)


class JsonHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        """A record that could not be written: its logger and the error's class, never its message or
        arguments, which the standard handler would print as they are."""
        if logging.raiseExceptions and sys.stderr:
            sys.stderr.write(f"logging error in {record.name}: {_name(sys.exc_info()[1])}\n")


def _uncaught(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logging.getLogger("nestor").critical("uncaught exception", exc_info=(exc_type, exc, tb))


def _uncaught_in_thread(args: threading.ExceptHookArgs) -> None:
    if not issubclass(args.exc_type, SystemExit):
        _uncaught(args.exc_type, args.exc_value, args.exc_traceback)


def setup_logging(level: str = "INFO") -> None:
    handler = JsonHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ArgsFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    sys.excepthook, threading.excepthook = _uncaught, _uncaught_in_thread
