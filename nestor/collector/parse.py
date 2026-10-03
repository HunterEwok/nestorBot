"""Pure conversion of Bot API JSON (dicts) into archive rows. No I/O here."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Content keys in priority order; the first present one names the message kind.
MEDIA_KINDS = (
    "photo", "video", "animation", "audio", "voice", "video_note", "document",
    "sticker", "story", "poll", "location", "venue", "contact", "dice", "game",
    "paid_media", "invoice", "giveaway", "giveaway_winners",
)

# Service messages (FR-5). Stored with kind = 'service' and event = the key.
SERVICE_EVENTS = (
    "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo",
    "delete_chat_photo", "group_chat_created", "supergroup_chat_created",
    "message_auto_delete_timer_changed", "migrate_to_chat_id", "migrate_from_chat_id",
    "pinned_message", "forum_topic_created", "forum_topic_edited", "forum_topic_closed",
    "forum_topic_reopened", "general_forum_topic_hidden", "general_forum_topic_unhidden",
    "video_chat_scheduled", "video_chat_started", "video_chat_ended",
    "video_chat_participants_invited", "boost_added", "chat_background_set",
    "users_shared", "chat_shared", "write_access_allowed", "proximity_alert_triggered",
    "giveaway_created", "giveaway_completed", "successful_payment", "refunded_payment",
)

# Nested messages that carry someone else's full text. In raw we keep only a
# reference to them, so /forget_me does not have to hunt for copies (finding 16).
_NESTED_MESSAGE_KEYS = ("reply_to_message", "pinned_message")


@dataclass
class UserRow:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    is_bot: bool
    partial: bool = False  # only the id is known (history import): never overwrite a profile


@dataclass
class ChatRow:
    chat_id: int
    title: str | None
    type: str
    is_forum: bool


@dataclass
class MessageRow:
    chat_id: int
    message_id: int
    thread_id: int | None
    is_topic_message: bool
    user_id: int | None
    sender_chat_id: int | None
    sent_at: datetime
    reply_to_id: int | None
    kind: str
    event: str | None
    text: str | None
    entities: list[dict] | None
    forward_origin: dict | None
    media_group_id: str | None
    file_id: str | None
    file_unique_id: str | None
    edited_at: datetime | None
    raw: dict[str, Any]
    users: list[UserRow] = field(default_factory=list)  # everyone mentioned structurally


def ts(unix: int | None) -> datetime | None:
    return None if unix is None else datetime.fromtimestamp(unix, timezone.utc)


def parse_user(u: dict) -> UserRow:
    return UserRow(
        user_id=u["id"],
        username=u.get("username"),
        first_name=u.get("first_name"),
        last_name=u.get("last_name"),
        is_bot=bool(u.get("is_bot", False)),
        partial=bool(u.get("partial", False)),
    )


def parse_chat(c: dict) -> ChatRow:
    return ChatRow(chat_id=c["id"], title=c.get("title"), type=c["type"], is_forum=bool(c.get("is_forum", False)))


def _reference(nested: dict) -> dict:
    ref: dict[str, Any] = {"message_id": nested["message_id"]}
    if "date" in nested:
        ref["date"] = nested["date"]
    if "from" in nested:
        ref["from"] = {"id": nested["from"]["id"]}
    if "message_thread_id" in nested:
        ref["message_thread_id"] = nested["message_thread_id"]
    return ref


def reduce_raw(msg: dict) -> dict:
    raw = dict(msg)
    for key in _NESTED_MESSAGE_KEYS:
        if isinstance(raw.get(key), dict) and "message_id" in raw[key]:
            raw[key] = _reference(raw[key])
    return raw


def _kind_and_file(msg: dict) -> tuple[str, str | None, str | None, str | None]:
    """Returns (kind, event, file_id, file_unique_id)."""
    for event in SERVICE_EVENTS:
        if event in msg:
            return "service", event, None, None
    for kind in MEDIA_KINDS:
        if kind in msg:
            content = msg[kind]
            if kind == "photo" and isinstance(content, list) and content:
                content = content[-1]  # largest size
            if isinstance(content, dict) and "file_id" in content:
                return kind, None, content["file_id"], content.get("file_unique_id")
            return kind, None, None, None
    if "text" in msg:
        return "text", None, None, None
    return "other", None, None, None


def parse_message(msg: dict) -> MessageRow:
    kind, event, file_id, file_unique_id = _kind_and_file(msg)
    is_topic = bool(msg.get("is_topic_message", False))
    thread_id = msg.get("message_thread_id")

    reply_to_id = None
    reply = msg.get("reply_to_message")
    if reply:
        reply_to_id = reply["message_id"]
        # In forum topics every message "replies" to the topic's creation message;
        # that is not a real reply.
        if is_topic and thread_id is not None and reply_to_id == thread_id:
            reply_to_id = None

    users: list[UserRow] = []
    if "from" in msg:
        users.append(parse_user(msg["from"]))
    members = msg.get("new_chat_members")
    for member in members if isinstance(members, list) else []:
        users.append(parse_user(member))
    if isinstance(msg.get("left_chat_member"), dict):
        users.append(parse_user(msg["left_chat_member"]))

    return MessageRow(
        chat_id=msg["chat"]["id"],
        message_id=msg["message_id"],
        thread_id=thread_id,
        is_topic_message=is_topic,
        user_id=msg["from"]["id"] if "from" in msg else None,
        sender_chat_id=msg["sender_chat"]["id"] if "sender_chat" in msg else None,
        sent_at=ts(msg["date"]),
        reply_to_id=reply_to_id,
        kind=kind,
        event=event,
        text=msg.get("text", msg.get("caption")),
        entities=msg.get("entities", msg.get("caption_entities")),
        forward_origin=msg.get("forward_origin"),
        media_group_id=msg.get("media_group_id"),
        file_id=file_id,
        file_unique_id=file_unique_id,
        edited_at=ts(msg.get("edit_date")),
        raw=reduce_raw(msg),
        users=users,
    )
