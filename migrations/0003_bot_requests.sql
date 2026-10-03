-- Stage 3: invocation, answers, per-request scoping of the Hermes read tools.

-- Per-chat bot settings changed by group admins, e.g. {"keyword_bot": false}.
ALTER TABLE chats ADD COLUMN settings JSONB NOT NULL DEFAULT '{}';

-- Telegram message ids of the bot's answer (long answers are split), so that a reply
-- to any part continues the same conversation (FR-15).
ALTER TABLE bot_requests ADD COLUMN thread_id BIGINT;
ALTER TABLE bot_requests ADD COLUMN answer_msg_ids BIGINT[];
ALTER TABLE bot_requests ADD COLUMN parent_id BIGINT REFERENCES bot_requests ON DELETE SET NULL;
CREATE INDEX bot_requests_answer_idx ON bot_requests USING GIN (answer_msg_ids);
CREATE INDEX bot_requests_chat_idx ON bot_requests (chat_id, created_at);

-- A ticket is issued for every LLM call and put into its prompt. The read tools only
-- accept a valid ticket and only return messages of the ticket's chat, so a prompt
-- injection in one group cannot read another group's archive.
CREATE TABLE request_tickets (
  ticket      TEXT PRIMARY KEY,
  chat_id     BIGINT NOT NULL,
  thread_id   BIGINT,
  expires_at  TIMESTAMPTZ NOT NULL
);

CREATE VIEW reader.request_tickets AS
SELECT ticket, chat_id, thread_id, expires_at
FROM request_tickets
WHERE expires_at > now();

-- Expose the predecessor of a migrated group so its history stays reachable.
CREATE OR REPLACE VIEW reader.chats AS
SELECT chat_id, title, type, is_forum, migrated_from_chat_id
FROM chats
WHERE migrated_to_chat_id IS NULL;

GRANT SELECT ON reader.request_tickets TO hermes_reader;
