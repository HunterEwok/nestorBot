-- Answers now quote the messages they refer to (nestor.bot.markup puts the author's words
-- into a link to the message). The authors quoted are kept with the request, so that
-- /forget_me also removes those answers from the archive and from the conversation history.
ALTER TABLE bot_requests ADD COLUMN quoted_user_ids BIGINT[];
CREATE INDEX bot_requests_quoted_idx ON bot_requests USING GIN (quoted_user_ids);

-- backfill: answers written before this column, tagged the way the bot tags them now.
-- (tests/test_migrations.py runs the statements from here on against a filled database)

-- The messages an answer refers to, as nestor.bot.markup reads them: [«quote»](#id), a bare #id
-- of three digits or more («проблема #1» is a count) and t.me/c links into the chat itself.
CREATE TEMP TABLE quoted_refs AS
SELECT b.id, b.chat_id, coalesce(r[1], r[2], r[3])::bigint AS message_id, r[3] IS NOT NULL AS linked
FROM bot_requests b
CROSS JOIN LATERAL regexp_matches(
    b.answer,
    '(?<![\w#&/])#([1-9]\d{2,11})(?!\w)|\]\(\s*#(\d{1,12})\s*\)|t\.me/c/' || (-b.chat_id - 1000000000000)
        || '/(?:\d+/)?(\d{1,12})',
    'g') AS r
WHERE b.answer IS NOT NULL;

-- 1. Their authors. A #id may also mean the basic group the chat was migrated from (both number
--    from 1): then both authors; a t.me link names the supergroup itself. The bot's own
--    messages come in step 3.
UPDATE bot_requests b SET quoted_user_ids = q.ids
FROM (SELECT x.id, array_agg(DISTINCT m.user_id ORDER BY m.user_id) AS ids
      FROM quoted_refs x
      JOIN chats c ON c.chat_id = x.chat_id
      JOIN messages m ON m.message_id = x.message_id
                     AND (m.chat_id = x.chat_id OR (NOT x.linked AND m.chat_id = c.migrated_from_chat_id))
      JOIN users u ON u.user_id = m.user_id AND NOT u.is_bot
      GROUP BY x.id) q
WHERE b.id = q.id;

-- 2. An answer about the message the request replied to (theses, who is right, a fact-check
--    verdict, a song from that message, any question the archive model answered with that
--    message in front of it) carries its author's words; a reminder confirmation repeats the
--    asker's. The web profile never sees the replied message. A reply to the topic's first
--    message is not a reply to anyone.
UPDATE bot_requests b
SET quoted_user_ids = (SELECT array_agg(DISTINCT v ORDER BY v)
                       FROM unnest(coalesce(b.quoted_user_ids, '{}'::bigint[]) || ARRAY[r.user_id]) AS v)
FROM messages t
JOIN messages r ON r.chat_id = t.chat_id AND r.message_id = t.reply_to_id AND r.kind <> 'service'
JOIN users u ON u.user_id = r.user_id AND NOT u.is_bot
WHERE t.chat_id = b.chat_id AND t.message_id = b.trigger_msg_id AND b.answer IS NOT NULL
  AND coalesce(b.scenario, '') NOT IN ('web', 'stats', 'reminder')
  AND NOT (t.is_topic_message AND t.reply_to_id = t.thread_id);

UPDATE bot_requests b
SET quoted_user_ids = (SELECT array_agg(DISTINCT v ORDER BY v)
                       FROM unnest(coalesce(b.quoted_user_ids, '{}'::bigint[]) || ARRAY[b.user_id]) AS v)
WHERE b.scenario = 'reminder' AND b.answer IS NOT NULL AND b.user_id IS NOT NULL;

-- 3. An answer that refers to the bot's own earlier answer quotes whoever that one quoted.
UPDATE bot_requests b
SET quoted_user_ids = (SELECT array_agg(DISTINCT v ORDER BY v)
                       FROM unnest(coalesce(b.quoted_user_ids, '{}'::bigint[]) || q.ids) AS v)
FROM (SELECT x.id, array_agg(DISTINCT v) AS ids
      FROM quoted_refs x
      JOIN bot_requests p ON p.chat_id = x.chat_id AND x.message_id = ANY(p.answer_msg_ids) AND p.id <> x.id
      CROSS JOIN LATERAL unnest(p.quoted_user_ids) AS v
      GROUP BY x.id) q
WHERE b.id = q.id;

-- 4. A follow-up in a conversation (a reply to the bot's answer) was written with the earlier
--    turns in front of the model and may repeat their quotes: it inherits them, six turns up.
WITH RECURSIVE up AS (
    SELECT id AS root, parent_id, 0 AS depth FROM bot_requests WHERE parent_id IS NOT NULL
    UNION ALL
    SELECT up.root, p.parent_id, up.depth + 1
    FROM up JOIN bot_requests p ON p.id = up.parent_id
    WHERE p.parent_id IS NOT NULL AND up.depth < 5)
UPDATE bot_requests b
SET quoted_user_ids = (SELECT array_agg(DISTINCT v ORDER BY v)
                       FROM unnest(coalesce(b.quoted_user_ids, '{}'::bigint[]) || q.ids) AS v)
FROM (SELECT up.root AS id, array_agg(DISTINCT v) AS ids
      FROM up
      JOIN bot_requests p ON p.id = up.parent_id
      CROSS JOIN LATERAL unnest(p.quoted_user_ids) AS v
      GROUP BY up.root) q
WHERE b.id = q.id;

-- 5. A reference to a message the archive no longer has: its author may have used /forget_me
--    before this column existed, and the words beside the reference are then theirs. Once
--    anybody has, such answers go where /forget_me sends them: out of the archive, out of the
--    quotes in replies to them, and out of the conversation history.
CREATE TEMP TABLE orphan_answers AS
SELECT DISTINCT x.id, x.chat_id, b.answer_msg_ids
FROM quoted_refs x
JOIN bot_requests b ON b.id = x.id
JOIN chats c ON c.chat_id = x.chat_id
WHERE EXISTS (SELECT 1 FROM users WHERE opted_out)
  AND NOT EXISTS (SELECT 1 FROM messages m
                  WHERE m.message_id = x.message_id
                    AND (m.chat_id = x.chat_id OR (NOT x.linked AND m.chat_id = c.migrated_from_chat_id)));
DELETE FROM messages m USING orphan_answers o, users u
WHERE m.chat_id = o.chat_id AND m.message_id = ANY(o.answer_msg_ids) AND u.user_id = m.user_id AND u.is_bot;
UPDATE messages m SET raw = raw - 'quote'
FROM orphan_answers o
WHERE m.chat_id = o.chat_id AND m.raw ? 'quote'
  AND m.raw->'reply_to_message'->>'message_id' = ANY(o.answer_msg_ids::text[]);
UPDATE bot_requests b SET answer = NULL FROM orphan_answers o WHERE b.id = o.id;

-- 6. Fired reminders and /reminders lists used to be archived. They repeat people's words and
--    no request ties them to a person, so they leave the archive (the bot no longer stores them).
DELETE FROM messages m USING users u
WHERE u.user_id = m.user_id AND u.is_bot
  AND (m.text LIKE '⏰ %, напоминаю%' OR m.text LIKE 'Твои напоминания в этой группе:%'
       OR m.text LIKE '🗓 Твои напоминания в этой группе%');

DROP TABLE orphan_answers;
DROP TABLE quoted_refs;
