"""Telethon (MTProto) objects -> Bot API-shaped dicts, so imported history goes
through the same parse_message/save_message path as live updates."""
from __future__ import annotations

import re
from typing import Any

from telethon import utils
from telethon.tl import types as t

IMPORT_SOURCE = "telegram_import"

# MTProto action class -> Bot API service message key
_ACTIONS = {
    t.MessageActionChatAddUser: "new_chat_members",
    t.MessageActionChatJoinedByLink: "new_chat_members",
    t.MessageActionChatJoinedByRequest: "new_chat_members",
    t.MessageActionChatDeleteUser: "left_chat_member",
    t.MessageActionChatEditTitle: "new_chat_title",
    t.MessageActionChatEditPhoto: "new_chat_photo",
    t.MessageActionChatDeletePhoto: "delete_chat_photo",
    t.MessageActionChatCreate: "group_chat_created",
    t.MessageActionChannelCreate: "supergroup_chat_created",
    t.MessageActionChatMigrateTo: "migrate_to_chat_id",
    t.MessageActionChannelMigrateFrom: "migrate_from_chat_id",
    t.MessageActionPinMessage: "pinned_message",
    t.MessageActionTopicCreate: "forum_topic_created",
    t.MessageActionTopicEdit: "forum_topic_edited",
    t.MessageActionSetMessagesTTL: "message_auto_delete_timer_changed",
    t.MessageActionGroupCall: "video_chat_started",
    t.MessageActionGroupCallScheduled: "video_chat_scheduled",
    t.MessageActionInviteToGroupCall: "video_chat_participants_invited",
    t.MessageActionBoostApply: "boost_added",
}

_ENTITIES = {
    t.MessageEntityMention: "mention",
    t.MessageEntityHashtag: "hashtag",
    t.MessageEntityCashtag: "cashtag",
    t.MessageEntityBotCommand: "bot_command",
    t.MessageEntityUrl: "url",
    t.MessageEntityEmail: "email",
    t.MessageEntityPhone: "phone_number",
    t.MessageEntityBold: "bold",
    t.MessageEntityItalic: "italic",
    t.MessageEntityUnderline: "underline",
    t.MessageEntityStrike: "strikethrough",
    t.MessageEntitySpoiler: "spoiler",
    t.MessageEntityCode: "code",
    t.MessageEntityPre: "pre",
    t.MessageEntityTextUrl: "text_link",
    t.MessageEntityMentionName: "text_mention",
    t.MessageEntityCustomEmoji: "custom_emoji",
    t.MessageEntityBlockquote: "blockquote",
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def chat_dict(entity: Any) -> dict:
    if isinstance(entity, t.Channel):
        kind = "supergroup" if entity.megagroup or entity.gigagroup else "channel"
        return {"id": utils.get_peer_id(entity), "type": kind, "title": entity.title,
                "is_forum": bool(entity.forum)}
    if isinstance(entity, t.Chat):
        return {"id": utils.get_peer_id(entity), "type": "group", "title": entity.title}
    raise ValueError(f"not a group: {type(entity).__name__}")


def user_dict(user: t.User) -> dict:
    d: dict[str, Any] = {"id": user.id, "is_bot": bool(user.bot), "first_name": user.first_name or ""}
    if user.last_name:
        d["last_name"] = user.last_name
    if user.username:
        d["username"] = user.username
    return d


def _entities(entities: list | None) -> list[dict] | None:
    if not entities:
        return None
    out = []
    for e in entities:
        d = {"type": _ENTITIES.get(type(e), _snake(type(e).__name__.removeprefix("MessageEntity"))),
             "offset": e.offset, "length": e.length}
        if isinstance(e, t.MessageEntityTextUrl):
            d["url"] = e.url
        elif isinstance(e, t.MessageEntityMentionName):
            d["user"] = {"id": e.user_id}
        elif isinstance(e, t.MessageEntityPre) and e.language:
            d["language"] = e.language
        out.append(d)
    return out


def _media_kind(media: Any) -> str | None:
    """Bot API content key for the media. MTProto file ids are not Bot API file_ids,
    so only the kind is kept; media can be fetched later by (chat_id, message_id)."""
    if media is None or isinstance(media, (t.MessageMediaEmpty, t.MessageMediaWebPage)):
        return None
    if isinstance(media, t.MessageMediaPhoto):
        return "photo"
    if isinstance(media, t.MessageMediaDocument):
        if media.voice:
            return "voice"
        if media.round:
            return "video_note"
        attrs = getattr(media.document, "attributes", None) or []
        if any(isinstance(a, t.DocumentAttributeSticker) for a in attrs):
            return "sticker"
        if any(isinstance(a, t.DocumentAttributeAnimated) for a in attrs):
            return "animation"
        if media.video or any(isinstance(a, t.DocumentAttributeVideo) for a in attrs):
            return "video"
        if any(isinstance(a, t.DocumentAttributeAudio) for a in attrs):
            return "audio"
        return "document"
    return {
        t.MessageMediaPoll: "poll",
        t.MessageMediaGeo: "location",
        t.MessageMediaGeoLive: "location",
        t.MessageMediaVenue: "venue",
        t.MessageMediaContact: "contact",
        t.MessageMediaDice: "dice",
        t.MessageMediaGame: "game",
        t.MessageMediaStory: "story",
    }.get(type(media), "document")


def to_bot_api(msg: t.Message | t.MessageService, chat: dict, sender: Any = None) -> dict:
    """Build a Bot API Message dict. `sender` is the resolved User/Channel (msg.sender)."""
    d: dict[str, Any] = {
        "message_id": msg.id,
        "date": int(msg.date.timestamp()),
        "chat": chat,
        "import_source": IMPORT_SOURCE,
    }
    if isinstance(sender, t.User):
        d["from"] = user_dict(sender)
    elif isinstance(sender, (t.Channel, t.Chat)):
        d["sender_chat"] = {"id": utils.get_peer_id(sender), "type": "channel", "title": sender.title}
    elif isinstance(msg.from_id, t.PeerUser):
        d["from"] = {"id": msg.from_id.user_id, "is_bot": False, "first_name": "", "partial": True}

    reply = msg.reply_to
    if isinstance(reply, t.MessageReplyHeader) and reply.reply_to_msg_id and not reply.reply_to_peer_id:
        if reply.forum_topic:
            d["is_topic_message"] = True
            d["message_thread_id"] = reply.reply_to_top_id or reply.reply_to_msg_id
        d["reply_to_message"] = {"message_id": reply.reply_to_msg_id}
        if reply.quote_text:
            d["quote"] = {"text": reply.quote_text}

    if msg.edit_date and not getattr(msg, "edit_hide", False):
        d["edit_date"] = int(msg.edit_date.timestamp())

    if isinstance(msg, t.MessageService):
        action = msg.action
        key = _ACTIONS.get(type(action), _snake(type(action).__name__.removeprefix("MessageAction")))
        # Service payloads carry only ids here; keep them under import_action so that
        # parse_message records the event without overwriting known user names.
        d[key] = True
        d["import_action"] = action.to_dict()
        d["import_action"].pop("_", None)
        if isinstance(action, t.MessageActionChatEditTitle):
            d["new_chat_title"] = action.title
        elif isinstance(action, t.MessageActionChatMigrateTo):
            d["migrate_to_chat_id"] = utils.get_peer_id(t.PeerChannel(action.channel_id))
        elif isinstance(action, t.MessageActionChannelMigrateFrom):
            d["migrate_from_chat_id"] = utils.get_peer_id(t.PeerChat(action.chat_id))
        return d

    fwd = msg.fwd_from
    if fwd is not None:
        origin: dict[str, Any] = {"type": "hidden_user", "date": int(fwd.date.timestamp())}
        if fwd.from_name:
            origin["sender_user_name"] = fwd.from_name
        if fwd.from_id is not None:
            origin["type"] = "user" if isinstance(fwd.from_id, t.PeerUser) else "chat"
            origin["sender_id"] = utils.get_peer_id(fwd.from_id)
        d["forward_origin"] = origin

    if msg.grouped_id:
        d["media_group_id"] = str(msg.grouped_id)

    kind = _media_kind(msg.media)
    text = msg.message or None
    entities = _entities(msg.entities)
    if kind is None:
        d["text"] = text or ""
        if entities:
            d["entities"] = entities
    else:
        d[kind] = {}
        if text:
            d["caption"] = text
        if entities:
            d["caption_entities"] = entities
    return d
