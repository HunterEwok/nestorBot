-- The viral song of the day (nestor/trends/viral.py): which song was posted on which day, so the
-- same song is not the song of the day again for a month and the same artist not for a few days.
CREATE TABLE music_picks (
  day           DATE PRIMARY KEY,
  song_key      TEXT NOT NULL,              -- sources.song_key: main artist | title without brackets
  artist_key    TEXT NOT NULL,
  artist        TEXT NOT NULL,
  title         TEXT NOT NULL,
  score         REAL NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Historical editions of the charts copied from the Wayback Machine once, on the first start
-- (nestor/trends/backfill.py). Such an edition never replaces one the bot read itself.
ALTER TABLE music_snapshots ADD COLUMN origin TEXT NOT NULL DEFAULT 'read';   -- read | wayback
