# nestorBot

Групповой Telegram-бот: молча архивирует сообщения разрешённых групп в PostgreSQL и (с этапа 3) отвечает по прямому обращению через Hermes Agent.

Состояние: этапы 1 (схема, миграции, роли), 2 (сборщик) и 3 (ответы через Hermes) из ТЗ v0.1, плюс импорт старой истории, интернет, напоминания, песни, дайджест, мудрость дня, вечерняя новостная сводка и выбор модели по задаче.

## Устройство

- `nestor/collector` — сборщик. Единственный владелец токена бота, long polling, без LLM.
  - `parse.py` — чистое преобразование JSON Bot API в строки базы.
  - `store.py` — идемпотентная запись: ключ `(chat_id, message_id)`, правки в `message_edits`.
  - `app.py` — aiogram-диспетчер, allowlist (FR-7), повтор записи при недоступной базе.
- `nestor/bot` — ответы по обращению (этап 3), работают внутри того же процесса сборщика.
  - `triggers.py` — обращение ли это к боту: @упоминание, реплай на бота, «нестор»/«бот» в начале, команды.
  - `context.py` — контекст для модели: ветка, сообщения упомянутого участника, последние сообщения или период; всегда только текущий чат.
  - `answerer.py` — лимиты, очередь «одна задача на чат», «👀», ответ реплаем с разбиением на 4096, журнал `bot_requests`, команды и все сценарии ниже.
  - `llm.py` — клиент OpenAI-совместимого API Hermes.
  - `routing.py` — какая задача на каком профиле Hermes (`MODEL_ROUTES`), «нестор, подумай» поднимает вопрос на более сильную модель.
  - `reminders.py` — разбор времени по-русски («в пятницу», «через 2 часа», «10.10 в 18:30») без модели и таблица `reminders`.
  - `music.py` — проверка ссылок на песню (YouTube, Spotify, Apple Music без ключей) и карточка с плеером и четырьмя кнопками.
  - `daily.py` — утренний дайджест и мудрость дня: фиксированный список тем, проверка цитаты и ссылки, таблица `daily_posts`.
  - `export.py` — `/export`: свои сообщения файлом в личку.
  - `scheduler.py` — фоновый цикл в том же процессе: напоминания и ежедневные посты.
- `nestor/news` — вечерняя новостная сводка из открытых источников (наука о каннабисе и психоделиках, законы и индустрия, сообщество, регги и даб). Свой фоновый цикл в процессе сборщика, переписку не читает.
  - `sources.py` — список источников и чёрный список каналов; `adapters/` — PubMed, Europe PMC, bioRxiv/medRxiv, ClinicalTrials.gov, RSS, Reddit через Arctic Shift, t.me/s, Reggaeville.
  - `filters.py` — торговля, дозировки, инструкции, криминал, тема; `store.py` — таблицы `news_*`, склейка одной истории из разных источников.
  - `triage.py` (дешёвая модель: строка по-русски на новость), `rank.py` (отбор по разделам), `digest.py` (сводка умной моделью, ссылки подставляет код).
  - `python -m nestor.news --check` — проверить источники с этой машины без базы и модели.
- `nestor/mcp` — MCP-сервер только для чтения (роль `hermes_reader`), его запускает Hermes. Каждый вызов требует ticket из промпта и видит только чат этого запроса.
- `nestor/importer` — разовый импорт старой истории через аккаунт владельца (Telethon).
- `deploy/hermes` — профили Hermes: `group` и `deep` (архив, без встроенных инструментов), `web` и `smart` (только интернет, без архива), `news` (без инструментов), SOUL.md, порядок проверки.
- `migrations/` — SQL-миграции, применяются `python -m nestor.migrate`.
- `nestor/bootstrap.py` — роли `nestor` (пишет) и `hermes_reader` (только представления `reader.*`) и сама база.
- `deploy/systemd/nestor-collector.service` — юнит с `Restart=always`.

### Гарантия «0 потерь»

Апдейты обрабатываются по одному (`handle_as_tasks=False`), и следующий `getUpdates`, который подтверждает их Telegram, уходит только после коммита в базу. Если база недоступна, запись повторяется бесконечно с нарастающей паузой; неподтверждённые апдейты Telegram хранит до 24 часов. Повторная доставка после падения ничего не дублирует.

### Что видит Hermes

Роль `hermes_reader` не имеет доступа к схеме `public`. Ей доступны только представления:

| Представление | Что внутри |
|---|---|
| `reader.messages` | сообщения без `raw`, с автором, готовой ссылкой `t.me/c/...`, ссылками из текста (`urls`) и именем файла (`file_name`); без пользователей, сделавших `/forget_me` |
| `reader.users` | участники, кроме opted_out |
| `reader.chats` | чаты |
| `reader.message_edits` | история правок видимых сообщений |

Сессии роли по умолчанию read-only. Полнотекстовый поиск: `tsv @@ plainto_tsquery('russian', ...)`.

## Установка

Сборщик работает на Raspberry Pi (система на microSD), PostgreSQL на отдельной машине с SSD. Pi ходит к базе через VPN (Tailscale или WireGuard, рекомендуется) или напрямую через интернет по TLS.

### Сервер базы

1. PostgreSQL 16, данные на SSD. Положить `deploy/postgres/nestor.conf` в `/etc/postgresql/16/main/conf.d/` и поправить адрес в `listen_addresses`.
2. Добавить строки из `deploy/postgres/pg_hba.example` в `pg_hba.conf` (адрес Pi), перезапустить PostgreSQL.
3. Создать роли и базу. Запускать можно откуда угодно, где есть этот репозиторий и доступ суперпользователем к серверу:
   ```sh
   PG_ADMIN_URL='postgresql://postgres:...@db-host/postgres' COLLECTOR_PASSWORD=... READER_PASSWORD=... \
       python -m nestor.bootstrap
   ```

### Raspberry Pi

1. В BotFather: Bot Settings → Group Privacy → Turn off. После этого удалить бота из группы и добавить заново.
2. ```sh
   sudo useradd --system --home /opt/nestor nestor
   sudo git clone https://github.com/HunterEwok/nestorBot /opt/nestor && sudo chown -R nestor: /opt/nestor
   sudo -u nestor python3 -m venv /opt/nestor/.venv      # 3.11 из Raspberry Pi OS подходит
   sudo -u nestor /opt/nestor/.venv/bin/pip install -e /opt/nestor
   sudo cp /opt/nestor/.env.example /opt/nestor/.env && sudo chmod 600 /opt/nestor/.env
   # в .env: BOT_TOKEN, ALLOWED_CHAT_IDS и DATABASE_URL на сервер базы, например
   #   postgresql://nestor:...@100.64.0.1/nestor                      (через VPN)
   #   postgresql://nestor:...@db.example.com/nestor?sslmode=verify-full  (через интернет)
   # строки PG_ADMIN_URL, *_PASSWORD и NESTOR_TEST_PG_ADMIN_URL на Pi не нужны
   sudo cp /opt/nestor/deploy/journald/nestor.conf /etc/systemd/journald.conf.d/ && sudo systemctl restart systemd-journald
   sudo cp /opt/nestor/deploy/systemd/nestor-collector.service /etc/systemd/system/
   sudo systemctl enable --now nestor-collector
   ```
   Миграции применяются при каждом старте юнита (`ExecStartPre`). Если база недоступна, юнит перезапускается каждые 10 секунд, пока она не появится.

На microSD сборщик ничего не пишет, кроме журнала (до 100 МБ, 14 дней). Пока база недоступна, сообщения ждут в Telegram (до 24 часов) и дозаписываются после восстановления связи.

`chat_id` группы: добавить бота с пустым `ALLOWED_CHAT_IDS`, он сразу выйдет и запишет `chat_id` в журнал (`journalctl -u nestor-collector`).

## Импорт старой истории

Bot API не отдаёт сообщения, написанные до добавления бота, поэтому история читается разово через твой аккаунт Telegram (Telethon) и запускается с ноутбука, не с Pi.

1. На https://my.telegram.org → API development tools получить `api_id` и `api_hash`, вписать в `.env` как `TG_API_ID` и `TG_API_HASH`. В `DATABASE_URL` указать базу на сервере (роль `nestor`, через туннель).
2. `pip install -e ".[import]"` (в dev-окружении уже стоит).
3. `python -m nestor.importer --list`: список твоих групп с `chat_id`, звёздочкой отмечены те, что уже в `ALLOWED_CHAT_IDS`. Первый запуск спросит телефон и код входа; сессия сохранится в `nestor-import.session`, это залогиненный аккаунт, файл никому не отдавать.
4. `python -m nestor.importer`: импорт всех групп из `ALLOWED_CHAT_IDS` (или `--chat <id>` для одной).

Импорт можно прерывать и запускать снова: уже сохранённое пропускается, а живые сообщения сборщика импорт не перезаписывает. Обычные (не супер-) группы не импортируются: у них номера сообщений у каждого аккаунта свои. Такую группу нужно сначала превратить в супергруппу (например, включить видимость истории для новых участников), тогда её старая часть импортируется под прежним `chat_id`.

## Локальная разработка

Нужны Python 3.11+ и Docker (две базы PostgreSQL 18 (как на роутере) из `docker-compose.yml`: `db` на 5432 для запуска, `db-test` на 5433 для тестов).

```sh
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env          # для тестов менять ничего не нужно
docker compose up -d
pytest                        # тестам нужен только db-test
python -m nestor.bootstrap    # один раз: роли и база в db
python -m nestor.migrate
python -m nestor.collector    # нужен настоящий BOT_TOKEN и ALLOWED_CHAT_IDS в .env
```

В PyCharm те же шаги лежат готовыми конфигурациями запуска в `.run/`: `1 bootstrap db`, `2 migrate`, `3 collector`, `4 import history`, `import: list groups`, `tests`.

Тесты пересоздают роли `nestor`, `hermes_reader` и базу `nestor_test`, поэтому `NESTOR_TEST_PG_ADMIN_URL` должен указывать на одноразовый кластер. Без этой переменной тесты базы пропускаются.

Покрыто приёмкой этапов 1–2: `hermes_reader` не может INSERT/UPDATE/DELETE/DDL и не видит таблицы; 1 000 сообщений через настоящий aiogram-диспетчер с повторной доставкой половины — 0 потерь и 0 дублей; правки, allowlist, переход группы в супергруппу.

Вручную на Pi остаётся проверить: перезапуск сборщика и час простоя — сообщения дозаписываются.
