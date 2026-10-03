-- Core archive schema. Owned by the collector role (nestor); see nestor/bootstrap.py.
-- All timestamps are UTC; display in Europe/Tallinn happens in the application.

-- Chats the bot has seen. Whether a chat may be read at all is decided by the
-- ALLOWED_CHAT_IDS allowlist (FR-7); `enabled` is the /pause switch.
CREATE TABLE chats (
  chat_id               BIGINT PRIMARY KEY,
  title                 TEXT,
  type                  TEXT NOT NULL,              -- group | supergroup
  is_forum              BOOLEAN NOT NULL DEFAULT FALSE,
  enabled               BOOLEAN NOT NULL DEFAULT TRUE,
  migrated_to_chat_id   BIGINT,                     -- set when a group became a supergroup
  migrated_from_chat_id BIGINT,
  added_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Group members as seen by the bot
CREATE TABLE users (
  user_id     BIGINT PRIMARY KEY,
  username    TEXT,
  first_name  TEXT,
  last_name   TEXT,
  is_bot      BOOLEAN NOT NULL DEFAULT FALSE,
  opted_out   BOOLEAN NOT NULL DEFAULT FALSE,  -- set by /forget_me
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per Telegram message (current version). Service events (joins,
-- title changes, pins, ...) are rows with kind = 'service' and event set.
CREATE TABLE messages (
  chat_id          BIGINT NOT NULL REFERENCES chats,
  message_id       BIGINT NOT NULL,
  thread_id        BIGINT,                    -- message_thread_id as sent by Telegram
  is_topic_message BOOLEAN NOT NULL DEFAULT FALSE,  -- thread_id is a forum topic
  user_id          BIGINT REFERENCES users,   -- from; NULL only for sender_chat posts we cannot attribute
  sender_chat_id   BIGINT,                    -- anonymous admin or linked channel
  sent_at          TIMESTAMPTZ NOT NULL,
  reply_to_id      BIGINT,
  kind             TEXT NOT NULL DEFAULT 'text',  -- text | photo | voice | document | ... | service
  event            TEXT,                      -- service event name, e.g. new_chat_members
  text             TEXT,                      -- message text or media caption
  entities         JSONB,                     -- links, mentions, formatting
  forward_origin   JSONB,
  media_group_id   TEXT,
  file_id          TEXT,
  file_unique_id   TEXT,
  edited_at        TIMESTAMPTZ,
  raw              JSONB NOT NULL,            -- Telegram Message object, reply_to_message reduced to ids
  stored_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  tsv              TSVECTOR GENERATED ALWAYS AS
                     (to_tsvector('russian'::regconfig, coalesce(text, ''))) STORED,
  PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX messages_tsv_idx    ON messages USING GIN (tsv);
CREATE INDEX messages_time_idx   ON messages (chat_id, sent_at);
CREATE INDEX messages_author_idx ON messages (chat_id, user_id, sent_at);
CREATE INDEX messages_reply_idx  ON messages (chat_id, reply_to_id);
CREATE INDEX messages_thread_idx ON messages (chat_id, thread_id, sent_at) WHERE thread_id IS NOT NULL;

-- Previous versions of edited messages
CREATE TABLE message_edits (
  id          BIGSERIAL PRIMARY KEY,
  chat_id     BIGINT NOT NULL,
  message_id  BIGINT NOT NULL,
  old_text    TEXT,
  old_edited_at TIMESTAMPTZ,                  -- edit time of the replaced version, NULL = original
  replaced_at TIMESTAMPTZ NOT NULL,           -- edit_date of the edit that replaced it
  FOREIGN KEY (chat_id, message_id) REFERENCES messages ON DELETE CASCADE
);
CREATE INDEX message_edits_msg_idx ON message_edits (chat_id, message_id);

-- Audit log of every bot invocation and LLM call (filled from stage 3 on)
CREATE TABLE bot_requests (
  id             BIGSERIAL PRIMARY KEY,
  chat_id        BIGINT NOT NULL,
  trigger_msg_id BIGINT NOT NULL,
  user_id        BIGINT NOT NULL,
  scenario       TEXT,                    -- theses | who_is_right | summary | search
  question       TEXT,
  answer         TEXT,
  model          TEXT,
  input_tokens   INT,
  output_tokens  INT,
  latency_ms     INT,
  status         TEXT NOT NULL,           -- ok | error | rate_limited | timeout
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX bot_requests_time_idx ON bot_requests (created_at);
CREATE INDEX bot_requests_user_idx ON bot_requests (user_id, created_at);
