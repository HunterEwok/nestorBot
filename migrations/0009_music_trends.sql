-- Rising Russian musicians (nestor/trends): daily snapshots of public charts, the artists seen
-- in them, and the weekly «Послушать» post. Public chart data only, nothing from the chats.

-- One row per chart: when it was read and its health.
CREATE TABLE music_sources (
  key           TEXT PRIMARY KEY,
  last_run_at   TIMESTAMPTZ,
  last_ok_day   DATE,                       -- local day of the last snapshot
  next_run_at   TIMESTAMPTZ,                -- after an error
  status        TEXT,                       -- ok | http_403 | timeout | error: ...
  size          INT,                        -- entries in the last snapshot
  failures      INT NOT NULL DEFAULT 0      -- consecutive
);

-- A chart as it was on a day. shift: places gained since the chart's own previous edition, when the
-- chart says so (positive: up); is_new: the chart marks it as a new entry. Both serve the first run,
-- before there is a history of our own.
CREATE TABLE music_snapshots (
  day           DATE NOT NULL,
  source        TEXT NOT NULL,
  position      INT NOT NULL,
  artist        TEXT NOT NULL,
  artist_key    TEXT NOT NULL,              -- normalized main artist
  title         TEXT NOT NULL,
  shift         INT,
  is_new        BOOLEAN NOT NULL DEFAULT FALSE,
  url           TEXT,
  PRIMARY KEY (day, source, position)
);
CREATE INDEX music_snapshots_artist_idx ON music_snapshots (artist_key, day);

-- What is known about an artist: their audience where a source tells it, whether they are already
-- well known (/music known, or the check model), and when they were recommended.
CREATE TABLE music_artists (
  artist_key      TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  listeners       BIGINT,
  listeners_at    TIMESTAMPTZ,
  known           BOOLEAN NOT NULL DEFAULT FALSE,
  known_reason    TEXT,                     -- command | model | listeners
  checked_at      TIMESTAMPTZ,              -- the check model's last verdict
  russian         BOOLEAN,                  -- the check model: performs in Russian / from the Russian scene
  recommended_at  TIMESTAMPTZ
);

ALTER TABLE daily_posts DROP CONSTRAINT daily_posts_kind_check;
ALTER TABLE daily_posts ADD CONSTRAINT daily_posts_kind_check CHECK (kind IN ('digest', 'wisdom', 'news', 'music'));
