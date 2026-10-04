"""What the digest reads. Research notes with endpoints, limits and terms:
/mnt/project-files/nestorbot/research/news-sources-tech.md (owner's project files).

Not here on purpose: Google News and Bing (robots.txt and personal-use terms, owner's
decision 2026-10-04), Bandcamp (its rules forbid scraping), United Reggae (dead since
2019), Leafly's main feed (strains and products), and the Telegram channels in BLOCKED.
"""
from __future__ import annotations

import re

from .model import Source

# Shops, grow channels and album dumps: never read, never linked, whatever the settings say.
BLOCKED_CHANNELS = frozenset({
    "microdosetoday", "cannabiscultivation", "growbud", "growpotbot", "podsalyami",
    "reggaefoundation", "reggae_music_albums",
})

PUBMED_QUERY = (
    "(psilocybin[tiab] OR psilocin[tiab] OR psychedelic*[tiab] OR \"lysergic acid diethylamide\"[tiab] "
    "OR LSD[tiab] OR MDMA[tiab] OR \"3,4-methylenedioxymethamphetamine\"[tiab] OR DMT[tiab] "
    "OR dimethyltryptamine[tiab] OR \"5-MeO-DMT\"[tiab] OR ayahuasca[tiab] OR ibogaine[tiab] "
    "OR mescaline[tiab] OR cannabis[tiab] OR cannabinoid*[tiab] OR cannabidiol[tiab] "
    "OR tetrahydrocannabinol[tiab] OR THC[tiab] OR marijuana[tiab]) AND hasabstract"
)
EUROPEPMC_TERMS = ("psilocybin OR psychedelic* OR \"lysergic acid diethylamide\" OR MDMA OR DMT OR ayahuasca "
                   "OR ibogaine OR mescaline OR cannabis OR cannabinoid* OR cannabidiol OR tetrahydrocannabinol")
TRIAL_INTERVENTIONS = (
    "psilocybin", "MDMA", "midomafetamine", "lysergic acid diethylamide", "LSD", "DMT", "dimethyltryptamine",
    "5-MeO-DMT", "mebufotenin", "lysergide", "ayahuasca", "ibogaine",
    "mescaline", "cannabidiol", "CBD", "cannabis", "tetrahydrocannabinol", "THC", "dronabinol", "nabilone",
    "nabiximols",
    # names "cannabis" is no part of: "Smoked marijuana", "Synthetic cannabinoid", "Cannabigerol", "Psilocin"
    "marijuana", "marihuana", "cannabinoid", "cannabigerol", "psilocin",
)
ARCTIC_SHIFT = "https://arctic-shift.photon-reddit.com/api/posts/search"

SOURCES: tuple[Source, ...] = (
    # ---- research ----
    Source("pubmed", "PubMed", "pubmed", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/", "research", every_h=6,
           mode="science", max_age_days=4, params={"query": PUBMED_QUERY}),
    Source("europepmc", "Europe PMC", "europepmc", "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
           "research", every_h=12, mode="science", max_age_days=10, params={"terms": EUROPEPMC_TERMS}),
    Source("medrxiv", "medRxiv", "biorxiv", "https://api.biorxiv.org/details/medrxiv", "research", every_h=24,
           mode="science", topic_required=True, max_age_days=5, params={"server": "medrxiv"}),
    Source("biorxiv", "bioRxiv", "biorxiv", "https://api.biorxiv.org/details/biorxiv", "research", every_h=24,
           mode="science", topic_required=True, max_age_days=5, params={"server": "biorxiv"}),
    Source("ctgov", "ClinicalTrials.gov", "ctgov", "https://clinicaltrials.gov/api/v2/", "trials", every_h=24,
           mode="science", max_age_days=14, params={"interventions": TRIAL_INTERVENTIONS}),
    # All drugs, alcohol and nicotine included: only items about the topics.
    Source("psypost", "PsyPost", "rss", "https://www.psypost.org/exclusive/drugs/feed/", "research", every_h=3,
           topic_required=True, max_age_days=10),
    # ScienceDaily: no text kept (their terms), a cache-buster against stale CDN copies.
    Source("sciencedaily_marijuana", "ScienceDaily", "rss",
           "https://www.sciencedaily.com/rss/mind_brain/marijuana.xml", "research", every_h=6, max_age_days=60,
           params={"cache_buster": True, "keep_text": False}),
    # One or two stories a month, gaps of 60+ days are normal: stale only after four months.
    Source("sciencedaily_psychedelics", "ScienceDaily", "rss",
           "https://www.sciencedaily.com/rss/mind_brain/psychedelic_drugs.xml", "research", every_h=6,
           max_age_days=120, params={"cache_buster": True, "keep_text": False}),
    # ---- policy and industry ----
    Source("marijuanamoment", "Marijuana Moment", "rss", "https://www.marijuanamoment.net/feed/", "policy",
           every_h=1, max_age_days=3),
    Source("norml_news", "NORML", "rss", "https://norml.org/news/feed/", "policy", every_h=2, max_age_days=14),
    Source("norml_blog", "NORML", "rss", "https://norml.org/feed/", "policy", every_h=2, max_age_days=14),
    Source("leafly_politics", "Leafly", "rss", "https://www.leafly.com/news/category/politics/feed", "policy",
           every_h=12, max_age_days=120),
    Source("hightimes", "High Times", "rss", "https://hightimes.com/category/news/feed/", "policy", every_h=2,
           max_age_days=14, params={"drop_categories": ("sponsored", "grow", "products", "strains", "deals")}),
    Source("psychedelicalpha", "Psychedelic Alpha", "rss", "https://psychedelicalpha.com/feed", "policy",
           every_h=3, max_age_days=21, params={"paid_marker": "Pα+"}),
    Source("lucidnews", "Lucid News", "rss", "https://www.lucid.news/feed/", "policy", every_h=3, max_age_days=21),
    Source("maps", "MAPS", "rss", "https://maps.org/feed/", "policy", every_h=6, max_age_days=45),
    # ---- community: Reddit through the Arctic Shift archive (Reddit closed its own API) ----
    # Personal posts of r/Psychonaut and r/microdosing are dosing reports by nature: links only.
    Source("reddit_psychonaut", "r/Psychonaut", "arctic", ARCTIC_SHIFT, "community", every_h=24, mode="community",
           max_age_days=4, params={"subreddit": "Psychonaut", "links_only": True}),
    Source("reddit_microdosing", "r/microdosing", "arctic", ARCTIC_SHIFT, "community", every_h=24,
           mode="community", max_age_days=4, params={"subreddit": "microdosing", "links_only": True}),
    Source("reddit_psychedelictherapy", "r/PsychedelicTherapy", "arctic", ARCTIC_SHIFT, "community", every_h=24,
           mode="community", max_age_days=10, params={"subreddit": "PsychedelicTherapy"}),
    # ---- reggae and dub ----
    Source("reggaeville_news", "Reggaeville", "reggaeville", "https://www.reggaeville.com/news/", "reggae",
           every_h=3, max_age_days=10, params={"page": "news"}),
    Source("reggaeville_releases", "Reggaeville", "reggaeville", "https://www.reggaeville.com/music/releases/",
           "reggae", every_h=12, max_age_days=21, params={"page": "releases"}),
    Source("worldareggae", "World A Reggae", "rss", "https://www.worldareggae.com/feed", "reggae", every_h=3,
           max_age_days=14),
    Source("dailyreggae", "Daily Reggae", "rss", "https://dailyreggae.com/news?format=rss", "reggae", every_h=3,
           max_age_days=14, params={"id_from_link": True}),
    Source("reddit_reggae", "r/reggae", "arctic", ARCTIC_SHIFT, "reggae", every_h=24, mode="community",
           max_age_days=4, params={"subreddit": "reggae"}),
    Source("reddit_dub", "r/dub", "arctic", ARCTIC_SHIFT, "reggae", every_h=24, mode="community", max_age_days=7,
           params={"subreddit": "dub"}),
    # ---- Russian media: only items about the topics ----
    Source("nplus1", "N+1", "rss", "https://nplus1.ru/rss", None, every_h=1, topic_required=True, lang="ru",
           max_age_days=4),
    Source("knife", "Нож", "rss", "https://knife.media/feed/", None, every_h=3, topic_required=True, lang="ru",
           max_age_days=14),
    Source("meduza", "Meduza", "rss", "https://meduza.io/rss/all", None, every_h=1, topic_required=True, lang="ru",
           max_age_days=3),
)

_CHANNEL = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")


def telegram_source(channel: str) -> Source:
    """A public channel read through its web preview t.me/s/<channel>. Posts are community
    content: the strict trade filter applies."""
    return Source(f"tg_{channel.lower()}", f"Telegram @{channel}", "tme", f"https://t.me/s/{channel}", "community",
                  every_h=1, mode="community", lang="", max_age_days=60, params={"channel": channel})


def parse_channels(value: str) -> tuple[str, ...]:
    """NEWS_TELEGRAM_CHANNELS: names with or without @ or t.me/, comma-separated. Blocked or
    malformed names stop the service at start, so a typo is noticed."""
    out: list[str] = []
    for raw in value.split(","):
        name = raw.strip().removeprefix("https://").removeprefix("t.me/").removeprefix("s/").lstrip("@").strip("/")
        if not name:
            continue
        if not _CHANNEL.fullmatch(name):
            raise ValueError(f"not a Telegram channel name: {raw.strip()!r}")
        if name.lower() in BLOCKED_CHANNELS:
            raise ValueError(f"channel @{name} is blocked (shop, grow channel or album dump)")
        if name.lower() not in (n.lower() for n in out):
            out.append(name)
    return tuple(out)


def all_sources(channels: tuple[str, ...] = ()) -> tuple[Source, ...]:
    return SOURCES + tuple(telegram_source(c) for c in channels)
