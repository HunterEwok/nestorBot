"""The command menu Telegram shows when someone types «/» (setMyCommands).

Without it the bot handles every command but the client suggests none. Members of a group see
their own commands, the group's admins the admin ones as well, and so does an owner (OWNER_IDS)
in every allowed group, admin or not. /status is left out on purpose (nestor.bot.triggers).
"""
from __future__ import annotations

import logging
from typing import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeAllGroupChats,
                           BotCommandScopeAllPrivateChats, BotCommandScopeChatMember)

log = logging.getLogger("nestor.bot")

MEMBER = (
    ("catchup", "что я пропустил"),
    ("summary", "краткое содержание, например /summary неделя"),
    ("stats", "активность и темы недели"),
    ("reminders", "мои напоминания"),
    ("unremind", "отменить напоминание, /unremind номер"),
    ("export", "мои сообщения файлом в личку"),
    ("privacy", "что хранится и куда уходит"),
    ("forget_me", "удалить мои сообщения из архива"),
    ("help", "что я умею"),
)
ADMIN = (
    ("pause", "остановить запись"),
    ("resume", "продолжить запись"),
    ("bot_word", "обращение «бот»: on или off"),
    ("digest", "дайджест: on, off или раз в N дней"),
    ("wisdom", "мудрость дня: on или off"),
    ("news", "новости: on, off или status"),
    ("reactions", "реакции: on или off"),
    ("address", "обращения своими словами: on или off"),
    ("chime", "мои реплики: on или off"),
    ("music", "восходящие артисты: on, off, now или status"),
)
PRIVATE = (
    ("export", "мои сообщения из архива файлом"),
    ("help", "что я умею"),
)


def _menu(pairs: Iterable[tuple[str, str]]) -> list[BotCommand]:
    return [BotCommand(command=c, description=d) for c, d in pairs]


async def publish(bot: Bot, chat_ids: Iterable[int], owner_ids: Iterable[int]) -> None:
    """Set the menus. Best effort: a failure is logged and the bot works on without suggestions."""
    admin = _menu(MEMBER + ADMIN)
    menus = [(_menu(MEMBER), BotCommandScopeAllGroupChats()), (admin, BotCommandScopeAllChatAdministrators()),
             (_menu(PRIVATE), BotCommandScopeAllPrivateChats())]
    # The owner in a group: Telegram refuses the scope when they are not in it, that group is just skipped.
    menus += [(admin, BotCommandScopeChatMember(chat_id=c, user_id=u)) for c in chat_ids for u in owner_ids]
    for commands, scope in menus:
        try:
            await bot.set_my_commands(commands, scope=scope)
        except TelegramAPIError as e:
            log.warning("command menu not set", extra={"scope": scope.type, "error": type(e).__name__})


async def clear(bot: Bot) -> None:
    """Archive only (no HERMES_URL): Telegram keeps a published menu until it is deleted, and the bot answers
    no command now. An owner's menu in a group is left; it is replaced as soon as answering is back."""
    for scope in (BotCommandScopeAllGroupChats(), BotCommandScopeAllChatAdministrators(),
                  BotCommandScopeAllPrivateChats()):
        try:
            await bot.delete_my_commands(scope=scope)
        except TelegramAPIError as e:
            log.warning("command menu not cleared", extra={"scope": scope.type, "error": type(e).__name__})
