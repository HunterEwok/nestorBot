"""One-off import of group history written before the bot joined (Bot API cannot read it).

Runs under the owner's Telegram user account via MTProto (Telethon), on a laptop,
never on the Pi: the account session gives access to all of the owner's chats.
"""
