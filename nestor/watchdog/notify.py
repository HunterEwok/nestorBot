"""Sending to the alert group: Telegram HTML without link previews, plain text if Telegram refuses it."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import LinkPreviewOptions

from ..bot import markup

log = logging.getLogger("nestor.watchdog")


async def send(bot: Bot, chat_id: int, text: str, *, silent: bool) -> bool:
    """Whether the message went out. Failures are logged by class name only."""
    async def go(body: str, html: bool) -> None:
        await bot.send_message(chat_id, body, parse_mode="HTML" if html else None, disable_notification=silent,
                               link_preview_options=LinkPreviewOptions(is_disabled=True))

    try:
        await go(text, True)
        return True
    except TelegramBadRequest:
        log.warning("alert formatting refused, sending plain text")
        try:
            await go(markup.to_plain(text), False)
            return True
        except TelegramAPIError as e:
            log.warning("alert send failed", extra={"error": type(e).__name__})
    except TelegramAPIError as e:
        log.warning("alert send failed", extra={"error": type(e).__name__})
    return False
