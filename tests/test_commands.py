from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetMyCommands

from nestor.bot import commands


class MenuBot:
    def __init__(self, refuse=()):
        self.menus, self.refuse = [], refuse

    async def set_my_commands(self, cmds, scope=None, **kw):
        if scope.type in self.refuse:
            raise TelegramBadRequest(SetMyCommands(commands=cmds), "Bad Request: user not found")
        self.menus.append((scope, [c.command for c in cmds]))
        return True


async def test_menus_per_scope():
    bot = MenuBot()
    await commands.publish(bot, [-100, -200], [7])
    by_type = {}
    for scope, names in bot.menus:
        by_type.setdefault(scope.type, []).append((scope, names))
    group = by_type["all_group_chats"][0][1]
    admin = by_type["all_chat_administrators"][0][1]
    assert "catchup" in group and "pause" not in group and "status" not in group
    assert "pause" in admin and "news" in admin and set(group) <= set(admin)
    assert by_type["all_private_chats"][0][1] == ["export", "help"]
    owner = by_type["chat_member"]
    assert {(s.chat_id, s.user_id) for s, _ in owner} == {(-100, 7), (-200, 7)}
    assert all(names == admin for _, names in owner)
    # every name is a command the bot handles
    from nestor.bot.triggers import COMMANDS
    assert set(admin) <= COMMANDS


async def test_a_refused_scope_does_not_stop_the_rest():
    bot = MenuBot(refuse={"chat_member"})
    await commands.publish(bot, [-100], [7])
    assert {s.type for s, _ in bot.menus} == {"all_group_chats", "all_chat_administrators", "all_private_chats"}
