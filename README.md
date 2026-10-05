# nestorBot

Групповой Telegram-бот: молча архивирует сообщения разрешённых групп в PostgreSQL и (с этапа 3) отвечает по прямому обращению через Hermes Agent.

Состояние: этапы 1 (схема, миграции, роли), 2 (сборщик) и 3 (ответы через Hermes) из ТЗ v0.1, плюс импорт старой истории, интернет, напоминания, песни, дайджест, мудрость дня, вечерняя новостная сводка и выбор модели по задаче. Этап 4: архив хранит сообщения 12 месяцев, каждую ночь копия базы ложится на карту Pi, сторож пишет о сбоях в Telegram.

## Устройство

- `nestor/collector` — сборщик. Единственный владелец токена бота, long polling, без LLM.
  - `parse.py` — чистое преобразование JSON Bot API в строки базы.
  - `store.py` — идемпотентная запись: ключ `(chat_id, message_id)`, правки в `message_edits`.
  - `app.py` — aiogram-диспетчер, allowlist (FR-7), повтор записи при недоступной базе.
  - `telemetry.py` — пульс для сторожа: раз в минуту в `ops_state` пишется, когда последний раз удался `getUpdates` и сколько сообщений не ушло (только коды и счётчики). Если сам сторож молчит 20 минут, сборщик один раз говорит об этом в группе оповещений.
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
  - `python -m nestor.news --check` — проверить источники с этой машины без базы и модели; `--check -v <источник>` — каждая запись с причиной отсева, датой и главным из meta.
- `nestor/retention.py` — ночная чистка архива по сроку хранения; `nestor/forget.py` — `/forget_me`; `nestor/promises.py` — все обещанные сроки в одном месте, из них собирается текст `/privacy`.
- `nestor/backup.py` — ночная копия базы (`pg_dump`) на карту Pi и восстановление из неё.
- `nestor/watchdog` — сторож: раз в 5 минут проверяет сборщик, базу, Hermes, копии, чистку и карту Pi и пишет о проблемах в группу оповещений.
- `nestor/logs.py` — JSON-логи в journald: ошибки только по имени класса, токены, пароли и ключи вымарываются.
- `nestor/mcp` — MCP-сервер только для чтения (роль `hermes_reader`), его запускает Hermes. Каждый вызов требует ticket из промпта и видит только чат этого запроса.
- `nestor/importer` — разовый импорт старой истории через аккаунт владельца (Telethon).
- `deploy/hermes` — профили Hermes: `group` и `deep` (архив, без встроенных инструментов), `web` и `smart` (только интернет, без архива), `news` (без инструментов), SOUL.md, порядок проверки.
- `migrations/` — SQL-миграции, применяются `python -m nestor.migrate`.
- `nestor/bootstrap.py` — роли `nestor` (пишет), `hermes_reader` (только представления `reader.*`), `nestor_backup` (только чтение, для `pg_dump`) и сама база. Пароли уходят на сервер только SCRAM-хешем.
- `deploy/systemd` — юниты: `nestor-collector` (`Restart=always`), `nestor-backup` (таймер 04:30 и 13:30), `nestor-watchdog` (таймер раз в 5 минут).

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

Сборщик работает на Raspberry Pi (система на microSD), PostgreSQL на отдельной машине. Pi ходит к базе по локальной сети, через VPN (Tailscale или WireGuard) или через интернет по TLS.

У нас так:

| Где | Что |
|---|---|
| Raspberry Pi 4, Debian 13 (trixie, arm64), Python 3.13, только microSD | `nestor-collector` (архив, ответы, чистка, пульс), Hermes Agent, `nestor-backup` (копии на карту), `nestor-watchdog` |
| Роутер OpenWrt, PostgreSQL 18 в Docker, `192.168.0.1:5432` | база `nestor` |
| Ноутбук | `python -m nestor.bootstrap` (пароль суперпользователя базы есть только здесь), импорт истории |

По Москве: 03:30 чистка архива, 04:30 копия базы (13:30 — повтор, если ночью не вышло), 09:30 сводка сторожа за вчера; сторож проверяет всё раз в 5 минут.

### Сервер базы

1. PostgreSQL 16 или новее (у нас 18). Положить `deploy/postgres/nestor.conf` в `/etc/postgresql/16/main/conf.d/` и поправить адрес в `listen_addresses`.
2. Добавить строки из `deploy/postgres/pg_hba.example` в `pg_hba.conf` (адрес Pi), перезапустить PostgreSQL.
3. Создать роли и базу. Запускать можно откуда угодно, где есть этот репозиторий и доступ суперпользователем к серверу. Пароли вводятся без эха, по одному на каждое имя (`PG_ADMIN_URL` — `postgresql://postgres:...@db-host/postgres`), и не остаются ни в истории оболочки, ни на экране:
   ```sh
   for v in PG_ADMIN_URL COLLECTOR_PASSWORD READER_PASSWORD; do printf '%s: ' "$v"; read -rs "$v"; echo; export "$v"; done
   python -m nestor.bootstrap; unset PG_ADMIN_URL COLLECTOR_PASSWORD READER_PASSWORD
   ```

### Raspberry Pi

1. В BotFather: Bot Settings → Group Privacy → Turn off. После этого удалить бота из группы и добавить заново.
2. ```sh
   sudo useradd --system --home /opt/nestor nestor
   sudo git clone https://github.com/HunterEwok/nestorBot /opt/nestor && sudo chown -R nestor: /opt/nestor
   sudo -u nestor python3 -m venv /opt/nestor/.venv      # Python 3.11 или новее (в Debian 13 — 3.13)
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

На microSD сборщик ничего не пишет, кроме журнала (до 100 МБ, 14 дней; в нём нет текстов сообщений). Пока база недоступна, сообщения ждут в Telegram (до 24 часов) и дозаписываются после восстановления связи. Копии базы и состояние сторожа тоже на карте, см. ниже.

`chat_id` группы: добавить бота с пустым `ALLOWED_CHAT_IDS`, он сразу выйдет и запишет `chat_id` в журнал (`journalctl -u nestor-collector`).

## Срок хранения

Сообщение хранится 12 месяцев с того дня, как попало в архив (`messages.stored_at`, по часам базы), а не с его даты в Telegram: импортированная история считается со дня импорта. Каждую ночь в `RETENTION_TIME` (03:30 по `TIMEZONE`) сборщик удаляет то, что старше, вместе с правками, цитатами из этих сообщений в чужих ответах и ответами бота, построенными на них; записи о запросах, напоминания и имена уходят по тем же 12 месяцам (подробно — в `/privacy`). Чистка работает и без Hermes. Итог каждой ночи — строка в `maintenance_runs` (только числа и коды), её читает сторож.

Удаление не отменить, поэтому чистка не запускается, если часы Pi и базы расходятся больше чем на 10 минут, в архиве есть сообщения с датой из будущего или срок вышел короче 360 дней. Если за ночь ушло бы больше 5000 сообщений и больше 15 % архива (так будет около 2027-10-03, когда истечёт вся импортированная история), чистка ждёт подтверждения, а сторож об этом пишет:

```sh
sudo -u nestor /opt/nestor/.venv/bin/python -m nestor.retention                  # что удалилось бы сейчас; ничего не удаляет
sudo -u nestor /opt/nestor/.venv/bin/python -m nestor.retention --at 2027-10-05  # ... в этот день
sudo -u nestor /opt/nestor/.venv/bin/python -m nestor.retention --run            # прогнать сейчас (раз в день)
sudo -u nestor /opt/nestor/.venv/bin/python -m nestor.retention --confirm        # разрешить всё, что уйдёт за 7 дней
```

`/forget_me` удаляет сразу (`nestor/forget.py`), из копий — в течение 5 недель. Сроки живут в `nestor/promises.py`, текст `/privacy` собирается из них.

## Резервные копии

Юнит `nestor-backup` каждую ночь в 04:30 (после чистки) снимает `pg_dump` базы с роутера на карту Pi; в 13:30 — ещё раз, только если ночью не вышло. Он работает от своего пользователя `nestor-backup` с ролью базы `nestor_backup`, которая умеет только читать; её пароль лежит в `/etc/nestor-backup/pgpass`, не в `.env`. Копия сначала пишется во временный файл, целиком читается обратно `pg_restore` и только потом получает имя `nestor-*.dump`. Хранятся 7 ежедневных и 4 еженедельных копии, не старше 35 дней; две последние удачные не удаляются, даже если новые не получаются.

В `/var/lib/nestor-backup`: `dumps/` (0700, больше никто не читает), `status.json` (его читает сторож), `last-error.txt` (хвост ошибки `pg_dump`; в журнал и в оповещения она не попадает).

Роль `nestor_backup` создаётся с ноутбука на роутере. Адрес суперпользователя вводится явно: без него `bootstrap` взял бы `PG_ADMIN_URL` из `.env` для разработки и создал роль в локальной базе (в журнале `backup role ready` с `"server"` — куда она ушла). `PG_ADMIN_URL` — `postgresql://postgres:...@192.168.0.1/postgres`, `BACKUP_PASSWORD` — длинный случайный (`openssl rand -hex 32`), тот же пойдёт в `pgpass` на Pi:

```sh
for v in PG_ADMIN_URL BACKUP_PASSWORD; do printf '%s: ' "$v"; read -rs "$v"; echo; export "$v"; done
python -m nestor.bootstrap backup; unset PG_ADMIN_URL BACKUP_PASSWORD
```

Установка на Pi. `pg_dump` нужен 18 из репозитория PGDG: свой клиент Debian 13 (17) сервер 18 не берёт. Сторож тоже нужен, даже без оповещений (раздел «Оповещения»): он хранит `optouts.json`, без которого восстановление из копии не сможет снова забыть тех, кто воспользовался `/forget_me`.

```sh
sudo apt install -y postgresql-common      # только скрипт подключения репозитория PGDG, сервер не ставится
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y && sudo apt install -y postgresql-client-18
sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin nestor-backup
sudo install -d -o root -g nestor-backup -m 0750 /etc/nestor-backup
sudo install -o root -g nestor-backup -m 0640 /opt/nestor/deploy/systemd/nestor-backup.env.example /etc/nestor-backup/backup.env
read -rs P    # вставить пароль nestor_backup, Enter; в истории и в аргументах команд его не будет
printf '192.168.0.1:5432:nestor:nestor_backup:%s\n' "$P" | sudo install -o nestor-backup -g nestor-backup -m 600 /dev/stdin /etc/nestor-backup/pgpass; unset P
sudo cp /opt/nestor/deploy/systemd/nestor-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nestor-backup.timer
sudo systemctl start nestor-backup; cat /var/lib/nestor-backup/status.json      # "ok": true; иначе "error" — код причины
```

Восстановление, если база потеряна. Копия возвращает и то, что после неё забыли через `/forget_me`, поэтому `restore` после загрузки применяет миграции, ещё раз забывает всех из `optouts.json` сторожа и удаляет то, что пережило срок хранения. База должна быть пустой. Порядок важен: пока кто-то держит соединение, базу не переименовать, а сборщик успел бы создать таблицы в новой.

1. На Pi остановить всё, что ходит в базу:
   ```sh
   sudo systemctl disable --now nestor-collector nestor-backup.timer nestor-watchdog.timer
   systemctl --user stop hermes-gateway        # его MCP-сервер тоже держит соединение с базой
   ```
2. Если старая база ещё есть: на сервере базы суперпользователем, подключившись к базе `postgres` (не к `nestor`), закрыть её соединения (DataGrip, psql с ноутбука) и переименовать её:
   ```sql
   SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'nestor' AND pid <> pg_backend_pid();
   ALTER DATABASE nestor RENAME TO nestor_broken;
   ```
   Если `ALTER` выдал ошибку, дальше не идти: шаг 3 оставит старую базу как есть.
3. С ноутбука создать новую пустую `nestor`. `bootstrap` заново задаёт пароли ролям, поэтому нужны те, что уже стоят на Pi и в Hermes, а не пароли из `.env` для разработки. Они вводятся без эха, по одному на каждое имя, и не остаются в истории оболочки:
   ```sh
   for v in PG_ADMIN_URL COLLECTOR_PASSWORD READER_PASSWORD BACKUP_PASSWORD; do printf '%s: ' "$v"; read -rs "$v"; echo; export "$v"; done
   python -m nestor.bootstrap; unset PG_ADMIN_URL COLLECTOR_PASSWORD READER_PASSWORD BACKUP_PASSWORD
   ```
   `PG_ADMIN_URL` — `postgresql://postgres:...@192.168.0.1/postgres`, `COLLECTOR_PASSWORD` — пароль из `DATABASE_URL` в `/opt/nestor/.env` на Pi, `READER_PASSWORD` — из `READER_DATABASE_URL` в `config.yaml` Hermes, `BACKUP_PASSWORD` — из `/etc/nestor-backup/pgpass`. Без `BACKUP_PASSWORD` у `nestor_backup` не будет доступа к новой базе (а на новом сервере не будет и самой роли), и копии перестанут сниматься. Сверить пароль `nestor`, не показывая его: первые 8 знаков должны совпасть — на Pi `sudo sed -nE 's|^DATABASE_URL=[^:]*://[^:]*:([^@]*)@.*|\1|p' /opt/nestor/.env | tr -d '\n' | sha256sum | cut -c1-8`, на ноутбуке `read -rs P; printf %s "$P" | sha256sum | cut -c1-8; unset P` (на Mac `shasum -a 256`).
4. На Pi:
   ```sh
   F=$(sudo ls -1 /var/lib/nestor-backup/dumps | tail -n 1)
   sudo cat "/var/lib/nestor-backup/dumps/$F" | sudo -u nestor /opt/nestor/.venv/bin/python -m nestor.backup restore \
       --database nestor --optouts /var/lib/nestor-watchdog/optouts.json                  # restore done
   ```
   - `restore failed not_empty`: в базе `nestor` уже что-то есть. Сначала на сервере `SELECT datname FROM pg_database`: если `nestor_broken` в списке нет, а старая база не потеряна, то это она — переименование на шаге 2 не прошло; ничего не удалять, вернуться к шагу 2. Если `nestor_broken` есть (или сервер новый), в новую базу успели записать (обычно сборщик, если его не остановили): `DROP DATABASE nestor WITH (FORCE)`, и снова шаги 3 и 4.
   - `restore failed role_missing`: на сервере нет роли, которой копия выдаёт права (обычно `nestor_backup`: шаг 3 был без `BACKUP_PASSWORD`). Шаг 3 с ним, и снова шаг 4; восстановление идёт одной транзакцией, база осталась пустой.
   - `restore failed optouts_unreadable`: `optouts.json` сторожа нет или он повреждён (сторож не был установлен). Без `--optouts` не запускать: вернутся все, кто забыт после копии. Если `nestor_broken` ещё читается, собрать список из неё и повторить шаг 4 (база осталась пустой):
     ```sh
     sudo install -d -o nestor -g nestor -m 0700 /var/lib/nestor-watchdog
     psql -h 192.168.0.1 -U postgres -d nestor_broken -Atc "SELECT coalesce(json_agg(user_id ORDER BY user_id), '[]') FROM users WHERE opted_out" \
         | sudo install -o nestor -g nestor -m 0600 /dev/stdin /var/lib/nestor-watchdog/optouts.json
     ```
5. Запустить всё обратно:
   ```sh
   sudo systemctl enable --now nestor-collector nestor-backup.timer nestor-watchdog.timer
   systemctl --user start hermes-gateway
   ```
6. Импорт истории для всех групп с ноутбука: сообщения, пришедшие после копии, Telegram второй раз не пришлёт. Сообщения самого бота импорт не берёт: его ответы хранятся в `bot_requests`, а те, что забыты или вышли по сроку, не возвращаются.
7. Через неделю нормальной работы удалить старую базу: `DROP DATABASE nestor_broken WITH (FORCE)`. Пока она есть, в ней лежит весь архив без срока хранения и без `/forget_me`: обещания `/privacy` до этого не выполнены.

## Оповещения

Сторож (`python -m nestor.watchdog`, таймер `nestor-watchdog` раз в 5 минут, пользователь `nestor`) — единственный, кто пишет о сбоях. Он проверяет базу и часы, миграции, жив ли сборщик и получает ли он сообщения из Telegram (по его пульсу), Hermes, ошибки модели, копии, чистку, место и запись на карте, опоздавшие напоминания и источники новостей. О проблеме пишет один раз, когда она подтвердилась, и ещё раз — тихо, когда прошла; не больше одного сообщения за проверку и шести в час. С 23:00 до 08:00 сообщения приходят без звука; критичная проблема, которая длится больше часа или ещё открыта в 08:00, один раз звонит. В 09:30 — тихая сводка за вчера; если в группе оповещений больше `ALERT_MAX_MEMBERS` участников (с ботом), вместо сводки только предупреждение о посторонних. Его состояние — в `/var/lib/nestor-watchdog`, поэтому он помнит, что уже сказал, и когда база недоступна.

В `.env`: `ALERT_CHAT_ID` — группа оповещений (она должна быть в `ALLOWED_CHAT_IDS`, иначе бот из неё выйдет; пусто — оповещений нет, но таймер сторожа всё равно нужен: он хранит `optouts.json` для восстановления), `ALERT_DAILY_AT`, `ALERT_QUIET`, `ALERT_DISK_MIN_GB`, `ALERT_MAX_MEMBERS` (у них есть значения по умолчанию) и `BACKUP_STATUS_FILE=/var/lib/nestor-backup/status.json`. Эту строку в уже существующий `/opt/nestor/.env` надо дописать: без неё сторож копии не проверяет (в `/status` — «Не удалось проверить: Резервные копии», в сводке — «Копии не отслеживаются»). После правки `.env` перезапустить сборщик.

```sh
sudo cp /opt/nestor/deploy/systemd/nestor-watchdog.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nestor-watchdog.timer
sudo -u nestor /opt/nestor/.venv/bin/python -m nestor.watchdog --test      # пробное сообщение в группу
sudo -u nestor /opt/nestor/.venv/bin/python -m nestor.watchdog --dry-run   # что он видит; ничего не шлёт
sudo -u nestor /opt/nestor/.venv/bin/python -m nestor.watchdog --mute 60   # на час работ: новые проблемы ждут, без звука
```

`/status` в группе оповещений показывает её админам то, что сторож увидел в последний раз; в других группах бот на неё не отвечает, в `/help` её нет. Если сторож сам перестал запускаться, через 20 минут об этом один раз напишет сборщик.

Ни в оповещения, ни в журнал, ни в состояние сторожа не попадают тексты сообщений, токены, ключи и пароли: только коды, числа и имена групп.

## Импорт старой истории

Bot API не отдаёт сообщения, написанные до добавления бота, поэтому история читается разово через твой аккаунт Telegram (Telethon) и запускается с ноутбука, не с Pi.

1. На https://my.telegram.org → API development tools получить `api_id` и `api_hash`, вписать в `.env` как `TG_API_ID` и `TG_API_HASH`. В `DATABASE_URL` указать базу на сервере (роль `nestor`, через туннель), в `BOT_TOKEN` — настоящий токен бота: по нему импорт узнаёт сообщения бота и пропускает их.
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
