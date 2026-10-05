-- Stage 4: retention (nestor.retention), backups (nestor.backup), monitoring (nestor.watchdog).
-- The schema part deletes nothing. The backfill only scrubs copies of words the archive should
-- not hold. Nothing here is granted to hermes_reader; nestor_backup gets SELECT through the
-- default privileges set by `python -m nestor.bootstrap backup`.

-- The purge walks messages by the time they entered the archive.
CREATE INDEX messages_stored_idx ON messages (stored_at);
-- By author alone: idle users, the FK check when a user row goes, /forget_me, /export.
CREATE INDEX messages_user_idx ON messages (user_id);
-- Deleting a request sets parent_id of its follow-ups to NULL (ON DELETE SET NULL).
CREATE INDEX bot_requests_parent_idx ON bot_requests (parent_id);

-- When the oldest message whose words a request may carry entered the archive: the messages
-- put in front of the model, those its answer links to, the replied message, and whatever the
-- earlier turns of its conversation carried. Its answer leaves when they do (nestor.retention).
ALTER TABLE bot_requests ADD COLUMN oldest_source_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX bot_requests_source_idx ON bot_requests (oldest_source_at) WHERE answer IS NOT NULL;

-- Runs of maintenance jobs, one row per job and local day: retention (the nightly purge),
-- retention_confirm (an operator allowed a large purge), restore_drill. Numbers and error
-- class names only, never text.
CREATE TABLE maintenance_runs (
  id          BIGSERIAL PRIMARY KEY,
  job         TEXT NOT NULL,
  day         DATE NOT NULL,                  -- local day (TIMEZONE)
  status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'blocked')),
  attempts    INT NOT NULL DEFAULT 1,
  cutoff      TIMESTAMPTZ,                    -- retention: what entered the archive before this went
  counts      JSONB NOT NULL DEFAULT '{}',
  error       TEXT,                           -- a short code or an exception class name
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  UNIQUE (job, day)
);

-- The span of message ids the purge removed, per chat: from the oldest to the newest. The importer
-- leaves it out, so history that already left the archive is not imported again with a fresh
-- stored_at; older history that never entered the archive can still be imported.
CREATE TABLE retention_marks (
  chat_id          BIGINT PRIMARY KEY,
  first_message_id BIGINT NOT NULL,
  message_id       BIGINT NOT NULL,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Health of the service (collector heartbeat, watchdog snapshot, collector_watch dedupe):
-- timestamps, counters and error codes only. Read by /status and nestor.watchdog.
CREATE TABLE ops_state (
  key         TEXT PRIMARY KEY,
  value       JSONB NOT NULL DEFAULT '{}',
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- backfill: (tests/test_migrations.py replays everything below against a filled database; a second run changes nothing)
UPDATE bot_requests SET oldest_source_at = created_at WHERE oldest_source_at > created_at;
-- What older requests were built from was not recorded: count them from the oldest message of
-- their chat and of the basic group it was migrated from, never later than their real sources.
UPDATE bot_requests b SET oldest_source_at = f.first_at
FROM (SELECT c.chat_id, min(m.stored_at) AS first_at
      FROM chats c
      JOIN messages m ON m.chat_id = c.chat_id OR m.chat_id = c.migrated_from_chat_id
      GROUP BY c.chat_id) f
WHERE b.chat_id = f.chat_id AND f.first_at < b.oldest_source_at;

-- Telegram's shared senders (nestor.forget.SHARED_IDS: anonymous admins, whoever posts as a channel,
-- forwards from a linked channel) are not one person: a /forget_me from one of them was no opt-out.
UPDATE users SET opted_out = FALSE WHERE opted_out AND user_id IN (1087968824, 136817688, 777000);

-- /forget_me keeps only the opt-out itself.
UPDATE users SET username = NULL, first_name = NULL, last_name = NULL
WHERE opted_out AND (username IS NOT NULL OR first_name IS NOT NULL OR last_name IS NOT NULL);

-- Cross-topic and cross-chat replies: keep only identifiers of the replied message; their quote
-- is not kept (nestor.collector.parse.reduce_raw does the same from now on).
UPDATE messages
SET raw = CASE WHEN raw ? 'reply_to_message' THEN raw ELSE raw - 'quote' END
          || jsonb_build_object('external_reply', jsonb_strip_nulls(jsonb_build_object(
               'message_id', raw->'external_reply'->'message_id',
               'chat', CASE WHEN jsonb_typeof(raw->'external_reply'->'chat') = 'object'
                            THEN jsonb_build_object('id', raw->'external_reply'->'chat'->'id') END,
               'origin', CASE WHEN jsonb_typeof(raw->'external_reply'->'origin') = 'object'
                              THEN jsonb_strip_nulls(jsonb_build_object(
                                'type', raw->'external_reply'->'origin'->'type',
                                'sender_user', CASE WHEN jsonb_typeof(raw->'external_reply'->'origin'->'sender_user') = 'object'
                                  THEN jsonb_build_object('id', raw->'external_reply'->'origin'->'sender_user'->'id') END,
                                'sender_chat', CASE WHEN jsonb_typeof(raw->'external_reply'->'origin'->'sender_chat') = 'object'
                                  THEN jsonb_build_object('id', raw->'external_reply'->'origin'->'sender_chat'->'id') END,
                                'chat', CASE WHEN jsonb_typeof(raw->'external_reply'->'origin'->'chat') = 'object'
                                  THEN jsonb_build_object('id', raw->'external_reply'->'origin'->'chat'->'id') END)) END)))
WHERE jsonb_typeof(raw->'external_reply') = 'object';

-- Any other quote without the replied message (Telegram leaves it out when that message was
-- deleted) cannot be attributed to anybody: not kept either, as in reduce_raw.
UPDATE messages SET raw = raw - 'quote' WHERE raw ? 'quote' AND NOT raw ? 'reply_to_message';

-- Quotes of opted-out authors inside replies that arrived after their /forget_me.
UPDATE messages r SET raw = r.raw - 'quote'
FROM users u
WHERE u.opted_out AND r.raw ? 'quote' AND r.raw->'reply_to_message'->'from'->>'id' = u.user_id::text;

-- Quotes in replies to forwards of opted-out authors: the forward is somebody else's row, the words are theirs.
UPDATE messages r SET raw = r.raw - 'quote'
FROM messages f, users u
WHERE u.opted_out AND r.raw ? 'quote' AND f.chat_id = r.chat_id AND f.message_id = r.reply_to_id
  AND coalesce(f.forward_origin->'sender_user'->>'id',
               CASE WHEN f.forward_origin->>'type' = 'user' THEN f.forward_origin->>'sender_id' END) = u.user_id::text;

-- A quote stays only beside a replied message the archive holds, which /forget_me and the purge find
-- it by. When that message is not in the archive (forgotten, purged, never stored) the quote goes.
UPDATE messages r SET raw = r.raw - 'quote'
WHERE r.raw ? 'quote'
  AND NOT EXISTS (SELECT 1 FROM messages t WHERE t.chat_id = r.chat_id AND t.message_id = r.reply_to_id);

-- Forwards of opted-out authors: keep the row (it is the forwarder's), drop the words and names.
-- Live updates name the author in sender_user, imported forwards by an id ({type, sender_id}).
UPDATE messages m
SET text = NULL, entities = NULL,
    forward_origin = jsonb_build_object('type', m.forward_origin->'type', 'sender_user', jsonb_build_object('id', u.user_id)),
    raw = (m.raw - 'text' - 'caption' - 'entities' - 'caption_entities'
                 - 'forward_from' - 'forward_sender_name' - 'forward_signature')
          || jsonb_build_object('forward_origin',
               jsonb_build_object('type', m.forward_origin->'type', 'sender_user', jsonb_build_object('id', u.user_id)))
FROM users u
WHERE u.opted_out
  AND coalesce(m.forward_origin->'sender_user'->>'id',
               CASE WHEN m.forward_origin->>'type' = 'user' THEN m.forward_origin->>'sender_id' END) = u.user_id::text;
