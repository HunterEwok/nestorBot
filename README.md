# nestorBot

Групповой Telegram-бот: молча архивирует сообщения разрешённых групп в PostgreSQL и (с этапа 3) отвечает по прямому обращению через Hermes Agent.

Состояние: этапы 1 (схема, миграции, роли) и 2 (сборщик) из ТЗ v0.1.

## Устройство

- `nestor/collector` — сборщик. Единственный владелец токена бота, long polling, без LLM.
  - `parse.py` — чистое преобразование JSON Bot API в строки базы.
  - `store.py` — идемпотентная запись: ключ `(chat_id, message_id)`, правки в `message_edits`.
  - `app.py` — aiogram-диспетчер, allowlist (FR-7), повтор записи при недоступной базе.
- `migrations/` — SQL-миграции, применяются `python -m nestor.migrate`.
- `nestor/bootstrap.py` — роли `nestor` (пишет) и `hermes_reader` (только представления `reader.*`) и сама база.
- `deploy/systemd/nestor-collector.service` — юнит с `Restart=always`.

### Гарантия «0 потерь»

Апдейты обрабатываются по одному (`handle_as_tasks=False`), и следующий `getUpdates`, который подтверждает их Telegram, уходит только после коммита в базу. Если база недоступна, запись повторяется бесконечно с нарастающей паузой; неподтверждённые апдейты Telegram хранит до 24 часов. Повторная доставка после падения ничего не дублирует.

### Что видит Hermes

Роль `hermes_reader` не имеет доступа к схеме `public`. Ей доступны только представления:

| Представление | Что внутри |
|---|---|
| `reader.messages` | сообщения без `raw`, с автором и готовой ссылкой `t.me/c/...`; без пользователей, сделавших `/forget_me` |
| `reader.users` | участники, кроме opted_out |
| `reader.chats` | чаты |
| `reader.message_edits` | история правок видимых сообщений |

Сессии роли по умолчанию read-only. Полнотекстовый поиск: `tsv @@ plainto_tsquery('russian', ...)`.

## Установка на Raspberry Pi

1. PostgreSQL 16 с данными на USB SSD; положить `deploy/postgres/nestor.conf` в `/etc/postgresql/16/main/conf.d/`.
2. В BotFather: Bot Settings → Group Privacy → Turn off. После этого удалить бота из группы и добавить заново.
3. ```sh
   sudo useradd --system --home /opt/nestor nestor
   sudo git clone https://github.com/HunterEwok/nestorBot /opt/nestor && sudo chown -R nestor: /opt/nestor
   sudo -u nestor python3 -m venv /opt/nestor/.venv      # 3.11 из Raspberry Pi OS подходит
   sudo -u nestor /opt/nestor/.venv/bin/pip install -e /opt/nestor
   sudo -u postgres env PG_ADMIN_URL='postgresql:///postgres?host=/var/run/postgresql' \
        COLLECTOR_PASSWORD=... READER_PASSWORD=... /opt/nestor/.venv/bin/python -m nestor.bootstrap
   cp .env.example /opt/nestor/.env && chmod 600 /opt/nestor/.env   # заполнить, PG_ADMIN_URL и пароли bootstrap оттуда убрать
   sudo cp deploy/systemd/nestor-collector.service /etc/systemd/system/
   sudo systemctl enable --now nestor-collector
   ```
   Миграции применяются при каждом старте юнита (`ExecStartPre`).

`chat_id` группы: добавить бота с пустым `ALLOWED_CHAT_IDS`, он сразу выйдет и запишет `chat_id` в журнал (`journalctl -u nestor-collector`).

## Локальная разработка

Нужны Python 3.11+ и Docker (две базы PostgreSQL 16 из `docker-compose.yml`: `db` на 5432 для запуска, `db-test` на 5433 для тестов).

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

В PyCharm те же шаги лежат готовыми конфигурациями запуска в `.run/`: `1 bootstrap db`, `2 migrate`, `3 collector`, `tests`.

Тесты пересоздают роли `nestor`, `hermes_reader` и базу `nestor_test`, поэтому `NESTOR_TEST_PG_ADMIN_URL` должен указывать на одноразовый кластер. Без этой переменной тесты базы пропускаются.

Покрыто приёмкой этапов 1–2: `hermes_reader` не может INSERT/UPDATE/DELETE/DDL и не видит таблицы; 1 000 сообщений через настоящий aiogram-диспетчер с повторной доставкой половины — 0 потерь и 0 дублей; правки, allowlist, переход группы в супергруппу.

Вручную на Pi остаётся проверить: перезапуск сборщика и час простоя — сообщения дозаписываются.
