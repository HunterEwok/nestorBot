-- The evening news digest from public sources (nestor/news).

-- One row per source: when it ran, its adapter state and its health.
CREATE TABLE news_sources (
  key             TEXT PRIMARY KEY,
  last_run_at     TIMESTAMPTZ,               -- last attempt
  last_ok_at      TIMESTAMPTZ,               -- last successful fetch
  next_run_at     TIMESTAMPTZ,               -- after an error or a Retry-After
  newest_item_at  TIMESTAMPTZ,               -- staleness: the newest item the source ever returned
  cursor          JSONB NOT NULL DEFAULT '{}',
  seeded          BOOLEAN NOT NULL DEFAULT FALSE,   -- the first fetch only marks what is already there as seen
  status          TEXT,                      -- ok | skipped | http_403 | timeout | error: ...
  failures        INT NOT NULL DEFAULT 0     -- consecutive
);

-- Everything fetched that passed the topic filter, plus what the trade and dosing filters
-- dropped (for tuning them). After 30 days only the identifiers are kept so that old feed
-- entries are not taken for new ones; after 400 days the row goes.
CREATE TABLE news_items (
  id            BIGSERIAL PRIMARY KEY,
  source        TEXT NOT NULL,
  ext_id        TEXT NOT NULL,              -- guid, PMID, NCT id + update date, post id
  url           TEXT,
  title         TEXT,
  summary       TEXT,
  doi           TEXT,
  title_key     TEXT,                       -- normalized title: one story from several sources
  lang          TEXT,
  categories    TEXT[],
  meta          JSONB NOT NULL DEFAULT '{}',
  published_at  TIMESTAMPTZ,
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  story_id      BIGINT,                     -- id of the first item of the same story
  -- seen: there before the first fetch or too old | dropped: filtered (drop_reason) |
  -- new: waits for triage | kept / rejected: triage verdict | used: in a digest
  status        TEXT NOT NULL,
  drop_reason   TEXT,
  section       TEXT,
  gist          TEXT,                       -- one line in Russian by the triage model
  importance    SMALLINT,
  triage_attempts INT NOT NULL DEFAULT 0,
  digest_day    DATE,
  UNIQUE (source, ext_id)
);
CREATE INDEX news_items_status_idx ON news_items (status, fetched_at);
CREATE INDEX news_items_doi_idx ON news_items (doi) WHERE doi IS NOT NULL;
CREATE INDEX news_items_title_idx ON news_items (title_key, fetched_at);
CREATE INDEX news_items_url_idx ON news_items (url, fetched_at);
CREATE INDEX news_items_story_idx ON news_items (story_id);

-- The digest is written once a day and posted to every group that has it on.
CREATE TABLE news_digests (
  day         DATE PRIMARY KEY,
  text        TEXT NOT NULL,               -- HTML for Telegram
  item_ids    BIGINT[] NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE daily_posts DROP CONSTRAINT daily_posts_kind_check;
ALTER TABLE daily_posts ADD CONSTRAINT daily_posts_kind_check CHECK (kind IN ('digest', 'wisdom', 'news'));

-- Triage and the digest itself belong to no group, but their tokens count toward the daily limit.
ALTER TABLE bot_requests ALTER COLUMN chat_id DROP NOT NULL;
-- Which Hermes profile answered (group, deep, web, smart, news): the cost per task.
ALTER TABLE bot_requests ADD COLUMN profile TEXT;
