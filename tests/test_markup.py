"""Model Markdown -> Telegram HTML: formatting, escaping, links and references to archive messages."""
import pytest

from nestor.bot.markup import Ref, quote, ref_ids, render, to_html, to_plain, visible_len

CHAT = -1001000000001
REFS = {37285: Ref("https://t.me/c/1000000001/37285", "«налоги надо снизить»"), 12: Ref(None, "«в старой группе»")}


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
    assert to_html("[«слова»](#12) и #12", REFS, CHAT) == "«слова» и «в старой группе»"
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
    text = "a #5 b https://t.me/c/1000000001/7 c #5 [x](#9) https://x.example/#3 page#4 https://t.me/c/999/8"
    assert ref_ids(text, CHAT) == [5, 7, 9]
    assert ref_ids(text, None) == [5, 9]
    assert ref_ids(" ".join(f"#{i}" for i in range(100)), CHAT) == list(range(50))


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


def test_to_plain_and_visible_len():
    html = to_html("**Итог** & [x](https://a.example) 😀")
    assert to_plain(html) == "Итог & x (a.example) 😀"
    assert visible_len(html) == len("Итог & x (a.example) ") + 2    # an emoji is two UTF-16 units
