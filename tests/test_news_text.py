"""nestor/news/text.py: entity decoding shared by every news adapter."""
from __future__ import annotations

import time

import pytest

from nestor.news.store import title_key
from nestor.news.text import ROUNDS, squash, unescape


@pytest.mark.parametrize("raw, plain", [
    ("It&#39;s", "It's"),                               # Reddit's own level
    ("It&amp;#39;s", "It's"),                           # a pasted title, escaped once more by the API
    ("For &amp;amp;#39;World Needs Love&amp;amp;#39;", "For 'World Needs Love'"),   # Reggaeville, seen live
    ("Eintritt &amp;amp;#8364;20 &amp;#64;name", "Eintritt €20 @name"),
    ("&amp;sect;ion", "§ion"),                          # a real double-escaped §
    ("&#x27;&#X27;&apos;&nbsp;", "''' "),
    ("&#0; &#1114112; &#99999999;", "� � &#99999999;"),   # out of range: U+FFFD; too long: not an entity
])
def test_unescape_decodes_every_level(raw, plain):
    assert unescape(raw) == plain


@pytest.mark.parametrize("raw", [
    "&regex; &sections; &copyright; &ampx;",            # unknown names with a known head
    "?a=1&section=2&para=3&copy2026 &reg=1 &amp",        # no semicolon: not an entity
    "R&D; Q&A; AT&T; & alone, a &; and &#; and &#x;",
])
def test_unescape_leaves_what_is_not_a_complete_entity(raw):
    assert unescape(raw) == raw


def test_unescape_rounds_are_capped():
    nested = "&" + "amp;" * 10 + "#39;"
    assert unescape(nested) == "&" + "amp;" * (10 - ROUNDS) + "#39;"
    assert unescape("&amp;amp;#39;", rounds=1) == "&amp;#39;"   # links take exactly one level


def test_unescape_is_cheap_on_hostile_input():
    text = "&amp;" * 500_000 + "&" * 100_000 + "&#" * 100_000
    start = time.perf_counter()
    unescape(text)
    assert time.perf_counter() - start < 3


def test_squash_drops_invisible_characters_and_collapses_whitespace():
    assert squash("  a&#x200B;b­ c \x00\n\t d&amp;nbsp;e ") == "ab c d e"
    assert squash("") == ""


def test_title_key_is_the_same_once_entities_are_gone():
    # a leftover "&#39;" became the token " 39 " and kept the same story from two outlets apart
    reggaeville = squash("Lake Anthony Unites Global Reggae Artists For &amp;amp;#39;World Needs Love&amp;amp;#39;")
    feed = "Lake Anthony Unites Global Reggae Artists For 'World Needs Love'"
    assert title_key(reggaeville) == title_key(feed) is not None
