-- Reminders, scheduled posts (digest, wisdom of the day), link and file search.

-- Scheduled posts have no triggering message and no asking user, but their tokens
-- still count toward the daily limit, so they are logged here as well.
ALTER TABLE bot_requests ALTER COLUMN trigger_msg_id DROP NOT NULL;
ALTER TABLE bot_requests ALTER COLUMN user_id DROP NOT NULL;
CREATE INDEX bot_requests_scenario_idx ON bot_requests (scenario, created_at);

-- «нестор, напомни в пятницу про сбор денег». The text is the asker's own words;
-- /forget_me deletes their reminders together with their messages.
CREATE TABLE reminders (
  id              BIGSERIAL PRIMARY KEY,
  chat_id         BIGINT NOT NULL,
  thread_id       BIGINT,                    -- forum topic of the request, if any
  user_id         BIGINT NOT NULL,
  request_msg_id  BIGINT NOT NULL,           -- the reminder is sent as a reply to it
  text            TEXT NOT NULL,
  due_at          TIMESTAMPTZ NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at         TIMESTAMPTZ,
  cancelled_at    TIMESTAMPTZ,
  attempts        INT NOT NULL DEFAULT 0
);
CREATE INDEX reminders_due_idx ON reminders (due_at) WHERE sent_at IS NULL AND cancelled_at IS NULL;
CREATE INDEX reminders_user_idx ON reminders (chat_id, user_id) WHERE sent_at IS NULL AND cancelled_at IS NULL;

-- One row per chat, kind and local day: the claim that makes a scheduled post happen
-- at most once, even across restarts.
CREATE TABLE daily_posts (
  id          BIGSERIAL PRIMARY KEY,
  chat_id     BIGINT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('digest', 'wisdom')),
  day         DATE NOT NULL,
  status      TEXT NOT NULL,                 -- running | posted | skipped | failed
  attempts    INT NOT NULL DEFAULT 1,
  message_id  BIGINT,
  themes      TEXT[],                        -- wisdom: themes from the fixed list, never chat text
  quote       TEXT,
  author      TEXT,
  source_url  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (chat_id, kind, day)
);

-- Links and file names for the find_links / find_files tools. Appended at the end of
-- the view, the earlier columns stay as they were. urls: targets of hidden links plus
-- http(s):// and www. addresses written in the text.
CREATE OR REPLACE VIEW reader.messages AS
SELECT m.chat_id,
       m.message_id,
       m.thread_id,
       m.is_topic_message,
       m.user_id,
       nullif(concat_ws(' ', u.first_name, u.last_name), '') AS author_name,
       u.username AS author_username,
       m.sender_chat_id,
       m.sent_at,
       m.reply_to_id,
       m.kind,
       m.event,
       m.text,
       m.edited_at,
       CASE WHEN m.chat_id <= -1000000000000 THEN
         'https://t.me/c/' || (-m.chat_id - 1000000000000)::text || '/'
         || CASE WHEN m.is_topic_message THEN m.thread_id::text || '/' ELSE '' END
         || m.message_id::text
       END AS link,
       m.tsv,
       ARRAY(
         SELECT DISTINCT l.url FROM (
           SELECT e->>'url' AS url
           FROM jsonb_array_elements(CASE WHEN jsonb_typeof(m.entities) = 'array' THEN m.entities
                                          ELSE '[]'::jsonb END) AS e
           WHERE e->>'type' = 'text_link'
           UNION ALL
           SELECT rtrim(parts[1], '.,;:!?)]}»"''')
           FROM regexp_matches(coalesce(m.text, ''), '((?:https?://|www\.)[^\s<>"«»]+)', 'gi') AS rx(parts)
         ) AS l
         WHERE l.url IS NOT NULL AND l.url <> ''
       ) AS urls,
       nullif(coalesce(
         m.raw->'document'->>'file_name',
         nullif(concat_ws(' — ', m.raw->'audio'->>'performer', m.raw->'audio'->>'title'), ''),
         m.raw->'audio'->>'file_name',
         m.raw->'video'->>'file_name',
         m.raw->'animation'->>'file_name'
       ), '') AS file_name
FROM messages m
LEFT JOIN users u ON u.user_id = m.user_id
WHERE u.user_id IS NULL OR NOT u.opted_out;

GRANT SELECT ON reader.messages TO hermes_reader;
