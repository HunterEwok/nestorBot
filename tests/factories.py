"""Bot API JSON builders for tests."""
from __future__ import annotations

import itertools

CHAT_ID = -1001000000001
FORUM_ID = -1001000000002
_update_ids = itertools.count(1)


def user(uid: int = 111, **kw) -> dict:
    return {"id": uid, "is_bot": False, "first_name": f"User{uid}", **kw}


def chat(chat_id: int = CHAT_ID, **kw) -> dict:
    return {"id": chat_id, "type": "supergroup", "title": "Test group", **kw}


def message(message_id: int, text: str | None = "привет", *, chat_id: int = CHAT_ID, uid: int = 111,
            date: int = 1_790_000_000, **kw) -> dict:
    msg = {"message_id": message_id, "date": date, "chat": chat(chat_id), "from": user(uid)}
    if text is not None:
        msg["text"] = text
    msg.update(kw)
    return msg


def update(kind: str, msg: dict) -> dict:
    return {"update_id": next(_update_ids), kind: msg}
