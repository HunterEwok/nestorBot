"""Which profile does which task: defaults, fallbacks, and routes that would let chat text
reach the internet are refused."""
import pytest

from nestor.bot import routing
from nestor.bot.routing import Router, parse_routes, split_think


def test_defaults_and_fallbacks():
    full = Router({"group": "G", "deep": "D", "web": "W", "smart": "S", "news": "N"})
    assert [full.profile(t) for t in ("answer", "theses", "who_is_right", "think", "digest", "topics")] == [
        "group", "deep", "deep", "deep", "group", "group"]
    assert [full.profile(t) for t in ("web", "think_web", "factcheck", "music", "wisdom", "news_triage", "news")] == [
        "web", "smart", "web", "smart", "smart", "news", "smart"]
    assert full.is_big("theses") and not full.is_big("answer")
    assert (full.profile("music_check"), full.profile("music_trends")) == ("news", "smart")

    basic = Router({"group": "G", "web": "W", "smart": None})
    assert basic.llm("theses") == "G" and basic.llm("think") == "G"
    assert basic.llm("music") == "W" and basic.llm("news") == "W" and basic.llm("news_triage") == "W"
    assert not basic.is_big("music")

    archive_only = Router({"group": "G"})
    assert archive_only.llm("web") is None and archive_only.llm("news_triage") is None
    assert archive_only.llm("theses") == "G"

    smart_only = Router({"group": "G", "smart": "S"})
    assert smart_only.llm("news_triage") == "S" and smart_only.llm("music_check") == "S"
    assert smart_only.llm("web") is None      # plain web questions do not silently go to the expensive model


def test_owner_routes():
    routes = parse_routes(" theses:group, music:web,news:web ,news_triage:smart")
    assert routes == {"theses": "group", "music": "web", "news": "web", "news_triage": "smart"}
    r = Router({"group": "G", "deep": "D", "web": "W", "smart": "S"}, routes)
    assert (r.llm("theses"), r.llm("who_is_right"), r.llm("music"), r.llm("news")) == ("G", "D", "W", "W")
    assert parse_routes("") == {}


@pytest.mark.parametrize("value", [
    "theses:web",        # chat text to a profile with internet
    "summary:group",     # unknown task
    "answer:smart",
    "web:group",         # a web question to the archive profile
    "news:deep",         # public news to an archive profile
    "music:",
])
def test_routes_that_break_the_split_are_refused(value):
    with pytest.raises(ValueError):
        parse_routes(value)


@pytest.mark.parametrize("text, think, rest", [
    ("подумай, кто прав", True, "кто прав"),
    ("Подумай хорошенько: правда ли, что Земля плоская?", True, "правда ли, что Земля плоская?"),
    ("подумай как следует — оцени тезисы", True, "оцени тезисы"),
    ("подумайте", True, "подумайте"),          # a bare «подумай» keeps its words as the question
    ("  Подумай хорошенько!  ", True, "Подумай хорошенько!"),
    ("поразмысли о смысле", True, "о смысле"),
    ("подумаешь, важность", False, "подумаешь, важность"),
    ("кто прав?", False, "кто прав?"),
])
def test_split_think(text, think, rest):
    assert split_think(text) == (think, rest)


def test_reactions_use_the_profile_without_tools_and_never_fall_back():
    assert Router({"group": "G", "news": "N"}).llm("react") == "N"
    # Without `news` the code picks reactions: no silent move to the archive or the internet profiles.
    assert Router({"group": "G", "deep": "D", "web": "W", "smart": "S"}).llm("react") is None
    assert parse_routes("react:group") == {"react": "group"}
    assert Router({"group": "G", "news": "N"}, {"react": "group"}).llm("react") == "G"
    for value in ("react:web", "react:smart", "react:deep"):
        with pytest.raises(ValueError):
            parse_routes(value)


def test_address_checks_use_the_profile_without_tools_and_never_fall_back(monkeypatch):
    assert Router({"group": "G", "news": "N"}).llm("address") == "N"
    # Without `news` nobody is asked and nothing is answered: no move to the archive or the internet profiles.
    assert Router({"group": "G", "deep": "D", "web": "W", "smart": "S"}).llm("address") is None
    assert Router({"group": "G", "news": "N"}, parse_routes("address:group")).llm("address") == "G"
    for value in ("address:web", "address:smart", "address:deep"):
        with pytest.raises(ValueError):
            parse_routes(value)
    # Not even if `news` fell back to the archive profile one day.
    monkeypatch.setitem(routing.FALLBACK, "news", ("group",))
    assert Router({"group": "G"}).llm("address") is None and Router({"group": "G"}).llm("react") is None


def test_remarks_use_the_profile_without_tools_and_never_fall_back(monkeypatch):
    assert Router({"group": "G", "news": "N"}).llm("chime") == "N"
    # Without `news` there are no remarks: no move to the archive or the internet profiles.
    assert Router({"group": "G", "deep": "D", "web": "W", "smart": "S"}).llm("chime") is None
    assert Router({"group": "G", "news": "N"}, parse_routes("chime:group")).llm("chime") == "G"
    for value in ("chime:web", "chime:smart", "chime:deep"):
        with pytest.raises(ValueError):
            parse_routes(value)
    monkeypatch.setitem(routing.FALLBACK, "news", ("group",))
    assert Router({"group": "G"}).llm("chime") is None
