-- Answers now quote the messages they refer to (nestor.bot.markup puts the author's words
-- into a link to the message). The authors quoted are kept with the request, so that
-- /forget_me also removes those answers from the archive and from the conversation history.
ALTER TABLE bot_requests ADD COLUMN quoted_user_ids BIGINT[];
CREATE INDEX bot_requests_quoted_idx ON bot_requests USING GIN (quoted_user_ids);

-- Answers written before this column: the messages they referred to (#id or t.me/c links)
-- name the authors they quoted.
UPDATE bot_requests b SET quoted_user_ids = q.ids
FROM (SELECT b.id, array_agg(DISTINCT m.user_id) AS ids
      FROM bot_requests b
      JOIN chats c ON c.chat_id = b.chat_id
      CROSS JOIN LATERAL regexp_matches(b.answer, '(?:#|t\.me/c/\d+/(?:\d+/)?)(\d{1,12})', 'g') AS r
      JOIN messages m ON m.message_id = r[1]::bigint
                     AND (m.chat_id = b.chat_id OR m.chat_id = c.migrated_from_chat_id)
      WHERE b.answer IS NOT NULL AND m.user_id IS NOT NULL
      GROUP BY b.id) q
WHERE b.id = q.id;
