from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from nestor.bot.answerer import split_text
from nestor.bot.context import classify, message_link, period_start
from nestor.bot.triggers import BotIdentity, detect

from .factories import message

ME = BotIdentity(42, "ewok_nestor_bot", ("нестор", "бот"))


def mention(text, at):
    start = text.index(at)
    return [{"type": "mention", "offset": len(text[:start].encode("utf-16-le")) // 2, "length": len(at)}]


def test_mention_is_stripped_with_utf16_offsets():
    text = "😀 @ewok_nestor_bot оцени тезисы"
    inv = detect(message(1, text, entities=mention(text, "@ewok_nestor_bot")), ME)
    assert (inv.kind, inv.text) == ("mention", "😀 оцени тезисы")


def test_mention_of_someone_else_is_not_a_trigger():
    text = "@petya привет"
    assert detect(message(1, text, entities=mention(text, "@petya")), ME) is None


def test_keyword_at_start_only_and_case_insensitive():
    assert detect(message(1, "Нестор, кто прав?"), ME).text == "кто прав?"
    assert detect(message(1, "БОТ: о чём говорили"), ME).text == "о чём говорили"
    assert detect(message(1, "а нестор что думает?"), ME) is None
    assert detect(message(1, "ботинки купил"), ME) is None


def test_generic_bot_word_can_be_disabled():
    assert detect(message(1, "бот, привет"), ME, generic_keyword_enabled=False) is None
    assert detect(message(1, "нестор, привет"), ME, generic_keyword_enabled=False) is not None


def test_reply_to_bot_but_not_topic_root():
    bot_msg = message(5, "ответ бота", uid=42)
    assert detect(message(6, "а подробнее?", reply_to_message=bot_msg), ME).replied_to_bot == 5
    in_topic = message(7, "просто в теме", message_thread_id=5, is_topic_message=True, reply_to_message=bot_msg)
    assert detect(in_topic, ME) is None


def test_commands():
    assert detect(message(1, "/summary неделя"), ME).args == "неделя"
    assert detect(message(1, "/help@ewok_nestor_bot"), ME).command == "help"
    assert detect(message(1, "/help@other_bot"), ME) is None
    assert detect(message(1, "/unknown"), ME) is None


def test_split_text_respects_limit_and_paragraphs():
    text = ("абзац " * 300 + "\n\n") * 5
    parts = split_text(text, 4096)
    assert all(len(p) <= 4096 for p in parts) and len(parts) >= 2
    assert "".join(parts).replace(" ", "").replace("\n", "") == text.replace(" ", "").replace("\n", "")


def test_classify_and_period():
    assert classify("оцени тезисы Пети") == "theses"
    assert classify("кто из них прав?") == "who_is_right"
    assert classify("о чём говорили сегодня") == "summary"
    assert classify("кто и когда говорил про отпуск") == "search"
    assert classify("какая погода в Кирове?") == "web"
    assert classify("курс доллара сегодня") == "web"
    assert classify("кто писал про погоду?") == "search"   # about this chat: archive, not the web
    assert classify("найди в чате новости про ремонт") != "web"
    tz = ZoneInfo("Europe/Tallinn")
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    assert period_start("неделя", now, tz) == datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    assert period_start("за 3 часа", now, tz) == datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
    assert period_start("", now, tz).astimezone(tz).hour == 0


def test_links():
    assert message_link(-1001234567890, 5) == "https://t.me/c/1234567890/5"
    assert message_link(-1001234567890, 5, 3, True) == "https://t.me/c/1234567890/3/5"
    assert message_link(-12345, 5) is None
