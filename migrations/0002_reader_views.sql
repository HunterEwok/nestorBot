-- Read-only surface for Hermes (role hermes_reader, created by nestor/bootstrap.py).
-- The role gets SELECT on these views only, never on the tables: no raw JSON,
-- no messages of users who opted out, no LLM audit log.
CREATE SCHEMA reader;

CREATE VIEW reader.chats AS
SELECT chat_id, title, type, is_forum
FROM chats
WHERE migrated_to_chat_id IS NULL;

CREATE VIEW reader.users AS
SELECT user_id, username, first_name, last_name, is_bot
FROM users
WHERE NOT opted_out;

CREATE VIEW reader.messages AS
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
       -- t.me/c links only exist for supergroups (chat_id = -100<id>)
       CASE WHEN m.chat_id <= -1000000000000 THEN
         'https://t.me/c/' || (-m.chat_id - 1000000000000)::text || '/'
         || CASE WHEN m.is_topic_message THEN m.thread_id::text || '/' ELSE '' END
         || m.message_id::text
       END AS link,
       m.tsv
FROM messages m
LEFT JOIN users u ON u.user_id = m.user_id
WHERE u.user_id IS NULL OR NOT u.opted_out;

CREATE VIEW reader.message_edits AS
SELECT e.chat_id, e.message_id, e.old_text, e.old_edited_at, e.replaced_at
FROM message_edits e
JOIN reader.messages m USING (chat_id, message_id);

GRANT USAGE ON SCHEMA reader TO hermes_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA reader TO hermes_reader;
