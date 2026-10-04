"""Model Markdown -> Telegram HTML: formatting, escaping, links and references to archive messages."""
import re
import time

import pytest

from nestor.bot.markup import (Ref, chunk_lines, linked_ids, quote, ref_ids, render, split_text, to_html,
                               to_plain, visible_len)

CHAT = -1001000000001
REFS = {37285: Ref("https://t.me/c/1000000001/37285", "«налоги надо снизить»"), 12: Ref(None, "«в старой группе»"),
        120: Ref(None, "«в старой группе»"), 1: Ref("https://t.me/c/1000000001/1", "«привет всем»")}


@pytest.mark.parametrize("source, html", [
    ("**Итог:** Петя прав", "<b>Итог:</b> Петя прав"),
    ("*оттенок* и `/help`", "<i>оттенок</i> и <code>/help</code>"),
    ("~~нет~~ и ||спойлер||", "<s>нет</s> и <tg-spoiler>спойлер</tg-spoiler>"),
    ("***оба***", "<b><i>оба</i></b>"),
    ("**жирный с *курсивом* внутри**", "<b>жирный с <i>курсивом</i> внутри</b>"),
    ("- первый\n- второй\n  - вложенный", "• первый\n• второй\n  ◦ вложенный"),
    ("* звёздочкой\n+ плюсом\n• точкой", "• звёздочкой\n• плюсом\n• точкой"),
    ("> цитата\n> вторая строка\nпосле", "<blockquote>цитата\nвторая строка</blockquote>\nпосле"),
    ("> 1\n> 2\n> 3\n> 4\n> 5", "<blockquote expandable>1\n2\n3\n4\n5</blockquote>"),
    ("## Заголовок **жирный**", "<b>Заголовок жирный</b>"),
    ("текст\n\n---\n\n\nдальше", "текст\n\nдальше"),
    ("| a | b |\n|---|:---:|\n| 1 | 2 |", "a — b\n\n1 — 2"),
    ("```python\nprint('<x>')\n```\nпосле", "<pre>print('&lt;x&gt;')</pre>\nпосле"),
    ("```незакрытый\nкод", "<pre>код</pre>"),
    ("```\n```", ""),
    ("## Итог ##", "<b>Итог</b>"),
    ("## Язык C#", "<b>Язык C#</b>"),
    ("||ответ: 42||", "<tg-spoiler>ответ: 42</tg-spoiler>"),             # a whole-line spoiler is not a table
    ("<script>alert(1)</script> & co", "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co"),
    ("2*3*4, a_b_c, 2**3 и a ** b", "2*3*4, a_b_c, 2**3 и a ** b"),   # not markup
    ("**незакрытый и *одна", "незакрытый и *одна"),                    # a stray ** is dropped
    # A reference at the start of a line, not a heading:
    ("#37285 в начале", '<a href="https://t.me/c/1000000001/37285">«налоги надо снизить»</a> в начале'),
])
def test_markdown(source, html):
    assert to_html(source, REFS, CHAT) == html


def test_links_show_their_domain_unless_the_label_does():
    assert to_html("[Reuters](https://www.reuters.com/a?b=1&c=2)") == \
        '<a href="https://www.reuters.com/a?b=1&amp;c=2">Reuters</a> (reuters.com)'
    assert to_html("[reuters.com](https://reuters.com/x)") == '<a href="https://reuters.com/x">reuters.com</a>'
    assert to_html("[Сбербанк](https://evil.example/login)").endswith("</a> (evil.example)")
    assert to_html("[вики](https://en.wikipedia.org/wiki/Foo_(bar)) конец") == \
        '<a href="https://en.wikipedia.org/wiki/Foo_(bar)">вики</a> (en.wikipedia.org) конец'
    assert to_html('[x](https://a.example/"onmouseover=1)') == \
        '<a href="https://a.example/&quot;onmouseover=1">x</a> (a.example)'
    # The label must be the domain itself, not merely contain it; a hidden label shows nothing.
    assert to_html("[sberbank.ru](https://rbank.ru/login)").endswith("</a> (rbank.ru)")
    assert to_html("[https://google.com/?r=evil.com](https://evil.com/x)").endswith("</a> (evil.com)")
    assert to_html("[login.evil.com.google.com](https://evil.com/x)").endswith("</a> (evil.com)")
    assert to_html("[Сбер ||rbank.ru||](https://rbank.ru/login)") == \
        '<a href="https://rbank.ru/login">Сбер rbank.ru</a> (rbank.ru)'
    assert to_html("[||rbank.ru||](https://rbank.ru/login)") == '<a href="https://rbank.ru/login">rbank.ru</a>'
    assert to_html("[https://www.Reuters.com/a](https://reuters.com/a)") == \
        '<a href="https://reuters.com/a">https://www.Reuters.com/a</a>'
    # Addresses browsers and Python read differently, or broken ones: the label alone, no crash.
    for target in (r"https://evil.com\@sberbank.ru/login", "https://a.com@b.com/x", "https://evil%2Ecom/x",
                   "https://rbc.ru[1]", "https://example.com]", "https://[localhost]/"):
        assert to_html(f"[Сбербанк]({target}) конец") == "Сбербанк конец", target
    # Bare addresses stay text: Telegram links them itself, markup inside them is not touched.
    assert to_html("см. https://example.com/a*b*c_d") == "см. https://example.com/a*b*c_d"


@pytest.mark.parametrize("source", ["[фишинг](javascript:alert(1))", "[фишинг](tg://user?id=1)",
                                    "[фишинг](file:///etc/passwd)"])
def test_other_schemes_keep_the_label_only(source):
    assert to_html(source) == "фишинг"


def test_references_to_archive_messages():
    # A quote written by the model, linked by code.
    assert to_html("Петя: [«налоги надо снизить»](#37285)", REFS, CHAT) == \
        'Петя: <a href="https://t.me/c/1000000001/37285">«налоги надо снизить»</a>'
    # A bare number or a label that says nothing gets the message's own words.
    assert to_html("как в #37285 и [сообщение](#37285)", REFS, CHAT) == \
        'как в <a href="https://t.me/c/1000000001/37285">«налоги надо снизить»</a> и ' \
        '<a href="https://t.me/c/1000000001/37285">«налоги надо снизить»</a>'
    # The same message twice, as a number and a link: one quote.
    assert to_html("Это было в #37285 (https://t.me/c/1000000001/37285).", REFS, CHAT) == \
        'Это было в <a href="https://t.me/c/1000000001/37285">«налоги надо снизить»</a>.'
    assert to_html("см. https://t.me/c/1000000001/37285, там", REFS, CHAT) == \
        'см. <a href="https://t.me/c/1000000001/37285">«налоги надо снизить»</a>, там'
    assert to_html("[тут](https://t.me/c/1000000001/37285)", REFS, CHAT) == \
        '<a href="https://t.me/c/1000000001/37285">тут</a>'
    # No t.me link (a basic group): the words without a link.
    assert to_html("[«слова»](#12) и #120", REFS, CHAT) == "«слова» и «в старой группе»"
    # A bare number under 100 counts things, it is not a message; the [«…»](#1) form always links.
    assert to_html("Проблема #1 и #12, а Петя: [«привет»](#1)", REFS, CHAT) == \
        'Проблема #1 и #12, а Петя: <a href="https://t.me/c/1000000001/1">«привет»</a>'
    # Unknown to the archive (deleted, author used /forget_me, invented): no link, no quote.
    assert to_html("[«что-то»](#99) и #99", REFS, CHAT) == "«что-то» и #99"
    assert to_html("[тут](https://t.me/c/1000000001/99)", REFS, CHAT) == "тут"
    # Another chat's message: the label alone; its bare address stays text.
    assert to_html("[чужое](https://t.me/c/999/5) и #37285 (https://t.me/c/999/37285)", REFS, CHAT) == \
        'чужое и <a href="https://t.me/c/1000000001/37285">«налоги надо снизить»</a> (https://t.me/c/999/37285)'
    # Not references: anchors in addresses, HTML entities.
    assert to_html("https://x.example/#37285 и page#37285 и &#37285;", REFS, CHAT) == \
        "https://x.example/#37285 и page#37285 и &amp;#37285;"


def test_ref_ids():
    text = "a #500 b https://t.me/c/1000000001/7 c #500 [x](#9) #12 https://x.example/#300 page#400 " \
           "https://t.me/c/999/8"
    assert ref_ids(text, CHAT) == [500, 7, 9]
    assert ref_ids(text, None) == [500, 9]
    assert ref_ids(" ".join(f"#{i}" for i in range(100, 200)), CHAT) == list(range(100, 150))


def test_quote():
    assert quote("  налоги\nнадо   снизить ") == "«налоги надо снизить»"
    assert quote(None) == quote("") == "сообщение"
    long = quote("слово " * 30)
    assert long.endswith("…»") and len(long) <= 64


def test_render_keeps_every_part_within_the_limit():
    parts = render("слово " * 1500)
    assert len(parts) == 3 and all(visible_len(p) <= 4096 for p in parts)
    # Quotes make the visible text longer than the source: parts are split again.
    refs = {i: Ref(f"https://t.me/c/1000000001/{i}", "«" + "ц" * 58 + "»") for i in range(1, 51)}
    text = "\n".join(f"- пункт [#{i}](#{i}) " + "х" * 20 for i in range(1, 51)) * 3
    parts = render(text, refs, CHAT)
    assert all(visible_len(p) <= 4096 for p in parts) and len(parts) >= 2
    assert render("") == [] and render("```\n```") == []


def test_render_splits_between_lines_and_keeps_code_blocks_whole():
    # Lines are kept whole, so a quote-link never breaks in two.
    refs = {i: Ref(f"https://t.me/c/1000000001/{i}", "«x»") for i in range(1, 41)}
    lines = [f"- 📌 **Тема номер {i}**: Петя написал [«давайте поедем на дачу в субботу утром»](#{i}), все за."
             for i in range(1, 41)]
    for intro in range(0, 140, 7):
        parts = render("Вступление " + "а" * intro + "\n" + "\n".join(lines), refs, CHAT, limit=1000)
        assert len(parts) >= 2
        assert all("](" not in p and "**" not in p and visible_len(p) <= 1000 for p in parts)
    # A long code block cut in two stays code in both parts; the text after it is not code.
    code = "\n".join(f"step {i}: compute *x* and **y** # {i}" for i in range(120))
    parts = render("Скрипт:\n\n```python\n" + code + "\n```\n\nИ **итог**: работает.", limit=2000)
    assert len(parts) >= 3
    assert all(p.startswith("<pre>") or p.startswith("Скрипт") for p in parts[:-1])
    assert parts[-1].endswith("И <b>итог</b>: работает.") and "<b>y</b>" not in "".join(parts)


def test_headings_never_take_long():
    started = time.monotonic()
    assert to_html("# a" + " " * 5000 + "b") == "<b>a" + " " * 5000 + "b</b>"
    assert time.monotonic() - started < 1


def test_to_plain_and_visible_len():
    html = to_html("**Итог** & [x](https://a.example) 😀")
    assert to_plain(html) == "Итог & x (a.example) 😀"
    assert visible_len(html) == len("Итог & x (a.example) ") + 2    # an emoji is two UTF-16 units


def test_the_bots_own_texts_are_well_formed_telegram_html():
    from html.parser import HTMLParser

    from nestor.bot import prompts

    allowed = {"b", "i", "u", "s", "a", "code", "pre", "blockquote", "tg-spoiler"}

    class Check(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.open: list[str] = []

        def handle_starttag(self, tag, attrs):
            assert tag in allowed, tag
            self.open.append(tag)

        def handle_endtag(self, tag):
            assert self.open and self.open.pop() == tag, tag

    texts = {name: value for name, value in vars(prompts).items()
             if name.isupper() and isinstance(value, str) and not name.endswith(("SYSTEM", "FORMAT", "BREVITY"))
             and name not in ("WEB_MARKER", "DIGEST_HEADER")}
    assert {"HELP", "PRIVACY", "PAUSED", "REACTIONS_STATE"} <= set(texts)
    for name, text in texts.items():
        check = Check()
        check.feed(text)
        assert check.open == [], name
        assert not re.search(r"&(?!amp;|lt;|gt;|quot;)", text), name
    help_text = prompts.HELP.format(username="ewok_nestor_bot")
    assert "<b>Команды</b>" in help_text and "/reactions on|off" in help_text
    assert to_plain(help_text).startswith("👋 Я Нестор: молча веду архив")


def test_a_one_line_code_block_does_not_open_a_block_for_the_splitter():
    lines = [f"- **Пункт {i}**: проверь [доку](https://docs.python.org/3/) и *всё* остальное…" for i in range(80)]
    parts = render("Запусти так:\n```ls -la```\n" + "\n".join(lines), {}, CHAT)
    assert len(parts) >= 2 and parts[0].startswith("Запусти так:\n<pre>ls -la</pre>\n• <b>Пункт 0</b>")
    assert all("<pre>" not in p and "**" not in p and "](" not in p for p in parts[1:])
    # A one-line block before a long real block: the real block stays code in every part.
    code = "\n".join(f"    x_{i} = compute(*args)  # step {i}" for i in range(200))
    parts = render("```pip install x```\n\n```python\n" + code + "\n```\n\nИтог: **готово**.", limit=2000)
    assert parts[0].startswith("<pre>pip install x</pre>\n\n<pre>    x_0 = compute(*args)")
    assert all(p.startswith("<pre>    x_") for p in parts[1:-1]) and parts[-1].endswith("Итог: <b>готово</b>.")
    # A block continued in the next part keeps the indentation of its first line.
    assert all("<pre>    x_" in p for p in parts[:-1])


def test_a_long_paragraph_is_not_cut_inside_a_link_or_a_bold_span():
    refs = {i: Ref(f"https://t.me/c/1000000001/{i}", "«x»") for i in range(1, 41)}
    text = " ".join(f"**Петя {i} сказал** [«давайте поедем на дачу в субботу утром»](#{i % 40 + 1})"
                    for i in range(300))
    parts = render(text, refs, CHAT)
    assert len(parts) >= 2 and all("](" not in p and "**" not in p and "[«" not in p for p in parts)
    assert all(visible_len(p) <= 4096 for p in parts)
    # The last space before the limit is inside a label: the cut moves back to the one before it.
    assert split_text("слово " * 10 + "[«длинная цитата из сообщения»](#5) хвост", limit=75) == [
        "слово слово слово слово слово слово слово слово слово слово", "[«длинная цитата из сообщения»](#5) хвост"]
    # A nested list item that starts the next part keeps its depth, also when a part is split
    # again because its HTML (quotes for bare #ids) outgrew the source.
    items = "\n".join(f"- пункт {i}\n  - вложенный {i}" for i in range(60))
    parts = render(items, limit=800)
    assert len(parts) >= 2 and all(not p.startswith("• вложенный") for p in parts)
    refs = {i: Ref(f"https://t.me/c/1000000001/{i}", "«" + "ц" * 58 + "»") for i in range(100, 300)}
    items = "\n".join(f"- пункт {i}\n  - вложенный #{100 + i} и #{101 + i} и #{102 + i}" for i in range(60))
    parts = render(items, refs, CHAT, limit=1000)
    assert len(parts) >= 4 and all(visible_len(p) <= 1000 for p in parts)
    assert all(not p.startswith("• вложенный") for p in parts) and "  ◦ вложенный" in parts[1]
    assert split_text("   Привет\nмир", 100) == ["Привет\nмир"]        # other leading space still goes


def test_splitting_a_long_answer_full_of_unclosed_bold_stays_quick():
    # The span scan around a cut is bounded: 50 000 characters of «**a » took half a minute before.
    started = time.perf_counter()
    parts = render("**a " * 12_500)
    assert len(parts) == 13 and time.perf_counter() - started < 5
    started = time.perf_counter()
    parts = render(" ".join(f"**Пункт {i}: Петя сказал, что налоги надо снизить" for i in range(1000)))
    assert len(parts) == 13 and time.perf_counter() - started < 5


def test_adjacent_spans_and_spoilers_in_tables():
    assert to_html("**a** и **b**") == "<b>a</b> и <b>b</b>"
    assert to_html("||a|| и ||b||") == "<tg-spoiler>a</tg-spoiler> и <tg-spoiler>b</tg-spoiler>"
    assert to_html("~~a~~ и ~~b~~") == "<s>a</s> и <s>b</s>"
    assert to_html("| Кто | Ответ |\n|---|---|\n| Убийца | ||дворецкий|| |") == \
        "Кто — Ответ\n\nУбийца — <tg-spoiler>дворецкий</tg-spoiler>"
    assert to_html("| ответ: ||42|| |") == "ответ: <tg-spoiler>42</tg-spoiler>"
    assert to_html("| `a|b` | [x](https://a.example/a|b) |") == \
        '<code>a|b</code> — <a href="https://a.example/a|b">x</a> (a.example)'
    # A line that begins with a spoiler and ends with a «|» is not a table row: the pipes of a
    # span are not cell borders, and «||» alone is text.
    assert to_html("||секрет|| текст a|b ||ещё||") == \
        "<tg-spoiler>секрет</tg-spoiler> текст a|b <tg-spoiler>ещё</tg-spoiler>"
    assert to_html("|ответ: ||42||") == "|ответ: <tg-spoiler>42</tg-spoiler>"
    assert [to_html(s) for s in ("|| не спойлер ||", "|||", "||")] == ["|| не спойлер ||", "|||", "||"]


def test_look_alike_hosts_and_direction_overrides():
    # A host that mixes scripts is shown as the address bar shows it; «сбер.рф» stays readable.
    assert to_html("[Войти в Сбербанк](https://sbеrbank.ru/login)") == \
        '<a href="https://sbеrbank.ru/login">Войти в Сбербанк</a> (xn--sbrbank-8gg.ru)'
    assert to_html("[sbеrbank.ru](https://sbеrbank.ru/login)") == \
        '<a href="https://sbеrbank.ru/login">sbеrbank.ru</a> (xn--sbrbank-8gg.ru)'
    assert to_html("[Сбер](https://сбер.рф/x)") == '<a href="https://сбер.рф/x">Сбер</a> (сбер.рф)'
    assert to_html("[сбер.рф](https://сбер.рф/x)") == '<a href="https://сбер.рф/x">сбер.рф</a>'
    # A whole label of Cyrillic letters that look like Latin ones under a Latin TLD, or a Cyrillic
    # TLD that does not exist: the xn-- form, as in the address bar. Real words stay readable.
    assert to_html("[Apple Store](https://аррӏе.com/x)") == \
        '<a href="https://аррӏе.com/x">Apple Store</a> (xn--80ak6aa92e.com)'
    assert to_html("[аррӏе.com](https://аррӏе.com/x)") == \
        '<a href="https://аррӏе.com/x">аррӏе.com</a> (xn--80ak6aa92e.com)'
    assert to_html("[PayPal](https://paypal.сом/)") == '<a href="https://paypal.сом/">PayPal</a> (paypal.xn--l1adi)'
    assert to_html("[Яндекс](https://яндекс.com/)") == '<a href="https://яндекс.com/">Яндекс</a> (яндекс.com)'
    # IDNA 2008, as browsers read it: «straße» is not «strasse». Invisible characters: the label alone.
    assert to_html("[сайт](https://straße.de/x)") == '<a href="https://straße.de/x">сайт</a> (straße.de)'
    assert to_html("[faß.de](https://faß.de/x)") == '<a href="https://faß.de/x">faß.de</a>'
    hidden = "[сайт](https://ab\u200dcd.com/x) и [сайт](https://a\u00adb.com/) и [сайт](https://cafe\u0301.fr/)"
    assert to_html(hidden) == "сайт и сайт и сайт"
    # A mark that makes a Latin letter look like another («ı» with a dot above is «i»): xn--.
    assert to_html("[Почта](https://gmaı\u0307l.com/)") == \
        '<a href="https://gmaı\u0307l.com/">Почта</a> (xn--gmal-nza43z.com)'
    assert to_html("[сайт](https://हिन्दी.भारत/)") == '<a href="https://हिन्दी.भारत/">сайт</a> (हिन्दी.भारत)'
    # A right-to-left override in a label would show the domain hint reversed: dropped.
    assert to_html("[Госуслуги\u202e](https://evil.com/x) и \u2066текст\u2069") == \
        '<a href="https://evil.com/x">Госуслуги</a> (evil.com) и текст'


def test_bare_numbers_are_references_only_when_the_model_saw_them():
    refs = {1234: Ref("https://t.me/c/1000000001/1234", "«купил хлеб»"), 500: Ref(None, "«x»")}
    assert ref_ids("issue #1234 и #500 и #99 и [x](#9)", CHAT) == [1234, 500, 9]
    assert ref_ids("issue #1234 (https://t.me/c/1000000001/1234)", CHAT) == [1234]
    assert linked_ids("см. https://t.me/c/1000000001/7 и https://t.me/c/999/8 и #9", CHAT) == {7}
    assert linked_ids("https://t.me/c/1000000001/7", -4242) == set()
    # Unresolved, a bare number stays a number; the explicit form is always a reference.
    assert to_html("issue #1234 и [«купил хлеб»](#1234)", {}, CHAT) == "issue #1234 и «купил хлеб»"
    assert to_html("issue #1234", refs, CHAT) == 'issue <a href="https://t.me/c/1000000001/1234">«купил хлеб»</a>'
    refs[1234] = Ref(refs[1234].url, refs[1234].quote, bare=False)
    assert to_html("issue #1234 и [«купил хлеб»](#1234)", refs, CHAT) == \
        'issue #1234 и <a href="https://t.me/c/1000000001/1234">«купил хлеб»</a>'


def test_chunk_lines():
    assert chunk_lines([], 10) == [[]]
    assert chunk_lines(["a" * 6, "b" * 6, "c" * 6, "d" * 20], 13) == [["aaaaaa", "bbbbbb"], ["cccccc"], ["d" * 20]]
    assert chunk_lines(["<b>x</b>", "y"], 3) == [["<b>x</b>", "y"]]
