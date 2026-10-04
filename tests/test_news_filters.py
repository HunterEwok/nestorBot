"""Code filters of the news digest: topics, blocked channels, crime, trade, dosing and guides,
per source mode, and clean_text for model-written text. No database, no network."""
import time

import pytest

from nestor.news import filters
from nestor.news.filters import RESEARCH_NEWS_DOMAINS, STORED_REASONS, clean_text, drop_reason, topic_match
from nestor.news.model import Item, Source
from nestor.news.sources import SOURCES, telegram_source

CURATED = Source("mm", "Marijuana Moment", "rss", "https://www.marijuanamoment.net/feed/", "policy", every_h=1)
HIGHTIMES = Source("hightimes", "High Times", "rss", "https://hightimes.com/category/news/feed/", "policy", every_h=2,
                   params={"drop_categories": ("sponsored", "grow", "products", "strains", "deals")})
BROAD_EN = Source("psypost", "PsyPost", "rss", "https://www.psypost.org/exclusive/drugs/feed/", "research", every_h=3,
                  topic_required=True)
BROAD_RU = Source("meduza", "Meduza", "rss", "https://meduza.io/rss/all", None, every_h=1, topic_required=True,
                  lang="ru")
SCIENCE = Source("pubmed", "PubMed", "pubmed", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/", "research", every_h=6,
                 mode="science")
PREPRINTS = Source("medrxiv", "medRxiv", "biorxiv", "https://api.biorxiv.org/details/medrxiv", "research", every_h=24,
                   mode="science", topic_required=True)
REDDIT = Source("reddit_reggae", "r/reggae", "arctic", "https://arctic-shift.example/api", "reggae", every_h=24,
                mode="community", params={"subreddit": "reggae"})
LINKS_ONLY = Source("reddit_microdosing", "r/microdosing", "arctic", "https://arctic-shift.example/api", "community",
                    every_h=24, mode="community", params={"subreddit": "microdosing", "links_only": True})
TG = telegram_source("psychedelicsocietyvienna")

PERMALINK = "https://www.reddit.com/r/microdosing/comments/1abcd/x/"


def item(title, summary="", url="https://example.org/news/1", **kw):
    return Item(ext_id="1", url=url, title=title, summary=summary, **kw)


def post(title, summary="", **meta):
    """A Reddit post as the Arctic Shift adapter gives it: fine unless the test says otherwise."""
    meta.setdefault("score", 40)
    meta.setdefault("is_self", False)
    return item(title, summary, url=PERMALINK, meta=meta)


# ---- topics ----

@pytest.mark.parametrize("text", [
    # cannabis
    "Cannabis use and working memory in adolescents", "Medical marijuana laws and opioid deaths",
    "Cannabinoid receptor CB1 in the hippocampus", "Endocannabinoid signalling", "CBD oil for anxiety",
    "THC potency keeps rising", "Hemp-derived cannabinoids face a federal ban", "Cannabidiol in Dravet syndrome",
    "Nabiximols for spasticity",
    # psychedelics
    "Psychedelic therapy goes mainstream", "Psychedelics and the default mode network",
    "Psilocybin for treatment-resistant depression", "Psilocin pharmacokinetics", "Magic mushrooms in Oregon",
    "LSD microdosing trial", "MDMA-assisted therapy for PTSD", "DMT extends the near-death experience",
    "5-MeO-DMT inhalation study", "Ayahuasca and grief", "Ibogaine for opioid withdrawal", "Mescaline from peyote",
    "Ketamine therapy clinics expand", "Ketamine for depression: a meta-analysis", "Treating PTSD with ketamine",
    "Esketamine nasal spray approved", "Hallucinogen use among older adults", "Microdosing and mood",
    # culture
    "The hippie movement at 60", "Reggae legend returns", "A new dub album from Mad Professor",
    "Dub poetry evening", "Rastafari culture in Ethiopia", "Ska band reunion tour", "Ska-punk revival",
    "Dancehall star signs a deal", "Bob Marley biopic tops the box office", "King Tubby's studio",
    # Russian
    "Каннабис в медицине", "Легализация марихуаны в Германии", "Конопляное масло", "Каннабиноиды и боль",
    "Психоделики и депрессия", "Псилоцибин помог при депрессии", "ЛСД снова изучают", "МДМА-терапия ПТСР",
    "Галлюциногены и мозг", "Аяуаска в Перу", "Ибогаин против зависимости", "Мескалин из кактуса",
    "Кетамин и депрессия", "Эскетамин зарегистрировали", "Фестиваль хиппи", "Регги-фестиваль в Москве",
    "Новый даб-альбом", "Растаманы и Эфиопия", "Растафарианство", "Дэнсхолл-вечеринка", "Дансхолл в Ямайке",
    "Фильм о жизни Боба Марли", "ска-панк на фестивале", "ТГК в крови водителей",
    # German
    "Psychedelika in der Therapie", "Der Hanfverband fordert mehr", "Zauberpilze in Wien", "Cannabis-Gesetz",
    "Ketamin-Therapie bei Depression",
])
def test_topic_match_finds_the_themes(text):
    assert topic_match(text)


@pytest.mark.parametrize("text", [
    "Dubai property prices", "Dublin marathon results", "Double trouble for the economy", "A dubious claim",
    "Dubbed into Russian", "What scientists dub the effect", "Dubstep night at the club",
    "Alaska oil drilling", "Nebraska caucus", "SKA radio telescope sees first light", "Скачать бесплатно",
    "Дабл-ю: новая серия", "Дабл чизбургер", "СКА обыграл ЦСКА", "Hanford nuclear site cleanup",
    "Hempstead mayor resigns", "ТГК-1 повысила тарифы", "Mice were anesthetized with ketamine/xylazine",
    "Alcohol and memory in older adults", "Garden weeds and climate", "Кусок марли", "Новый телескоп увидел галактику",
    "Reggaeton tops the charts", "Phase 2 results due in 2026", "",
])
def test_topic_match_ignores_lookalikes(text):
    assert not topic_match(text)


def test_topic_match_takes_only_strings():
    assert not topic_match(None)


@pytest.mark.parametrize("text", [
    "Noribogaine reduces opioid withdrawal in rats", "Tabernanthe iboga root bark",
    # Russian: inflection, case, ё and the spellings in use
    "КАННАБИС ПОМОГ ПРИ ХРОНИЧЕСКОЙ БОЛИ", "Курильщики конопли спят хуже", "Поле засеяли коноплёй",
    "Марихуаны в Канаде стало больше", "Каннабиноиды снизили тревожность у мышей", "Канабис в аптеках",
    "ТГК нашли в грудном молоке", "Психоделиков стали изучать снова", "Исследование псилоцибиновых грибов",
    "5-MeO-ДМТ в клинике", "Айяуаска помогла ветеранам", "Аяваска и горе", "Аяхуаска в Перу", "Ибога в Габоне",
    "Пейот и коренные народы", "Волшебные грибы легализовали в Колорадо", "Магические грибы в Орегоне",
    "Лизергиновая кислота", "Диметилтриптамин и околосмертный опыт", "Король даба Кинг Табби",
    "В дабе главное — бас", "Дабовый ремикс", "Раста-культура на Ямайке", "Зигги Марли выпустил альбом",
    "Денсхолл-вечеринка",
])
def test_topic_match_more_spellings(text):
    assert topic_match(text)


@pytest.mark.parametrize("text", [
    "Дабы избежать штрафов, банки подняли ставки", "Грибы в лесу", "Скорость роста экономики",
    "ТГК-2 подняла тарифы", "Ибо так решил суд", "Ribosome profiling of yeast",
])
def test_topic_match_more_lookalikes(text):
    assert not topic_match(text)


def test_noribogaine_preprint_passes():
    assert drop_reason(item("Noribogaine reduces opioid withdrawal in rats", meta={"version": "1"}), PREPRINTS) is None


@pytest.mark.parametrize("text, expected", [
    ("Microdosing and mood", True), ("Микродозинг и креативность", True), ("Microdose psilocybin study", True),
    ("Microdosing Ozempic is the new wellness trend", False), ("Microdosing semaglutide for weight loss", False),
    ("Микродозинг оземпика", False), ("Phase 0 microdosing study of a new antibiotic", False),
    ("Ozempic microdosing and LSD", True),   # another topic word still counts
])
def test_topic_microdosing_but_not_the_glp1_fad(text, expected):
    assert topic_match(text) is expected


# ---- blocked channels ----

@pytest.mark.parametrize("kw", [
    {"title": "Best source for shrooms: @microdosetoday"},
    {"title": "Repost", "summary": "via t.me/GrowBud"},
    {"title": "Repost", "summary": "see https://t.me/s/podsalyami/12"},
    {"title": "Albums", "url": "https://t.me/Reggae_Music_Albums/77"},
    {"title": "Repost", "meta": {"link": "https://telegram.me/cannabiscultivation"}},
    {"title": "Repost", "meta": {"forwarded_from": "@ReggaeFoundation"}},
    {"title": "Repost", "summary": "tg://resolve?domain=MicrodoseToday"},
    {"title": "Repost", "meta": {"links": ["https://telegram.dog/growpotbot"]}},
    # the subdomain form Telegram also resolves
    {"title": "Repost", "summary": "via microdosetoday.t.me"},
    {"title": "Repost", "meta": {"links": ["https://GrowBud.t.me/"]}},
])
def test_blocked_channels_anywhere(kw):
    for source in (CURATED, SCIENCE, TG, REDDIT):
        assert drop_reason(item(**{"title": "", **kw}), source) == "blocklist"


def test_blocked_channel_as_source_and_lookalike_names():
    assert drop_reason(item("Psilocybin news"), telegram_source("MicrodoseToday")) == "blocklist"
    assert drop_reason(item("Psilocybin news, thanks @microdosetoday_fan"), TG) is None
    assert drop_reason(item("Psilocybin news", "mail growbudget@example.org"), TG) is None
    assert drop_reason(item("Psilocybin news", "see microdosetoday_fan.t.me"), TG) is None


def test_blocklist_comes_before_every_other_reason():
    assert drop_reason(item("Weather today, ask @growpotbot"), BROAD_EN) == "blocklist"
    assert drop_reason(post("Shop @microdosetoday", removed=True), REDDIT) == "blocklist"


# ---- broad feeds: topic and crime ----

def test_broad_feeds_keep_only_the_topics():
    assert drop_reason(item("Alcohol and memory in older adults"), BROAD_EN) == "offtopic"
    assert drop_reason(item("Новый телескоп увидел галактику"), BROAD_RU) == "offtopic"
    assert drop_reason(item("Psilocybin eases anorexia symptoms"), BROAD_EN) is None
    assert drop_reason(item("Study", "Psilocybin eases anorexia symptoms"), BROAD_EN) is None
    # categories alone do not make an item topical
    assert drop_reason(item("Alcohol and memory", categories=("psychedelic drugs",)), BROAD_EN) == "offtopic"
    # curated feeds without topic_required keep everything
    assert drop_reason(item("Governor signs budget"), CURATED) is None


@pytest.mark.parametrize("title, summary", [
    ("Police arrested two men with 40 kg of cannabis", ""),
    ("Customs seized a record shipment of MDMA", ""),
    ("Psilocybin trafficking ring dismantled", ""),
    ("Cannabis smuggling suspect extradited", ""),
    ("Marijuana dealer sentenced to 8 years", ""),
    ("Police raid on an illegal cannabis farm", ""),
    ("Man pleads guilty to LSD distribution", ""),
    ("В Москве задержан наркокурьер с марихуаной", ""),
    ("У студента изъяли 2 кг гашиша", ""),
    ("Закладчик мефедрона и марихуаны получил срок", ""),
    ("Суд приговорил продавца ЛСД к 10 годам", ""),
    ("Каннабис", "Возбуждено уголовное дело по статье 228"),
    ("Контрабанда каннабиса через границу", ""),
    ("Задержали сбытчика марихуаны", "Его обвиняют в сбыте наркотиков"),
    ("Cannabis-Razzia in Berlin", "Drei Personen festgenommen"),
])
def test_drug_crime_dropped_from_broad_feeds(title, summary):
    source = BROAD_RU if any("а" <= c <= "я" for c in title.lower()) else BROAD_EN
    assert drop_reason(item(title, summary), source) == "crime"


@pytest.mark.parametrize("title, summary", [
    ("Senate votes on cannabis legalization bill", "Opponents warned of more arrests"),
    ("Governor pardons 10,000 marijuana convictions", "People convicted of possession will be cleared"),
    ("Germany: Bundestag passes cannabis law", ""),
    ("Госдума отклонила законопроект о легализации каннабиса", ""),
    ("В Германии легализовали каннабис", "Уголовные дела закроют"),
    ("CBD reduces seizures in children with Dravet syndrome", ""),
    ("Cannabis users had fewer cardiac arrests", ""),
    ("THC induces cell cycle arrest in tumour cells", ""),
    ("Псилоцибин и задержка развития у мышей", ""),
])
def test_policy_and_science_are_not_crime(title, summary):
    source = BROAD_RU if any("а" <= c <= "я" for c in title.lower()) else BROAD_EN
    assert drop_reason(item(title, summary), source) is None


def test_crime_only_on_broad_feeds():
    assert drop_reason(item("Cannabis smuggling suspect extradited"), CURATED) is None


@pytest.mark.parametrize("title, summary", [
    ("Cannabidiol induces G2/M arrest in glioma cells", ""),
    ("CBD induces G1 phase arrest and apoptosis", ""),
    ("THC and cell-cycle arrest", "Cells were arrested in G1 after 24 h"),
    ("Cannabis use and cardiorespiratory arrest", ""),
    ("THC alters CB1 receptor trafficking in neurons", ""),
    ("Cannabinoid receptors", "Membrane trafficking of CB1 is impaired; vesicle trafficking was normal"),
    ("Investors seized on the psilocybin hype", ""),
    ("Минздрав предложил изъять каннабидиол из перечня наркотических средств", ""),
    ("Учёные осуждают запрет психоделиков", ""),
    ("Президент осудил употребление ЛСД", ""),
    ("Мечта о медицинском каннабисе может сбыться", ""),
    # seizures of the body, not of drugs
    ("Cannabidiol raises the hyperthermia threshold in Scn1a+/- mice", "Untreated mice seized at 41.2 C"),
    ("Animals seized less often after CBD", ""),
    ("CBD in Dravet syndrome", "Patients who seized during the night slept worse"),
    ("Cannabidiol and febrile seizures", "Vehicle-treated rats seized within 5 min; one mouse seized at a lower "
                                          "temperature; treated pups seized more often than controls"),
])
def test_crime_words_in_other_senses_are_not_crime(title, summary):
    source = BROAD_RU if any("а" <= c <= "я" for c in title.lower()) else PREPRINTS
    assert drop_reason(item(title, summary, meta={"version": "1"}), source) is None


@pytest.mark.parametrize("title, summary", [
    ("Police seized 2 tonnes of cannabis", ""),
    ("Man arrested for selling cannabis", ""),
    ("Cannabis seized at the border", ""),
    ("Customs seized more than 2 tonnes of cannabis", ""),
    ("Cannabis", "Officers seized at 3 locations"),
])
def test_drug_crime_still_caught_on_preprint_feeds(title, summary):
    assert drop_reason(item(title, summary, meta={"version": "1"}), PREPRINTS) == "crime"


@pytest.mark.parametrize("title", [
    "Drug traffickers use Telegram to sell MDMA", "Man accused of trafficking cannabis",
    "Trafficking of illegal drugs such as cannabis rises", "Cannabis trafficking network dismantled",
    "Arrested for psilocybin possession", "Суд в Москве осудил продавца ЛСД", "Его осудили на 8 лет за марихуану",
    "Осуждённый за марихуану вышел на свободу",
    "В Москве задержан курьер с марихуаной", "Полиция задержала начальника отдела с гашишем",
    "Полиция задержала ростовчанина с коноплёй", "Задержали рецидивиста с ЛСД",
    "Сенатора арестовали за хранение кокаина и марихуаны", "Экс-депутата Госдумы задержали с марихуаной",
    "Двоих депутатов задержали с гашишем", "Каннабидиол задержал рост опухоли, а полиция задержала курьера с ЛСД",
    "Полиция задержала курьера с марихуаной: по законодательству ему грозит 15 лет",
    # courts and arrests that read like idioms or delays
    "Суд в Сочи приговорил бизнесмена к шести годам колонии за выращивание конопли",
    "В Петербурге приговорили бизнесмена, продававшего марихуану через Telegram",
    "В Москве вынесли приговор бизнесмену, торговавшему марихуаной",
    "Суд вынес приговор бизнес-партнёру торговца марихуаной",
    "Подростка задержали после смерти друга от передозировки: в его квартире нашли гашиш",
    "Музыканта задержали до начала концерта, у него нашли марихуану",
    "Задержали до начала суда: продавца каннабиса отправили в СИЗО",
    "Полиция задержала подозреваемого в гибели подростка, отравившегося марихуаной",
    "В Петербурге задержали подозреваемого в смерти студента после употребления мефедрона и гашиша",
    "Полиция задержала виновника гибели подростка, отравившегося марихуаной",
    "МВД задержало курьера и начало проверку: у него нашли марихуану",
    "В Москве задержали главу отдела развития компании с марихуаной",
    "На Сенатской площади задержали закладчика с марихуаной", "Депутаты горсовета задержаны с марихуаной",
])
def test_drug_crime_still_caught(title):
    source = BROAD_RU if any("а" <= c <= "я" for c in title.lower()) else BROAD_EN
    assert drop_reason(item(title), source) == "crime"


# ---- science ----

@pytest.mark.parametrize("pubtypes", [
    ["Comment"], ["Letter"], ["Editorial"], ["Erratum"], ["Published Erratum"], ["Retraction of Publication"],
    ["Retracted Publication"], ["News"], ["Newspaper Article"], ["Biography"], ["Portrait"], ["Interview"],
    ["Personal Narrative"], ["Journal Article", "comment"], ["EDITORIAL"], "Letter",
])
def test_science_drops_non_research_pubtypes(pubtypes):
    assert drop_reason(item("Psilocybin and depression", meta={"pubtypes": pubtypes}), SCIENCE) == "pubtype"


@pytest.mark.parametrize("meta", [
    {"pubtypes": ["Journal Article"]}, {"pubtypes": ["Randomized Controlled Trial", "Journal Article"]},
    {"pubtypes": ["Review", "Systematic Review"]}, {"pubtypes": []}, {}, {"pubtypes": None},
    {"pubtypes": ["Comment on a trial"]},
    {"version": "1"}, {"version": 1}, {"change": "new"}, {"change": "results"}, {"change": "status"},
])
def test_science_keeps_research(meta):
    assert drop_reason(item("Psilocybin and depression", meta=meta), SCIENCE) is None


@pytest.mark.parametrize("meta", [{"version": "2"}, {"version": 3}, {"change": "update"}])
def test_science_drops_minor_updates(meta):
    assert drop_reason(item("Psilocybin and depression", meta=meta), SCIENCE) == "minor_update"


def test_science_ignores_trade_words_and_doses():
    title = "Psilocybin 25 mg vs 1 mg: cost per patient of $1,200 and free shipping of samples"
    assert drop_reason(item(title), SCIENCE) is None


def test_preprints_need_the_topic():
    assert drop_reason(item("Gut microbiome in mice", meta={"version": "1"}), PREPRINTS) == "offtopic"
    assert drop_reason(item("Cannabis use in pregnancy", meta={"version": "1"}), PREPRINTS) is None
    assert drop_reason(item("Cannabis use in pregnancy", meta={"version": "2"}), PREPRINTS) == "minor_update"
    # v1 and v2 in the same window: a new preprint that was revised quickly, not an old one revised
    assert drop_reason(item("Cannabis use in pregnancy", meta={"version": "2", "first_in_window": True}),
                       PREPRINTS) is None


# ---- curated news sites ----

@pytest.mark.parametrize("kw", [
    {"title": "Weekly deals on vapes", "categories": ("deals",)},
    {"title": "Grow week", "categories": ("Grow", "news")},
    {"title": "Sponsored: the best gummies of the season"},
    {"title": "[Sponsored] Inside a CBD factory"},
    {"title": "Inside a CBD factory (Sponsored)"},
    {"title": "Inside a CBD factory | Sponsored"},
    {"title": "Partner Content: a new cannabis brand"},
    {"title": "Advertorial: delta-8 explained"},
    {"title": "Sponsored content: meet the founders"},
])
def test_curated_drops_sponsored(kw):
    assert drop_reason(item(**kw), HIGHTIMES) == "sponsored"


@pytest.mark.parametrize("kw", [
    {"title": "NIH-sponsored psilocybin trial begins"},
    {"title": "Government sponsored research on cannabis", "categories": ("politics",)},
    {"title": "Industry-sponsored studies under scrutiny"},
])
def test_curated_keeps_sponsored_research(kw):
    assert drop_reason(item(**kw), HIGHTIMES) is None


def test_drop_categories_are_per_source():
    assert drop_reason(item("Grow week", categories=("grow",)), CURATED) is None


@pytest.mark.parametrize("title, summary", [
    ("New vape line", "Use code HIGH20 at checkout"),
    ("Holiday sale", "Order now and get free shipping"),
    ("Gummies are back", "Buy now while supplies last"),
    ("Our seeds", "Promo code: SPRING"),
    ("Discount code inside", ""),
    ("Grab a coupon for 4/20", ""),
    ("Limited drop", "DM us for wholesale"),
    ("Limited drop", "Visit our shop for the full range"),
    ("Merch", "Buy yours at https://shop.example.com/hoodie"),
    ("Новая линейка масел", "Промокод НЕСТОР на скидку 10%"),
    ("Масла КБД", "Скидка 15% по коду CBD15"),
    ("Новинки", "Закажите сегодня, бесплатная доставка по всей России"),
    ("Новинки", "Заказать можно в нашем магазине"),
    ("Новинки", "Заказать можно в нашем интернет-магазине"),
    ("Каннабис", "Заказать можно у нас на сайте"), ("Каннабис", "Где заказать КБД-масло? Пишите"),
    ("Каннабис", "Успейте заказать со скидкой"), ("Каннабис", "Заказывайте по ссылке"),
    ("Новинки", "Пишите в ЛС"),
    ("Новинки", "Подробности в личку"),
])
def test_curated_trade_on_explicit_shop_signals(title, summary):
    assert drop_reason(item(title, summary), CURATED) == "trade"


@pytest.mark.parametrize("title, summary", [
    ("Cannabis prices fall as the market matures", "Wholesale price per pound dropped 20%"),
    ("Michigan marijuana sales hit $3.1 billion", "Tax revenue tops $1.2 billion for the state"),
    ("Psilocybin therapy could cost $10,000 per patient", "Insurers weigh coverage"),
    ("Cannabis delivery services legalized in New Jersey", "Retail stores and delivery licenses"),
    ("New York lets adults buy cannabis at licensed stores", "Sales begin on Monday"),
    ("President signs executive order on rescheduling", ""),
    ("Dispensaries must comply with the code of conduct", ""),
    ("Цены на каннабис в Германии падают", "Рынок растёт, налоги тоже"),
    ("В Таиланде запретили доставку каннабиса", "Магазины обязаны получать лицензию"),
    ("Роскомнадзор заблокировал интернет-магазины CBD-масел", ""),
    ("Минздрав решил заказать исследование медицинского каннабиса", ""),
    ("Минздрав решил заказать исследование каннабиса на сайте госзакупок", ""),
    # ЛС: лекарственные средства
    ("Каннабиноиды в ЛС: обзор рынка", "ЛС на основе каннабиса зарегистрированы в 12 странах"),
    ("Trump signs executive order to reschedule marijuana", "Details: https://www.dea.gov/products/scheduling"),
])
def test_curated_market_and_tax_news_is_not_trade(title, summary):
    assert drop_reason(item(title, summary), CURATED) is None


@pytest.mark.parametrize("title", [
    "How to grow cannabis indoors", "How to Make Cannabutter", "How to dose edibles", "How to microdose psilocybin",
    "How to extract CBD at home", "How to take magic mushrooms safely", "Grow guide: week 3", "Growing tips for autumn",
    "Beginner's guide to growing autoflowers", "Microdosing guide for beginners", "Best strains for sleep",
    "Top 10 indica strains of 2026", "Как вырастить коноплю дома", "Как сделать масло из каннабиса",
    "Как принимать псилоцибин", "Как правильно дозировать CBD", "Как приготовить отвар", "Лучшие сорта для сна",
    "Wie man Cannabis zu Hause anbaut",
    "How to make edibles at home", "How to make cannabis-infused olive oil", "How to make a bong out of an apple",
    "How to make DMT", "How to roll a joint", "How to grow weed outdoors", "How to take CBD oil",
    "Как сделать гашиш", "Как сделать чай из грибов", "Как принять ЛСД безопасно",
])
def test_curated_drops_guides(title):
    assert drop_reason(item(title), CURATED) == "howto"


@pytest.mark.parametrize("title, summary", [
    ("How to make sense of the new cannabis law", ""),
    ("How psilocybin changes the brain", ""),
    ("Senators ask how to fund psychedelic research", ""),
    ("Grow licenses approved in Ohio", ""),
    ("Study of strains of bacteria in hemp fields", ""),
    ("New study on psilocybin", "Researchers explain how to dose patients in trials"),   # guides: title only
    ("How to make psychedelic therapy affordable", ""),
    ("How to make cannabis legal in Texas", ""),
    ("How to take on Big Pharma with psilocybin", ""),
    ("How to take part in a psilocybin trial", ""),
    ("How to take psychedelic medicine to scale", ""),
    ("How to roll out psilocybin services in Colorado", ""),
    ("How to grow your cannabis business in a tough market", ""),
    ("Как сделать психоделическую терапию доступной", ""),
    ("Как принимать решения: что психоделики говорят о выборе", ""),
])
def test_curated_keeps_news_about_guides(title, summary):
    assert drop_reason(item(title, summary), CURATED) is None


# ---- community: Reddit and Telegram ----

def test_community_reddit_flags():
    assert drop_reason(post("Bad Ras", removed=True), REDDIT) == "removed"
    assert drop_reason(post("Bad Ras", over_18=True), REDDIT) == "nsfw"
    assert drop_reason(post("Weekly discussion thread", stickied=True), REDDIT) == "stickied"
    assert drop_reason(post("Bad Ras", score=1), REDDIT) == "low_score"
    assert drop_reason(post("Bad Ras", score=4.5), REDDIT) == "low_score"
    assert drop_reason(post("Bad Ras", score=5), REDDIT) is None
    assert drop_reason(post("Bad Ras", removed=False, over_18=False), REDDIT) is None
    assert drop_reason(item("Bad Ras", url=PERMALINK), REDDIT) is None   # no score: not judged on it
    assert drop_reason(post("Bad Ras", is_self=True), REDDIT) is None    # self posts are fine without links_only


def test_links_only_subreddits_take_research_and_news_links():
    assert drop_reason(post("What helped me", is_self=True), LINKS_ONLY) == "self_post"
    assert drop_reason(post("Psilocybin and OCD", link="https://www.nature.com/articles/s41398-024-03103-7",
                            domain="nature.com"), LINKS_ONLY) is None
    assert drop_reason(post("Microdosing study", link="https://pubmed.ncbi.nlm.nih.gov/38012345/"), LINKS_ONLY) is None
    assert drop_reason(post("Microdosing study", domain="doi.org"), LINKS_ONLY) is None
    assert drop_reason(post("Microdosing study", link="https://hub.jhu.edu/2026/10/01/psilocybin/"), LINKS_ONLY) is None
    assert drop_reason(post("Microdosing news", link="https://www.psypost.org/microdosing-x/"), LINKS_ONLY) is None
    assert drop_reason(post("Microdosing explained", link="https://youtu.be/abcdefghijk", domain="youtu.be"),
                       LINKS_ONLY) == "offtopic"
    assert drop_reason(post("My setup", domain="i.redd.it"), LINKS_ONLY) == "offtopic"
    assert drop_reason(post("Microdosing study", link="https://nature.com.evil.example/x"), LINKS_ONLY) == "offtopic"
    assert drop_reason(post("No link at all"), LINKS_ONLY) == "offtopic"     # the permalink is reddit.com
    # universities and agencies anywhere: edu, edu.au, ac.uk, gov, gov.uk, nhs.uk, and listed institutes
    for link in ("https://www.unimelb.edu.au/news/x", "https://www.kcl.ac.uk/news/x", "https://www.samhsa.gov/x",
                 "https://www.gov.uk/government/news/x", "https://www.nhs.uk/conditions/x", "https://www.unibas.ch/en/x",
                 "https://www.researchgate.net/publication/1_Psilocybin"):
        assert drop_reason(post("Psilocybin study", link=link), LINKS_ONLY) is None, link
    for link in ("https://evil.ac/x", "https://edu.evil.com/x", "https://gov.example.com/x", "https://myedu.com/x"):
        assert drop_reason(post("Psilocybin study", link=link), LINKS_ONLY) == "offtopic", link
    # the link itself, when the adapter puts it in url
    assert drop_reason(item("Psilocybin and OCD", url="https://www.thelancet.com/x", meta={"score": 50}),
                       LINKS_ONLY) is None


def test_research_domains():
    assert "youtube.com" not in RESEARCH_NEWS_DOMAINS and "reddit.com" not in RESEARCH_NEWS_DOMAINS
    for host in ("marijuanamoment.net", "norml.org", "reggaeville.com", "dailyreggae.com", "lucid.news",
                 "doi.org", "biorxiv.org", "nih.gov"):
        assert host in RESEARCH_NEWS_DOMAINS


@pytest.mark.parametrize("title, summary", [
    ("Psychedelic meetup, Eintritt 5 €", "Tickets 5 € at the door"),
    ("Integration circle", "Contribution €5"),
    ("Therapy sessions", "Prices: €60/€90"),
    ("Ounce (28 g): $198", ""),
    ("Albums", "Only 20 USD"),
    ("Albums", "10 EUR each"),
    ("Vinyl", "£10 plus postage"),
    ("Лекция о психоделиках", "Вход 300 ₽"),
    ("Лекция о психоделиках", "Вход 300 руб."),
    ("Лекция", "Билеты по 1 500 рублей"),
    ("Грибы", "В наличии"),
    ("Наличие уточняйте", ""),
    ("Доставка по городу", ""),
    ("Новинка", "Пишите в ЛС"),
    ("Новинка", "Пишите в личку"),
    ("Новинка", "Все вопросы в лс"),
    ("Новинка", "Наш магазин открыт"),
    ("Fresh batch", "In stock now"),
    ("Spores for sale", ""),
    ("Selling my vinyl collection", ""),
    ("Worldwide shipping", ""),
    ("Same-day delivery", ""),
    ("Questions? DM me", ""),
    ("DM for prices", ""),
    ("PM me", ""),
    ("Contact @kyle_gary", ""),
    ("Reach me on Wickr", ""),
    ("WhatsApp only", ""),
    ("Telegram: @plug_123", ""),
    ("Where to buy spores?", ""),
    ("Use code TRIP for 10% off", ""),
    ("Lecture", "Contact: @kyle_gary"),
    ("Spores", "Telegram @plug_123"),
    ("Spores", "Message me for prices"),
    ("Spores", "Send me a DM"),
    ("Spores", "Prices in the DMs"),
    ("Spores", "DM if interested"),
    ("Spores", "Hit me up"),
    ("Meetup", "Eintritt: 10,-"),
    ("Vinyl", "10 quid"),
    ("Retreat", "A$50 per night"),
    ("Retreat", "Ticket C$20"),
    ("Грибы", "Цена 500р"),
    ("Грибы", "Прайс по запросу"),
    ("Грибы", "Цена договорная"),
    ("Грибы", "Цена в директе"),
    ("Workshop", "Anmeldung per DM"),
    ("Pilzsporen", "Schreib mir eine PN"),
    ("Verkaufe Pilzsporen", ""),
    ("Sporen", "Preisliste auf Anfrage"),
    # price labels, also without a currency
    ("Грибы", "Цена: 2 500"),
    ("Грибы", "Цена 1500 за упаковку"),
    ("Spores", "Price: 30"),
    ("Sporen", "Preis: 30"),
    ("Workshop", "Kosten: 20"),
    ("Vortrag", "Eintritt: 10"),
    ("Психоделическая терапия", "Цена терапии 3000"),
    ("Терапия", "Стоимость терапии 5000 за сессию"),
    ("Терапия", "Цена терапии договорная"),
    ("Грибы", "Цена — 1500"),
    ("Грибы", "Цены: от 500"),
    ("Sporen", "Preise: 30"),
    ("Spores", "Price - 30"),
    # currency codes
    ("Грибы", "1500 RUB"),
    ("Spores", "50 USDT"),
    # contacts, delivery, availability
    ("Spores", "PM for prices"),
    ("Sporen", "Bei Interesse PN"),
    ("Грибы", "Подробности в личных сообщениях"),
    ("Грибы", "Пишите мне в телеграм"),
    ("Грибы", "Напишите в тг"),
    ("Грибы", "Пишите в телегу"),
    ("Грибы", "Отправка по всей России"),
    ("Spores", "Ships worldwide"),
    ("Spores", "We ship to EU"),
    ("Грибы", "Самовывоз"),
    ("Pilzsporen", "jetzt erhältlich"),
    ("Wo kann man Psilos kaufen?", ""),
    ("Spores", "Prices from 30"),
    ("Spores", "Price 30 per gram"),
    ("Грибы", "Цены от 1500"),
    ("Workshop", "Kosten 20"),
    ("Грибы", "100 TON"),
    ("Грибы", "Оплата картой или переводом"),
    ("Spores", "Contact: t.me/dealerbot"),
    ("Грибы", "Связь: https://t.me/dealerbot"),
])
def test_community_strict_trade(title, summary):
    assert drop_reason(item(title, summary), TG) == "trade"


@pytest.mark.parametrize("title, summary", [
    ("New study: psilocybin therapy reduced depression for 5 years", ""),
    ("Lecture on the history of LSD research next Tuesday at the university", ""),
    ("Reggae Sumfest 2026 line-up announced", ""),
    # a public discussion, not a contact for buying
    ("Обсуждение", "Пишите свои мысли в комментариях"),
    ("We shipped our new album", ""),
    ("Relationship and friendship after a retreat", ""),
    ("The Price of Freedom: a new dub album", ""),
    ("Die Kosten der Studie trägt die Universität", ""),
    ("Цены на каннабис в Германии выросли на 20%", ""),
    ("Цена вопроса — свобода", ""),
    # years, terms and growth are not prices; news idioms are not contacts; a ship is not shipping
    ("Лечение кетамином", "Стоимость лечения в 2025 году выросла, пишет Reuters"),
    ("Психоделики", "Средняя цена в 2025 г. выросла"),
    ("Психоделики", "Стоимость терапии выросла на 20%"),
    ("Психоделики", "Стоимость выросла в 2 раза"),
    ("Учёный в тюрьме", "Цена ошибки: 3 года колонии"),
    ("Вопрос цены: 5 лет спустя", ""),
    ("Закон о каннабисе", "В личной переписке министр признал, что закон провалился"),
    ("The Windrush generation", "They came on the ship to the UK in 1948 and brought sound systems"),
    ("UK PM for drug reform? Not this year", ""),
    ("Lecture at eight pm for students", ""),
    ("Die Studien kosten 20 Millionen Euro im Jahr", ""),
    ("Цены от 2025 года", "Аналитики сравнили"),
    ("Psychedelic research", "Funding: price of the new MRI scanner was discussed in parliament"),
    ("Toronto conference", "TONIGHT: talk on ayahuasca"),
])
def test_community_trade_lookalikes_pass(title, summary):
    assert drop_reason(item(title, summary), TG) is None


@pytest.mark.parametrize("title, summary", [
    ("Ayahuasca integration talk, livestream Sept 26", "Join us online, free entry"),
    ("Doors open 8 pm for the lecture on psilocybin", ""),
    ("Best-selling reggae album of the year", ""),
    ("Phase 2 psilocybin trial in 2026", "40% of 120 patients improved"),
    ("Drug delivery of psilocybin through the nose", ""),
    ("Her vocal delivery on this dub track", ""),
    ("Sharing circle on Sunday", "We do not provide any illegal substances"),
    ("Новый выпуск подкаста о психоделиках", "Слушайте по ссылке"),
    ("Psilocybin and stocks", "Cannabis stocks rally in stock market"),
    ("Integration circle", "Starts at 6 PM if the weather allows"),
    ("Интеграция", "Встречи 2 р в неделю"),
    ("Лекция о психоделиках", "Вопросы присылайте в директорат фестиваля"),
    ("How many hits did Bob Marley have?", ""),
])
def test_community_keeps_clean_posts(title, summary):
    assert drop_reason(item(title, summary), TG) is None


@pytest.mark.parametrize("title, summary", [
    ("Took 2 g of mushrooms last night", ""),
    ("Took 3.5g", ""),
    ("LSD 100 µg", ""),
    ("LSD 100 μg", ""),
    ("LSD 100ug", ""),
    ("LSD 150 mcg", ""),
    ("Psilocybin 0.1 g", ""),
    ("Started at 1-2 g", ""),
    ("MDMA 1.5 mg/kg", ""),
    ("Drops", "5 ml tincture"),
    ("Псилоцибин 0,5 г", ""),
    ("ЛСД 100 мкг", ""),
    ("Микродозинг: 50 мг каждые три дня", ""),
    ("Как я начал", "1-2 гр грибов"),
    ("По 2 грамма", ""),
    ("Тинктура 5 мл", ""),
    ("My dosing schedule", ""),
    ("Microdose schedule that works for me", ""),
    ("Fadiman protocol week 3", ""),
    ("The Stamets stack", ""),
    ("How much should I take for a first trip?", ""),
    ("What dose is best for anxiety", ""),
    ("Low-dose psilocybin study", ""),
    ("Дозировка псилоцибина", ""),
    ("Какая доза нужна", ""),
    ("Схема приёма микродоз", ""),
    ("Сколько нужно принимать", ""),
    ("Die richtige Dosis", ""),
    ("Dose of the day", "2 g of shrooms"),
    ("My dose of psilocybin", ""),
    ("How much psilocybin should I take?", ""),
    ("How many grams for a first trip", ""),
    # units written out
    ("100 micrograms of LSD is a strong trip", ""),
    ("Psilocybin", "125 milligrams"),
    ("Псилоцибин", "120 миллиграмм"),
    ("100 микрограмм ЛСД", ""),
    ("Psilocybin", "25 Milligramm"),
    ("Half a tab", ""),
    ("Пол марки хватило", ""),
    ("Четверть марки на двоих", ""),
    ("1/2 tab was plenty", ""),
    ("Picked up an eighth", ""),
])
def test_community_drops_dosing(title, summary):
    assert drop_reason(item(title, summary), TG) == "dosing"


@pytest.mark.parametrize("title", [
    "Microdosing and creativity: new study", "Микродозинг и креативность: новое исследование",
    "Phase 2 trial with 5G-connected sensors", "Psilocybin in 2026: 40% of patients improved",
    "Статья о психоделиках, 2025 г.", "3 ML models predict psilocybin response", "Dub vinyl stacks",
    "Your daily dose of dub: King Tubby", "Sunday morning dose of roots reggae",
    "A dose of reality for psychedelic hype", "How many Americans use psychedelics? New survey",
    "Сколько нужно времени, чтобы психоделическая терапия стала легальной",
    "Ziggy Marley won 8 Grammys", "Nominees for the 2026 Grammy for Best Reggae Album", "Пол Маркс споёт регги",
    "An eighth of Americans have tried psilocybin", "Dub Club, 24/7 vibes", "Bob Marley tribute in 3/4 time",
])
def test_community_dose_lookalikes_pass(title):
    assert drop_reason(item(title), TG) is None


@pytest.mark.parametrize("title", ["How to grow mushrooms at home", "Как вырастить грибы", "Grow guide for beginners",
                                   "Lemon tek", "Extracting DMT from mimosa", "Extraction of mescaline from cacti",
                                   "DMT extraction from mimosa"])
def test_community_drops_guides(title):
    assert drop_reason(item(title), TG) == "howto"


@pytest.mark.parametrize("title", ["How to make dub delays in Ableton", "How to grow your reggae fanbase"])
def test_community_music_tutorials_pass(title):
    assert drop_reason(post(title), REDDIT) is None


def test_community_order_of_reasons():
    # Before the archive's second look, the removal mark and the score are not final.
    assert drop_reason(post("Spores for sale, 2 g", unsettled=True, removed=True), REDDIT) == "unsettled"
    assert drop_reason(post("Bad Ras", unsettled=False), REDDIT) is None
    assert drop_reason(post("Spores for sale, 2 g", removed=True, over_18=True), REDDIT) == "removed"
    assert drop_reason(post("Spores for sale, 2 g", over_18=True), REDDIT) == "nsfw"
    assert drop_reason(post("Spores for sale, 2 g", score=1), REDDIT) == "low_score"
    assert drop_reason(post("Spores for sale, 2 g"), REDDIT) == "trade"
    assert drop_reason(post("How to grow at 2 g per jar"), REDDIT) == "dosing"
    assert drop_reason(post("Study", is_self=True, score=1), LINKS_ONLY) == "self_post"


def test_filters_read_the_whole_post_not_the_summary():
    """Adapters clip the stored summary; trade, doses and blocked channels at the end of a long post still count."""
    head = "Integration circle notes. " * 80
    tail = {"trade": "Bestellung per PN, 30 € pro Packung", "dosing": "We each took 2 g.",
            "blocklist": "More at t.me/growbud"}
    for reason, text in tail.items():
        long_post = Item(ext_id="1", url=PERMALINK, title="Integration circle", summary=head[:1499] + "…",
                         full_text=head + text, meta={"score": 50, "is_self": True})
        assert drop_reason(long_post, TG) == reason
        assert drop_reason(long_post, REDDIT) == reason
    assert drop_reason(Item(ext_id="1", url=PERMALINK, title="Integration circle", summary=head[:1499] + "…",
                            full_text=head), TG) is None


def test_community_reads_the_link_and_categories():
    assert drop_reason(post("Nice tee", link="https://store.example.com/reggae-tees"), REDDIT) is None
    assert drop_reason(post("Buy here", link="https://shop.example.com/spores"), REDDIT) == "trade"
    assert drop_reason(item("Nice", categories=("for sale",)), TG) == "trade"


def test_curated_rules_are_not_strict():
    """The owner's trade rule in full applies to community posts only."""
    text = ("Psychedelic meetup, Eintritt 5 €", "Tickets 5 € at the door")
    assert drop_reason(item(*text), CURATED) is None
    assert drop_reason(item(*text), TG) == "trade"
    assert drop_reason(item("Psilocybin 25 mg beats placebo"), CURATED) is None
    assert drop_reason(item("Psilocybin 25 mg beats placebo"), TG) == "dosing"


# ---- the registry as configured ----

@pytest.mark.parametrize("source", [*SOURCES, TG], ids=lambda s: s.key)
def test_clean_topical_items_pass_every_source(source):
    meta = {"score": 50, "is_self": False, "link": "https://www.nature.com/articles/s41586-026-00001-1"}
    assert drop_reason(item("Psilocybin therapy and reggae culture: a new study", "Researchers report results.",
                            meta=meta), source) is None


def test_stored_reasons():
    assert STORED_REASONS == {"blocklist", "trade", "dosing", "howto", "crime", "sponsored"}
    assert {"offtopic", "unsettled", "removed", "nsfw", "stickied", "self_post", "low_score", "pubtype",
            "minor_update"}.isdisjoint(STORED_REASONS)


# ---- clean_text ----

@pytest.mark.parametrize("text, expected", [
    ("Participants received 25 mg of psilocybin (25 mg) twice.", "Participants received psilocybin twice."),
    ("Пациенты получали 25 мг псилоцибина.", "Пациенты получали псилоцибина."),
    ("Псилоцибин (0,5 г) и ЛСД (100 мкг) сравнили.", "Псилоцибин и ЛСД сравнили."),
    ("Participants took 1–2 g of dried mushrooms.", "Participants took dried mushrooms."),
    ("Ketamine at 10 mg/kg reduced immobility.", "Ketamine reduced immobility."),
    ("The dose rose from 25 mg to 50 mg over 6 weeks.", "The dose rose over 6 weeks."),
    ("Приняли от 1 до 2 г грибов.", "Приняли грибов."),
    ("LSD (100 µg/day, orally) for anxiety", "LSD (orally) for anxiety"),
    ("They gave a single dose of 25 mg psilocybin.", "They gave psilocybin."),
    ("Псилоцибин в дозе 25 мг помог.", "Псилоцибин помог."),
    ("a 25-mg dose", "a dose"),
    ("Подробнее: https://example.org/x?a=1.", "Подробнее."),
    ("See (https://doi.org/10.1/abc) and www.example.com/page, then t.me/channel/5.", "See and, then."),
    ("Phase 2 trial in 2026: 40% of 120 patients (n = 120) improved.",
     "Phase 2 trial in 2026: 40% of 120 patients (n = 120) improved."),
    ("В 2025 г. исследование фазы 3 охватило 233 человека.", "В 2025 г. исследование фазы 3 охватило 233 человека."),
    ("5G networks and 3 ML models", "5G networks and 3 ML models"),
    ("Details: doi.org/10.1038/s41591-023-02565-4 and nature.com/articles/x.", "Details: and."),
    ("Читайте на сайт.рф/новости", "Читайте на"),
    ("Mice got 10 mg kg−1 ketamine", "Mice got ketamine"),
    ("Препарат вводили в дозе 0,5 мг/кг массы тела.", "Препарат вводили."),
    ("Mice were treated at 1 mg/kg body weight daily.", "Mice were treated daily."),
    ("LSD (100 μg/mL) in vitro", "LSD in vitro"),
    ("U.S./Canada, т.е./и т.п., e.g./i.e.", "U.S./Canada, т.е./и т.п., e.g./i.e."),
    ("  many   spaces\n\tand lines  ", "many spaces and lines"),
    ("", ""),
])
def test_clean_text(text, expected):
    assert clean_text(text) == expected


@pytest.mark.parametrize("text, expected", [
    # Telegram links bare domains, @names, e-mails and tg:// in a bot message as well
    ("Магазин shroom-shop.com закрыли.", "Магазин закрыли."),
    ("Читайте на example.com", "Читайте на"),
    ("Подробнее: MAPS.ORG/research.", "Подробнее."),
    ("Ссылка evil.com?ref=1 в описании", "Ссылка в описании"),
    ("Grow kits (growkit.store:443/buy) are illegal", "Grow kits are illegal"),
    ("Source: psypost.org", "Source"),
    ("Исследование описал psypost.org.", "Исследование описал."),
    ("Сайт shop.рф закрыли", "Сайт закрыли"),
    ("Сайт SHOP.РФ и магазин.онлайн закрыли", "Сайт и закрыли"),
    ("Канал microdosetoday.t.me удалили", "Канал удалили"),
    ("Пишите @microdosetoday", "Пишите"),
    ("Бот @dealer_bot продаёт грибы", "Бот продаёт грибы"),
    ("Канал (@microdosetoday) закрыли", "Канал закрыли"),
    ("Почта a@b.com для связи", "Почта для связи"),
    ("Откройте tg://resolve?domain=microdosetoday.", "Откройте."),
    ("Издание N+1 (nplus1.ru) пишет о ЛСД", "Издание N+1 пишет о ЛСД"),
    ("Знаете evil.com?", "Знаете?"),
    # Telegram links "@name" after any non-word character, and after what the cleaning removed
    ("Читайте @@microdosetoday", "Читайте @"),
    ("Канал закрыт.@microdosetoday и ...@dealer_bot", "Канал закрыт. и..."),
    ("Пишите foo.bar@dealer_bot или x.io@microdosetoday", "Пишите или"),
    ("Подробнее...psypost.org", "Подробнее..."),
    # what is left is never glued into a new domain
    ("Магазин shop (https://evil.example) .com", "Магазин shop .com"),
    # abbreviations, decimals, names and initials stay
    ("U.S./Canada, e.g. psilocybin, т.е. ЛСД, и т. д.", "U.S./Canada, e.g. psilocybin, т.е. ЛСД, и т. д."),
    ("Средний балл вырос с 3.5 до 4.1", "Средний балл вырос с 3.5 до 4.1"),
    ("5-MeO-DMT и N+1", "5-MeO-DMT и N+1"),
    ("A Ph.D. student at 8 p.m.", "A Ph.D. student at 8 p.m."),
    ("А. С. Пушкин и Л.Н. Толстой", "А. С. Пушкин и Л.Н. Толстой"),
    ("Встреча в г.Москва 19.07.2026", "Встреча в г.Москва 19.07.2026"),
    ("Цена — 5 лет, @ 8 утра", "Цена — 5 лет, @ 8 утра"),
    ("Альбом получил 2 Grammy, на 2026 Grammy у Зигги Марли — 8 Grammys",
     "Альбом получил 2 Grammy, на 2026 Grammy у Зигги Марли — 8 Grammys"),
    # dose units written out
    ("Добровольцы получали 100 микрограммов ЛСД.", "Добровольцы получали ЛСД."),
    ("Пациенты получили 25 миллиграммов псилоцибина.", "Пациенты получили псилоцибина."),
    ("Выпили 150 миллилитров отвара.", "Выпили отвара."),
    ("Participants received 25 milligrams of psilocybin.", "Participants received psilocybin."),
    ("They took 100 micrograms of LSD and 5 millilitres of tincture.", "They took LSD and tincture."),
    ("Probanden erhielten 25 Milligramm Psilocybin.", "Probanden erhielten Psilocybin."),
    ("Probanden erhielten 100 Mikrogramm LSD.", "Probanden erhielten LSD."),
    ("a 25-milligram dose", "a dose"),
])
def test_clean_text_removes_links_and_dose_words(text, expected):
    assert clean_text(text) == expected


def test_clean_text_is_cheap_on_long_runs():
    # Model text echoes untrusted posts: each of these took 10-40 s of CPU on the event loop.
    texts = ["-a." * 16000, "a-a." * 12000, ":" * 50000 + "x", " " * 50000 + "x", "@xx " * 12000, "1 mg " * 10000]
    start = time.perf_counter()
    for text in texts:
        clean_text(text)
    assert time.perf_counter() - start < 3


def test_clean_text_takes_only_strings():
    assert clean_text(None) == ""


def test_module_has_no_runtime_state():
    """Filters are pure: same answer twice, the item is not changed."""
    i = post("Spores for sale", "2 g", link="https://youtu.be/x")
    before = (i.title, i.summary, dict(i.meta))
    assert filters.drop_reason(i, LINKS_ONLY) == filters.drop_reason(i, LINKS_ONLY) == "offtopic"
    assert (i.title, i.summary, dict(i.meta)) == before


def test_the_source_language_plays_no_part_in_the_verdict():
    # lang plays no part in the filters: the same text drops the same way on an English broad feed
    for title in ("Псилоцибин ослабил симптомы анорексии", "Новый телескоп увидел галактику",
                  "В Москве задержан курьер с марихуаной"):
        assert drop_reason(item(title, lang="ru"), BROAD_RU) == drop_reason(item(title, lang="ru"), BROAD_EN)
