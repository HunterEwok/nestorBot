"""What the digest never shows, decided by code before any model sees an item.

The owner's rules: the topics are cannabis science, psychedelic science and therapy and
the culture around them (hippies, reggae, dub); no crime, no sales, no dosing and no ways
of use. Any post with prices, availability, delivery or «пишите в ЛС» is trade, not topic,
and goes; blocked Telegram channels are never read or linked.

Strictness follows Source.mode:
- science: journals, preprints and the trial registry. Only non-research publication types
  and minor revisions (a new preprint version, a routine trial record update) are dropped.
- curated: news sites. They discuss market prices, taxes and sales as policy, so trade
  means explicit shop signals only (promo codes, "order now", «наш магазин»), plus
  sponsored posts and grow/dosing guides in the title.
- community: Reddit and Telegram. Any price (with a currency or a label: «Цена: 2 500»),
  availability, delivery, contacts for buying and any dose figure drop the post, wherever
  in the post they are (Item.full_text). Event tickets («Eintritt 5 €») go too: the owner's
  rule makes no exception for them.
topic_required (broad feeds) keeps only items that mention the topics and are not
drug-crime reports; legalization and other policy news stays.

Sources are in English, Russian and German. Patterns use stems and word boundaries
tuned against common false hits: "Dubai", "Alaska", «скачать», «дабл», "cardiac
arrest", "G2/M arrest", "receptor trafficking", «осуждать», «задержка», «дабы», "best-selling",
"8 pm for", «в 2025 г.», "daily dose of dub", "how to make dub delays".
"""
from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import urlparse

from .model import Item, Source
from .sources import BLOCKED_CHANNELS, SOURCES

# Drops worth a row in news_items, to tune the patterns; others are not stored at all.
STORED_REASONS = frozenset({"blocklist", "trade", "dosing", "howto", "crime", "sponsored"})

NON_RESEARCH_PUBTYPES = frozenset({
    "comment", "letter", "editorial", "erratum", "published erratum", "retraction of publication",
    "retracted publication", "news", "newspaper article", "biography", "portrait", "interview",
    "personal narrative",
})
MIN_SCORE = 5

# Where a link post of a links-only subreddit may point: journals, preprint servers, research
# institutions, major news outlets and the sites the digest reads anyway. Matched with
# subdomains; _ACADEMIC adds any university and government site ("edu", "ac.uk", "edu.au",
# "gov", "gov.uk"). No YouTube, image hosts or Reddit itself.
RESEARCH_NEWS_DOMAINS = frozenset({
    # journals and publishers
    "nature.com", "science.org", "sciencemag.org", "cell.com", "thelancet.com", "nejm.org", "jamanetwork.com",
    "bmj.com", "springer.com", "springeropen.com", "biomedcentral.com", "wiley.com", "sciencedirect.com",
    "elsevier.com", "tandfonline.com", "sagepub.com", "frontiersin.org", "mdpi.com", "plos.org", "pnas.org",
    "oup.com", "cambridge.org", "karger.com", "thieme-connect.com", "acs.org", "apa.org", "annualreviews.org",
    "psychiatryonline.org", "akjournals.com", "liebertpub.com", "lww.com", "aspetjournals.org", "physiology.org",
    "jneurosci.org", "elifesciences.org", "embopress.org", "royalsocietypublishing.org", "hindawi.com",
    "dovepress.com", "psychiatrist.com",
    # indexes, preprints, registries, agencies
    "nih.gov", "europepmc.org", "doi.org", "biorxiv.org", "medrxiv.org", "psyarxiv.com", "osf.io", "arxiv.org",
    "researchsquare.com", "ssrn.com", "preprints.org", "clinicaltrials.gov", "who.int", "fda.gov", "cdc.gov",
    "europa.eu", "unodc.org", "emcdda.europa.eu", "researchgate.net", "semanticscholar.org", "jstor.org",
    "medscape.com", "news-medical.net",
    # research institutions and science news
    "hopkinsmedicine.org", "maps.org", "beckleyfoundation.org", "unibas.ch", "uzh.ch", "ethz.ch", "epfl.ch",
    "charite.de", "mpg.de", "ki.se", "mcgill.ca", "utoronto.ca", "ubc.ca", "camh.ca", "uva.nl",
    "maastrichtuniversity.nl", "sciencenews.org", "the-scientist.com", "knowablemagazine.org", "undark.org",
    "chacruna.net", "usona-institute.org", "sciencedaily.com", "eurekalert.org", "medicalxpress.com", "phys.org",
    "newscientist.com", "scientificamerican.com", "theconversation.com", "statnews.com", "technologyreview.com",
    "smithsonianmag.com", "nationalgeographic.com", "quantamagazine.org", "psypost.org",
    # major news outlets
    "nytimes.com", "washingtonpost.com", "theguardian.com", "bbc.co.uk", "bbc.com", "reuters.com", "apnews.com",
    "npr.org", "cnn.com", "wsj.com", "bloomberg.com", "ft.com", "economist.com", "theatlantic.com",
    "newyorker.com", "time.com", "vox.com", "wired.com", "axios.com", "politico.com", "nbcnews.com",
    "cbsnews.com", "abcnews.go.com", "latimes.com", "usatoday.com", "forbes.com", "businessinsider.com",
    "vice.com", "independent.co.uk", "telegraph.co.uk", "cbc.ca", "abc.net.au", "aljazeera.com", "dw.com",
    "spiegel.de", "zeit.de", "sueddeutsche.de", "faz.net", "lemonde.fr", "theverge.com", "nature.org",
    "meduza.io", "nplus1.ru", "knife.media",
}) | frozenset(
    # the curated sites of sources.py (feeds, Reggaeville), whatever gets added there
    (urlparse(s.url).hostname or "").removeprefix("www.") for s in SOURCES if s.kind in ("rss", "reggaeville")
)


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in parts), re.I)


# ---- topics ----

_TOPIC = _rx(
    # cannabis
    r"cannabi", r"mari[jh]uana", r"\b(?:cbd|cbg|cbn|thc[av]?)\b", r"\bhemp(?:seeds?)?\b", r"\bhashish\b",
    r"\b(?:dronabinol|nabilone|nabiximols|sativex|epidiolex)\b", r"\bhanf(?!ord)", r"\bkiff(?:en|er)\b",
    # psychedelics
    r"psychedeli", r"psilocyb", r"psilocin", r"\bmagic\s+mushrooms?\b", r"\bshrooms\b", r"\bzauberpilz",
    r"\blsd\b", r"lysergi", r"\bmdma\b", r"\b(?:5-meo-)?dmt\b", r"dimethyltryptamin", r"ayahuasca",
    r"iboga",                            # ibogaine, noribogaine, Tabernanthe iboga
    r"\bme(?:sc|sk)alin", r"\bpeyote\b", r"\bhall?u[cz]inogen", r"\bentheogen",
    r"\b(?:es|ar)ketamin", r"\bspravato\b",
    # ketamine counts in therapy only: rodent anaesthesia in preprints is not the topic
    r"\bketamin\w*(?:[\s-]+\w+){0,3}?[\s-]+(?:assist|therap|treat|infusion|clinic|depress|antidepress|ptsd|suicid"
    r"|behandl)",
    r"\b(?:therap|treat|depress|ptsd)\w*(?:[\s-]+\w+){0,3}?[\s-]+ketamin",
    # culture
    r"\bhipp(?:ie|ies|y)\b", r"\breggae\b", r"\brasta(?:fari\w*|m[ae]n)?\b", r"\bdance\s?hall\b",
    r"\b(?:bob|ziggy|damian|stephen|julian|rita|ky-mani)\s+marley\b", r"\bking\s+tubby",
    # "dub" the music, not the verb ("what scientists dub ...")
    r"\bdub(?:wise|plates?)?\b(?!\s+(?:it|them|him|her|this|that|these|those|the|a|an)\b)",
    r"(?-i:\b[Ss]ka\b)",                 # not "SKA" (Square Kilometre Array) or «СКА»
    # Russian
    r"кан+аби", r"марихуан", r"конопл", r"\bгашиш", r"\b(?:тгк|кбд)\b(?!-?\d)",   # not «ТГК-1», a power company
    r"психодел", r"псилоци", r"\bлсд\b", r"лизерги", r"\bмдма\b", r"\bдмт\b", r"диметилтриптамин", r"галлюциноген",
    r"энтеоген", r"а[йи]?я(?:у|ху|в)аск",           # аяуаска, айяуаска, аяваска, аяхуаска
    r"\bибог[аиеуо]", r"мескалин", r"\bпейот", r"\b(?:волшебн|магическ)\w*\s+гриб", r"кетамин",
    r"\bхиппи\b", r"\bр[еэ]гг(?:и|ей)\b", r"\bраста\b", r"растаман", r"растафар", r"д[эае]нсхол",
    # «даб», «даба», «дабом», «дабовый»; not «дабы» (in order to) or «дабл»
    r"\bдаб(?:а|е|у|ом|ов(?:ый|ая|ое|ые|ого|ой|ом|ых|ую|ым|ыми))?\b", r"\bкинг\w*\s+табби",
    r"\b(?:боб|зигги|д[эе]миан|стив[еи]н|джулиан|рит)\w{0,2}\s+марли\b", r"(?-i:\b[Сс]ка\b)",
)
# Microdosing alone counts, unless it is the GLP-1 fad or a phase 0 drug trial.
_MICRODOSE = _rx(r"\bmi[ck]ro-?dos", r"микродоз")
_NOT_PSYCHEDELIC_MICRODOSE = _rx(
    r"\b(?:ozempic|wegovy|mounjaro|zepbound|semaglutide|tirzepatide|retatrutide|glp-?1|phase\s+0)\b",
    r"оземпик|семаглутид|тирзепатид|\bглп-?1\b",
)


def topic_match(text: str) -> bool:
    """The text mentions cannabis, psychedelics or reggae/dub/hippie culture (EN, RU, basic DE)."""
    if not isinstance(text, str):
        return False
    if _TOPIC.search(text):
        return True
    return _MICRODOSE.search(text) is not None and _NOT_PSYCHEDELIC_MICRODOSE.search(text) is None


# ---- crime (topic_required feeds) ----

_DRUGS = (r"(?:drugs?|narcotics?|cannabis|marijuana|weed|pot|hash(?:ish)?|cocaine|heroin|opioids?|fentanyl"
          r"|meth(?:amphetamine)?|psilocybin|mushrooms?|lsd|mdma|ecstasy|ketamine)")
_CRIME = _rx(
    r"\barrest(?:ed|s|ing)?\b",
    r"\bseiz(?:ed|ing)\b", r"\bdrug\s+seizures?\b", r"\bconfiscat", r"\bsmuggl", r"\bdealers?\b",
    # trafficking of drugs, not of receptors and vesicles (bioRxiv, PsyPost)
    r"\btraffickers?\b", rf"\b(?:{_DRUGS}|human|arms|sex)\s+traffick",
    rf"\btraffick\w*\s+(?:in|of)\s+(?:(?:illegal|illicit|controlled)\s+(?:substances?|drugs?)|{_DRUGS})\b",
    r"\btrafficking\s+(?:ring|network|charges?|case|operation|gang|route|offen[cs]e)s?\b",
    r"\b(?:accused|suspected|charged|convicted|guilty)\s+of\s+(?:drug\s+)?trafficking\b",
    r"\bdrug\s+(?:bust|ring|lord|cartel|gang|raid|charges?)s?\b", r"\bcartels?\b", r"\bkingpin",
    r"\bpolice\s+raids?\b", r"\braided\b", r"\bsentenced\b", r"\bprison\s+(?:sentence|term)s?\b",
    r"\b(?:years|months)\s+(?:in\s+)?(?:prison|jail)\b", r"\bjailed\b", r"\bconvicted\s+of\b",
    r"\bplead(?:s|ed)?\s+guilty\b", r"\bindicted\b", r"\bcharged\s+with\b",
    r"\bзадержа(?:н|л|ни)", r"\bизъя(?:т|л|ти)", r"наркокурьер", r"закладк", r"закладчик", r"\bсбыт(?!ь)",
    r"контрабанд", r"наркоторгов", r"наркодилер", r"наркопритон", r"нарколаборатор", r"наркобарон", r"наркокартел",
    r"\bуголовн\w*\s+дел", r"\bприговор", r"\bарест", r"\bобыск",
    # convicted, not «осуждать», to condemn
    r"\bосужд[её]н", r"\bсуд\w*\s+(?:[\w-]+\s+){0,3}?осуди", r"\bосуди\w*\s+(?:[\w-]+\s+){0,3}?на\s+\d+\s+(?:год|лет)",
    r"\bст(?:\.|ать[еияю])\s*228\b",
    r"\bfestgenommen\b", r"\bfestnahme", r"\bbeschlagnahm", r"\brazzia", r"\bschmuggel", r"\bdrogenhändler",
    r"\bverurteilt\b", r"\bhaftstrafe",
)
# Policy news mentions arrests and sentences too; these words keep it.
_POLICY = _rx(
    r"legali[sz]", r"decriminali[sz]", r"\breschedul", r"\bpardon", r"\bamnest", r"\bexpung", r"\bclemency",
    r"\breferend", r"\bballot", r"\bvot(?:e|es|ed|ing)\b", r"\bbill\b", r"\blawmakers\b", r"\blegislat",
    r"\bsenate\b", r"\bparliament",
    r"легализ", r"декриминализ", r"помилов", r"амнист", r"референдум", r"законопроект", r"голосова",
    r"legalisier", r"entkriminalisier", r"gesetzentwurf", r"\bbundestag",
)


# Crime words in other senses, cut out before the search: cell biology and medicine in
# preprints and PsyPost ("G2/M arrest", "cells arrested in G1", "mice seized at 41.2 C"),
# "seized on", and taking a substance off the controlled list («изъять из перечня»), which is policy.
_NOT_CRIME = _rx(
    r"\b(?:cardiac|cardio\w*|heart|respiratory|circulatory|sinus|cycle|growth|mitotic|meiotic|developmental"
    r"|proliferati\w*|transcription\w*|translation\w*|replicat\w*|fork|maturation|phase"
    r"|g[0-2](?:[/-](?:g[0-2]|m|s))?)[\s-]+arrest\w*",
    r"\barrested\s+(?:in|at)\s+(?:the\s+)?(?:g[0-2]|s|m|g2/m|mitosis|metaphase|prophase|anaphase|interphase)\b",
    r"\barrested\s+development\b",
    r"\bseiz\w*\s+(?:on|upon)\b", r"\bseized\s+(?:the|this|that|an?)\s+(?:opportunity|moment|initiative|chance)\b",
    # a seizure of the body: who seized, or how often and at what temperature; not "police seized
    # more than 2 t", "seized at the border"
    r"\b(?:subjects?|animals?|patients?|participants?|mice|mouse|rats?|pups?|children|child|infants?|littermates?"
    r"|zebrafish|larvae)\s+(?:(?:who|that|which)\s+)?(?:\w+\s+)?seiz(?:ed|ing)\b",
    r"\bseized\s+(?:less|more)\s+(?:often|frequently|readily)\b",
    r"\bseized\s+at\s+(?:an?\s+)?(?:lower|higher|\d+(?:[.,]\d+)?\s*°?\s*c\b)",
    r"\bизъя\w*\s+(?:[\w-]+\s+){0,3}?из\s+(?:перечн|спис|оборот|реестр)",
)


def _crime(text: str) -> bool:
    return _CRIME.search(_NOT_CRIME.sub(" ", text)) is not None and _POLICY.search(text) is None


# ---- doses ----

_SP = r"[ \t\u00a0]*"
_NUM = r"\d+(?:[.,]\d+)?"
# One figure: «25 мг», «0,5 г», «1–2 g», «10 mg/kg», «100 µg/day», «100 микрограммов», "25-milligram".
# A four-digit number before «г» is a year («в 2025 г.»), not grams; "8 Grammys" are awards.
_FIGURE = (
    rf"(?<![\w.,])(?!(?:1\d|2[01])\d\d{_SP}(?-i:г)(?!\w)){_NUM}"
    rf"(?:{_SP}(?:-|–|—|to|до|and|и|or|или){_SP}{_NUM})?(?:{_SP}|-)"
    r"(?:mcg|µg|μg|ug|mg|grams?|gramm(?!y)\w*|gr|(?-i:g)|(?-i:m[lL])|мкг|мг|грамм\w*|гр|(?-i:г)|мл"
    r"|mi(?:cro|kro|lli)gram(?:s|m\w*)?|millilit(?:er|re)\w*|(?:микро|милли)грамм\w*|миллилитр\w*)(?!\w)"
    rf"(?:(?:{_SP}(?:/|per|на){_SP}(?:kg|кг|kilo\w*|килограмм\w*)(?!\w)|{_SP}(?:kg|кг)[-−]1(?!\d))"
    r"(?:\s+(?:массы\s+тела|(?:of\s+)?body\s*weight)\b)?)?"
    rf"(?:{_SP}/{_SP}(?:day|d|сут\w*|день|(?-i:m[lL])|мл)(?!\w))?"
)
_DOSE = re.compile(_FIGURE, re.I)
# clean_text takes a run of figures with the words that would be left dangling around it:
# «в дозе 25 мг», «from 25 mg to 50 mg of», «от 1 до 2 г».
_DOSE_PHRASE = re.compile(
    r"(?:\b(?:в\s+доз(?:е|ах)|доз(?:ой|ами)|(?:at\s+)?(?:a\s+|the\s+)?(?:single\s+)?doses?\s+of|dosed\s+at"
    r"|at|from|between|от)\s+(?=\d))?"
    rf"{_FIGURE}(?:{_SP}(?:,|;|-|–|—|to|до|and|и|or|или){_SP}{_FIGURE})*"
    r"(?:\s+of\b)?", re.I)


_DOSING_WORDS = _rx(
    # not "your daily dose of dub" in r/reggae and r/dub
    r"\bdos(?:e|es|ed|age|ages|ing)\b(?!\s+of\s+(?:(?:good|pure|some|heavy|classic|conscious|deep|fresh|real|positive"
    r"|vintage|old[\s-]school)\s+)?(?:reggae|dub|roots|ska|rocksteady|dancehall|riddims?|music|vibes?|bass|culture"
    r"|positivity|reality|nostalgia|history|inspiration|sunshine|love|soul|jazz|funk|humou?r)\b)",
    r"\b(?:dosing|dose|microdos\w*|fadiman|stamets)\s+(?:schedule|protocol|regimen)",
    r"\b(?:stamets|microdos\w*|psilocybin|mushroom|shroom|lion'?s\s+mane|niacin)\s+stack(?:s|ing)?\b",
    # "how much should I take", not "how many people use psychedelics"
    r"\bhow\s+(?:much|many)\b[^.!?\n]{0,30}?\b(?:should|do|can|could|would|did|to)\s+(?:i\s+|you\s+|we\s+|one\s+)?"
    r"(?:take|dose|eat|smoke|use|microdose|drink|vape)\b",
    r"\bhow\s+many\s+(?:grams?|g|mg|mcg|ug|µg|ml|tabs?|caps(?:ules)?|drops|gummies|edibles|squares|blotters?)\b",
    r"\bwhat\s+dose\b",
    r"\b(?:half|quarter|third)\s+(?:of\s+)?an?\s+(?:tab|blotter|gummy|cap(?:sule)?|pill|edible|gram)s?\b",
    r"(?<![\d/])\d/\d\s*(?:of\s+an?\s+)?(?:tabs?|blotters?|caps?|gummies|gummy|grams?|g|марк[аиу]?)\b",   # "1/2 tab"
    # an eighth of an ounce; not "an eighth of the population"
    r"\ban\s+eighth\b(?!\s+of\s+(?:the|all|a|an|us|them|those|people|respondents|participants|patients|adults|"
    r"americans|users|voters)\b)(?!\s+(?:grade|place|time|century|album|edition)\b)",
    r"дозир", r"\bдоз(?:а|ы|у|ой|е|ам|ами|ах)?\b", r"\bсхем\w*\s+при[её]м",
    r"\b(?:пол|четверть)[\s-]?марк(?:и|у)\b",     # not «Пол Маркс», a name
    r"\bсколько\b[^.!?\n]{0,30}\b(?:принимать|принять|съесть|курить|брать)\b",   # not «сколько нужно времени»
    r"\bdosis\b", r"\bdosier",
)


# ---- trade ----

# Explicit shop signals: trade in any mode.
_SHOP = _rx(
    r"\border\s+now\b", r"\bbuy\s+now\b", r"\bshop\s+now\b", r"\badd\s+to\s+cart\b",
    r"\buse\s+(?:my\s+|our\s+|the\s+)?(?:promo\s+|discount\s+)?code\b", r"\bwith\s+code\s+(?-i:[A-Z0-9]{3,})\b",
    r"\b(?:promo|discount|coupon)\s*codes?\b", r"\bcoupons?\b", r"\bfree\s+shipping\b",
    r"(?<!\d)(?<!\d\s)\b(?:dm|pm)\s+(?:me|us)\b", r"(?<!\d)(?<!\d\s)\bdm\s+(?:for|if)\b", r"\bdms\s+(?:are\s+)?open\b",
    r"\b(?:visit|check\s+out)\s+our\s+(?:online\s+)?(?:shop|store)\b",
    r"\b(?:in|at|from)\s+our\s+(?:online\s+)?(?:shop|store)\b",
    r"промокод", r"скидк\w*[^.!?\n]{0,40}\bкод(?:у|ом|а)?\b", r"\bкод(?:у|ом|а)?\b[^.!?\n]{0,30}скидк",
    r"\bзакаж(?:и|ите)\b", r"\bзаказ(?:ать|ывайте)\b(?!\s+(?:исследовани|экспертиз|анализ|опрос|доклад))",
    r"пиши(?:те)?\s+в\s+(?:лс|личк|личные|директ)", r"\bв\s+личк",
    # «в нашем интернет-магазине», not news about «интернет-магазины»
    r"\bнаш\w*[\s-]+(?:интернет[\s-]+)?магазин", r"магазин\w*\s+по\s+ссылке",
    r"бесплатн\w*\s+доставк", r"доставк\w*\s+(?:по\s+(?:всей|россии|рф|городу|миру|москве|спб|снг)|бесплатн|курьер)",
    r"\bjetzt\s+(?:bestellen|kaufen)\b", r"\bgutschein", r"\brabattcode",
)
_SHOP_LINK = re.compile(r"https?://(?:(?:shop|store)\.[^\s/]+|[^\s/]+/\S*?\b(?:shop|store|products?|cart)\b)", re.I)
_BUY_CALL = _rx(r"\b(?:buy|purchase)\b", r"(?<!executive\s)(?<!court\s)(?<!in\s)\border\b(?!\s+(?:to|of)\b)",
                r"\b(?:get|grab)\s+yours\b", r"\bavailable\s+(?:now\s+)?(?:at|in)\b",
                r"\bкуп(?:и|ите|ить)\b", r"\bkaufen\b|\bbestellen\b")

_PRICE = _rx(
    r"[$€£₽¥₴]\s?\d",                     # also "US$20", "A$50", "C$10"
    r"\d(?:[.,]-+)?\s?(?:(?:k|m|bn|тыс|млн|млрд|million|billion|thousand)\.?\s?)?"
    r"(?:[$€£₽¥₴]|(?:eur|euros?|usd|usdt|usdc|dollars?|bucks|quid|gbp|chf|rub|uah|btc|xmr|ltc|eth|руб\w*|евро|долл\w*"
    r"|бакс\w*|грн|гривен\w*)(?!\w)|р\.)",
    r"\d\d\s?р(?![\w-])",                  # «500р»; one digit is «2 р в день», times
    r"(?<![\w.,])\d{1,6},[-–](?![\w-])",     # «Eintritt 10,-»
    r"\bпрайс", r"\bprice\s?lists?\b",
    # a price without a currency: "Price: 30", «Цена — 1500», «Цены: от 500», «Цена 1500 за упаковку»,
    # «Цена терапии договорная»; not "Eintritt: 18+", «цены выросли на 20%», «стоимость в 2025 году»
    # or «цена ошибки: 3 года»
    r"\b(?:prices?|costs?|preise?|kosten|eintritt)\s*[:—–-]\s*\d(?![\d.,]*\s*(?:\+|%|jahr|years?\b))",
    r"\bцены\s*[:—–-]\s*(?:от\s+)?(?!\d[\d\s.,]*(?:год|лет\b|%|процент|раза?\b))\d",
    # "Prices from 30", "Price 30 per gram", «Цены от 1500», "Kosten 20"; not "Kosten 20 Prozent mehr"
    r"\bprices?\s+(?:from|start(?:s|ing)?\s+(?:at|from))\s+\d", r"\b(?:price|preis)\s+\d[\d.,]*\s+(?:per|pro|each|für)\b",
    r"\bцены?\s+от\s+(?!(?:19|20)\d\d\s*г)(?!\d[\d\s.,]*(?:год|лет\b|%|процент|раза?\b))\d",
    r"\bkosten\s+(?:nur\s+)?\d(?![\d.,]*\s*(?:%|prozent|jahr|mal\b|millionen|milliarden|mio\b|mrd\b))",
    r"(?-i:\d\s?TON\b)",                   # Toncoin
    r"\bоплат\w*\s+(?:картой|переводом|на\s+карту|крипт\w*|наличн\w*|usdt|btc)",
    r"\b(?:цена|стоимость)\b(?:\s+[а-яё]+){0,2}?\s*[:—–-]?\s*(?:от\s+)?"
    r"(?:(?!(?:19|20)\d\d\s*г(?:\.|\b))(?!\d[\d\s.,]*(?:год|лет\b|%|процент|раза?\b))\d|договорн)",
)
# Community posts: the full rule of the owner.
_STRICT_TRADE = _rx(
    r"\bin\s+stock\b(?!\s+market)", r"\bfor\s+sale\b", r"(?<!best-)(?<!best\s)(?<!top-)(?<!top\s)\bselling\b",
    r"\bshipping\b", r"(?<!drug\s)(?<!vocal\s)(?<!lyrical\s)\bdelivery\b",
    r"\bwhere\s+(?:to|can\s+i)\s+(?:buy|get|order|find)\b",
    r"\bhmu\b", r"\bwickr\b", r"\bwhats\s?app\b", r"\b(?:telegram|tg|signal|threema|kik)\s*[:\-]?\s*@\w",
    r"\b(?:contact|reach)(?:\s*:\s*|\s+)(?:me\s+|us\s+)?(?:at\s+|on\s+|via\s+)?@\w",
    r"\bget\s+a\s+hold\s+of\s+(?:me|us)\b", r"\b(?:message|msg|text|inbox)\s+(?:me|us)\b", r"\bhit\s+(?:me|us)\s+up\b",
    r"\bsend\s+(?:me|us)\s+an?\s+(?:dm|pm|message)\b",
    r"\b(?:in|via|by|through|per)\s+(?:my\s+|the\s+|our\s+)?(?:dms?|pms)\b", r"\b(?:per|via|eine)\s+pn\b",
    r"\bв\s+лс\b", r"\bв\s+директе?\b",   # not «в директорат»
    r"в\s+наличии", r"\bналичи[еяию]\b", r"доставк", r"магазин", r"\bкуп(?:ить|ите)\b", r"\bпродаж", r"\bпродам\b",
    r"\bпродаю\b", r"\bгде\s+(?:купить|взять|достать|найти|заказать)\b",
    r"\bпиши(?:те)?\b[^.!?\n]{0,20}(?:\bлс\b|личк|директ|@\w)", r"\bконтакт\w*\s*:?\s*@\w",
    r"\bversand", r"\blieferung", r"\bzu\s+verkaufen\b", r"\bverkaufe\b", r"\bauf\s+lager\b", r"\bpreisliste",
    # "PM for prices", not "8 pm for the lecture", "the PM for health" or "UK PM for drug reform"
    r"(?<!\d)(?<!\d\s)(?<!the\s)\bpm\s+(?:if\b|for\s+(?:the\s+|a\s+)?(?:prices?|pricing|price\s?lists?|info|details"
    r"|more|menu|orders?|inquiries|quotes?|rates?|costs?|availability)\b)",
    r"\bbei\s+interesse\b[^.!?\n]{0,20}?\b(?:pn|dm|pm|privat\w*)\b",
    # not "we shipped our new album", "relationship", the Windrush "ship to the UK"
    r"\b(?:we|i)\s+(?:also\s+|now\s+|only\s+)?ship\b",
    r"(?<!\bthe\s)(?<!\ba\s)\bships?\s+(?:worldwide|internationally|discreetly|anywhere|everywhere|within|in\s+\d"
    r"|to\s+(?:the\s+)?(?:eu|us|usa|uk|europe|canada|australia|all|any|your))\b",
    r"\b(?:jetzt|ab\s+sofort|wieder)\s+(?:\w+\s+)?erhältlich\b",
    r"\bwo\s+(?:kann|könnte|kriegt|bekommt)\s+(?:man|ich)\b[^.!?\n]{0,30}?\b(?:kaufen|bestellen|besorgen)\b",
    # contacts in private, not «пишите свои мысли в комментариях», a public discussion
    r"\b(?:на)?пиши(?:те)?\b[^.!?\n]{0,20}(?:телеграм|telegram|\bтг\b|\bтелег[уеи]\b|ватсап|вотсап|вайбер|viber)",
    # not the news idioms «в личной переписке», «в личном сообщении»
    r"\bв\s+личн(?:ых|ые)\s+сообщени", r"\bв\s+личн(?:ый|ом)\s+чат",
    r"\bотправ(?:к\w*|ля\w*|лю)\s+(?:по\s+(?:всей|всему|россии|рф|миру|снг|европе)|почтой|в\s+регионы)",
    r"\bсамовывоз",
    # a contact given as a link: "Contact: t.me/dealerbot", «Связь: t.me/…»
    r"\b(?:contact|kontakt|контакт\w*|связь|пиши(?:те)?)\s*:?\s*(?:https?://)?(?:t\.me|telegram\.me|wa\.me)/\w",
)

# ---- guides and sponsored posts ----

# "How to make" a product, not "how to make psychedelic therapy affordable" or "how to make dub delays".
_PRODUCT = (r"(?:canna-?butter|canna-?oil|butter|oil|edibles?|gummies|tinctures?|tea|hash(?:ish)?|rosin|wax|dabs?"
            r"|shatter|concentrates?|extracts?|brownies|cookies|capsules?|chocolates?|joints?|blunts?|bongs?|pipes?|rso"
            r"|ghee|honey|syrup|lemon\s+tek|spore\s+prints?|spawn|substrate|ayahuasca|dmt|lsd|mdma|ketamine|kief"
            r"|moon\s?rocks?)")
_PRODUCT_RU = (r"(?:масл|гашиш|настой|настойк|отвар|чай|экстракт|тинктур|брауни|печень|конфет|шоколад|косяк|джойнт"
               r"|бонг|трубк|кекс|мармелад|сироп|спор|грибниц|субстрат|лсд|дмт|мдма|аяуаск|к[ао]н+абутер)")
_HOWTO = _rx(
    r"\bhow\s+to\s+(?:dose|microdose|extract|smoke|vape|cook|bake|brew|prepare|cultivate|decarb\w*|clone|harvest"
    r"|germinate|infuse|consume|trip)\b",
    rf"\bhow\s+to\s+make\s+(?:[\w'-]+\s+){{0,3}}?{_PRODUCT}\b",
    # not "how to take on Big Pharma", "... psychedelic care to scale", "how to roll out", "how to grow a business"
    r"\bhow\s+to\s+take\b(?!\s+(?:on|part|back|action|advantage|stock|charge|control|care|over|place|note|aim|steps?"
    r"|a\s+stand|the\s+lead)\b)(?![^.!?\n]{0,40}\bto\s+scale\b)",
    r"\bhow\s+to\s+roll\b(?!\s+(?:out|back)\b)",
    r"\bhow\s+to\s+grow\b(?!\s+(?:[\w'-]+\s+){0,2}?(?:business(?:es)?|brands?|compan(?:y|ies)|industry|markets?|sales"
    r"|revenue|audience|fan\s?base|following|channel|community|movement|career|label|network|team|startup)\b)",
    r"\bgrow(?:ing|er'?s)?\s+(?:guide|tips|tutorial|diary|journal)\b",
    r"\bguide\s+to\s+(?:growing|grow|cultivating|dosing|microdosing|making|extracting|smoking|vaping)\b",
    r"\b(?:dosing|dosage|microdosing)\s+(?:guide|chart|calculator|tips)\b",
    # ways of use and home extraction: "lemon tek", "extracting DMT from mimosa"
    r"\b(?:lemon|pf|ubt)[\s-]*tek\b", r"\bлимонн?\w*\s+тек\b",
    r"\bextract(?:ing|ion|ed)?\s+(?:of\s+)?(?:the\s+)?(?:5-meo-)?(?:dmt|mescaline|lsa|ibogaine|psilocybin)\s+from\b",
    r"\b(?:5-meo-)?(?:dmt|mescaline|lsa|ibogaine|psilocybin)\s+extraction\s+from\b",
    r"\b(?:best|top)\s+(?:\d+\s+)?(?:\w+\s+)?strains?\b",
    r"\bкак\s+(?:правильно\s+)?(?:вырастить|выращивать|дозировать|приготовить|употреблять|курить|заварить"
    r"|экстрагировать)\b",
    rf"\bкак\s+(?:правильно\s+)?сделать\s+(?:[\w-]+\s+){{0,3}}?{_PRODUCT_RU}",
    # not «как принимать решения», «как принять себя»
    r"\bкак\s+(?:правильно\s+)?(?:принимать|принять)\b(?!\s+(?:решени|себя|закон|участи|мер|во\s+внимание|вызов"
    r"|гост|пациент|правил|поправк))",
    r"\bлучши[ехй]\s+(?:\w+\s+)?сорт", r"\bгайд\s+по\s+(?:выращиван|дозировк|микродозинг|употреблени)",
    r"\bинструкци\w*\s+по\s+(?:выращиван|применени|употреблени|приготовлени)",
    r"\bwie\s+man\b[^.!?\n]{0,30}\b(?:anbaut|anbauen|herstellt|dosiert|einnimmt|konsumiert)\b",
    r"\banbau-?(?:anleitung|tipps)\b", r"\bdosierungs?(?:anleitung|tipps)\b",
)
_SPONSORED = _rx(
    r"(?:^|[\[(|]|\s[-–—:|]\s)\s*sponsored\b", r"\bsponsored\s+(?:content|post|article|feature|story)\b",
    r"\bpartner\s+content\b", r"\badvertorial\b", r"\bpaid\s+(?:content|partnership|post)\b",
)

# @name, t.me/name, t.me/s/name, the telegram.me and telegram.dog mirrors, tg://resolve?domain=name
# and name.t.me
_CHANNELS = "|".join(sorted(map(re.escape, BLOCKED_CHANNELS)))
_BLOCKED = re.compile(r"(?:@|\b(?:t\.me|telegram\.(?:me|dog))/(?:s/)?|\btg://resolve\?(?:\S*?&)?domain=)(?:"
                      + _CHANNELS + r")(?![A-Za-z0-9_])"
                      + r"|(?<![\w-])(?:" + _CHANNELS + r")\.(?:t\.me|telegram\.(?:me|dog))\b", re.I)


# ---- the decision ----

def drop_reason(item: Item, source: Source) -> str | None:
    """Why the item must not reach the digest, or None when it passes."""
    # the whole post where the adapter clipped the summary: trade hides at the end of long posts
    core = _join((item.title, item.full_text or item.summary))
    meta = item.meta or {}
    full = _join((core, *item.categories, *(v for v in (meta.get("link"), meta.get("domain")) if isinstance(v, str))))

    if _blocked(item, source):
        return "blocklist"
    if source.topic_required:
        if not topic_match(core):
            return "offtopic"
        if _crime(core):
            return "crime"
    if source.mode == "science":
        return _science(meta)
    if source.mode == "community":
        return _community(item, source, meta, full)
    return _curated(item, source, full)


def _science(meta: dict[str, Any]) -> str | None:
    pubtypes = meta.get("pubtypes") or ()
    if isinstance(pubtypes, str):
        pubtypes = (pubtypes,)
    if any(isinstance(p, str) and p.strip().lower() in NON_RESEARCH_PUBTYPES for p in pubtypes):
        return "pubtype"
    version = meta.get("version")
    later = version is not None and str(version).strip() not in ("", "1") and not meta.get("first_in_window")
    if meta.get("change") == "update" or later:
        return "minor_update"
    return None


def _curated(item: Item, source: Source, full: str) -> str | None:
    title = item.title if isinstance(item.title, str) else ""
    dropped = {str(c).lower() for c in source.params.get("drop_categories", ())}
    if dropped & {str(c).lower() for c in item.categories} or _SPONSORED.search(title):
        return "sponsored"
    if _shop(full):
        return "trade"
    if _HOWTO.search(title):
        return "howto"
    return None


def _community(item: Item, source: Source, meta: dict[str, Any], full: str) -> str | None:
    if meta.get("unsettled"):
        return "unsettled"      # the archive's score and removal state are not final yet; asked for again
    if meta.get("removed"):
        return "removed"
    if meta.get("over_18"):
        return "nsfw"
    if meta.get("stickied"):
        return "stickied"
    if source.params.get("links_only"):
        if meta.get("is_self"):
            return "self_post"
        if not _allowed_domain(_link_domain(item)):
            return "offtopic"
    score = meta.get("score")
    if "score" in meta and isinstance(score, (int, float)) and not isinstance(score, bool) and score < MIN_SCORE:
        return "low_score"
    if _shop(full) or _PRICE.search(full) or _STRICT_TRADE.search(full):
        return "trade"
    if _DOSE.search(full) or _DOSING_WORDS.search(full):
        return "dosing"
    if _HOWTO.search(full):
        return "howto"
    return None


def _shop(text: str) -> bool:
    if _SHOP.search(text):
        return True
    return _SHOP_LINK.search(text) is not None and _BUY_CALL.search(text) is not None


def _blocked(item: Item, source: Source) -> bool:
    channel = source.params.get("channel")
    if isinstance(channel, str) and channel.lower() in BLOCKED_CHANNELS:
        return True
    texts = [item.url, item.title, item.summary, item.full_text]
    for value in (item.meta or {}).values():   # link, forwarded_from, link preview: whatever the adapter keeps
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, (list, tuple)):
            texts.extend(v for v in value if isinstance(v, str))
    return any(_BLOCKED.search(t) for t in texts if isinstance(t, str))


def _join(parts: Iterable[str]) -> str:
    return "\n".join(p for p in parts if isinstance(p, str) and p)


def _host(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    try:
        u = urlparse(url.strip())
        host = u.hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".").removeprefix("www.") if u.scheme in ("http", "https") else ""


def _link_domain(item: Item) -> str:
    """The host a link post points to: the adapter's meta link, Reddit's domain field, else the item url."""
    meta = item.meta or {}
    host = _host(meta.get("link"))
    if not host and isinstance(meta.get("domain"), str):
        host = meta["domain"].strip().lower().rstrip(".").removeprefix("www.")
    return host or _host(item.url)


_ACADEMIC = re.compile(r"(?:^|\.)(?:(?:edu|gov)(?:\.[a-z]{2})?|ac\.[a-z]{2}|nhs\.uk)$")


def _allowed_domain(host: str) -> bool:
    labels = host.split(".") if host else []
    return (any(".".join(labels[i:]) in RESEARCH_NEWS_DOMAINS for i in range(len(labels)))
            or _ACADEMIC.search(host) is not None)


# ---- text written by a model ----

# Whatever Telegram links in a bot message: URLs with a scheme (tg:// too) or "www.", bare
# domains ("psypost.org", "MAPS.ORG/research", "shop.рф", "x.store:443/buy"), e-mails and
# @names. "U.S.", "e.g.", «т.е.», "Ph.D.", "3.5" and «А. С. Пушкин» have no two-letter TLD and
# stay; a dotted word ("Node.js") goes too. Not «.москва»: «г.Москва». A domain starts only where
# a run of labels starts and labels are bounded as in DNS, or "a-a-a-..." and "-a.-a.-a." take
# seconds; "...psypost.org" is a run too. Every @name goes: Telegram links "@@name" and ".@name",
# and "x.io@name" once "x.io" is gone.
_TLD = r"(?:[a-z]{2,}|рф|рус|онлайн|сайт|орг|ком|укр|бел|срб|бг|қаз)(?![\w-])"
_TAIL = r"""[^\s<>()\[\]"'«»]*[^\s<>()\[\]"'«».,;:!?]"""
_URL = re.compile(
    rf"""(?:\b[a-z][a-z0-9+.-]{{1,15}}://|\bwww\.)(?:{_TAIL})?"""
    rf"|(?<![\w.+-])[\w.+-]{{1,64}}@(?:[\w-]{{1,63}}\.)+{_TLD}"
    rf"|(?<![\w@-])(?<![\w-]\.)(?:[\w-]{{1,63}}\.)+{_TLD}(?::\d{{1,5}})?(?:/(?:{_TAIL})?|[?#]{_TAIL})?"
    r"|@[a-z0-9_]{2,}(?:\.[\w-]+)*", re.I)
_EMPTY_BRACKETS = re.compile(r"\(\s*[,;:/–—-]?\s*\)|\[\s*[,;:/–—-]?\s*\]")


def clean_text(text: str) -> str:
    """Model text without links (URLs, domains, e-mails, @names) and dose figures («25 мг»,
    «1–2 g», «10 mg/kg»). Years, percentages, sample sizes and trial phases stay."""
    if not isinstance(text, str):
        return ""
    text = _URL.sub(" ", text)
    text = _DOSE_PHRASE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)                          # first: "\s*[,;]" is quadratic on long runs
    text = re.sub(r"([(\[])\s*[,;]\s*", r"\1", text)          # «(, перорально)»
    text = re.sub(r"\s*[,;]\s*([)\]])", r"\1", text)          # «(перорально, )»
    text = _EMPTY_BRACKETS.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?…)\]])(?!\w)", r"\1", text)   # not «shop .com» into a new domain
    text = re.sub(r"([(\[])\s+", r"\1", text)
    text = re.sub(r",(?:\s*,)+", ",", text)
    text = re.sub(r"(?<![,;:])[,;:]+(?=[.!?…]|\s*$)", "", text)   # «Подробнее: <url>.»; whole runs only
    return text.strip()
