-- 0006 tagged old answers with the authors they quoted, but carried quotes from one answer to
-- another a single link at a time. An answer that refers to an answer that refers to a third,
-- or a follow-up to a follow-up further than six turns up, inherits from all of them, as the
-- bot does now. Running it again changes nothing.

-- 1. The links: a reference to a message of the bot's own earlier answer, read as in 0006, and
--    a follow-up in a conversation. Then everything each answer leads to along them.
CREATE TEMP TABLE answer_edges AS
SELECT DISTINCT b.id AS src, p.id AS dst
FROM bot_requests b
CROSS JOIN LATERAL regexp_matches(
    b.answer,
    '(?<![\w#&/])#([1-9]\d{2,11})(?!\w)|\]\(\s*#(\d{1,12})\s*\)|t\.me/c/' || (-b.chat_id - 1000000000000)
        || '/(?:\d+/)?(\d{1,12})',
    'g') AS r
JOIN bot_requests p ON p.chat_id = b.chat_id AND p.answer_msg_ids @> ARRAY[coalesce(r[1], r[2], r[3])::bigint]
                   AND p.id <> b.id
WHERE b.answer IS NOT NULL
UNION
SELECT id, parent_id FROM bot_requests WHERE parent_id IS NOT NULL;

CREATE TEMP TABLE answer_reach AS
WITH RECURSIVE reach(src, dst) AS (       -- UNION drops pairs already seen: cycles end
    SELECT src, dst FROM answer_edges
    UNION
    SELECT r.src, e.dst FROM reach r JOIN answer_edges e ON e.src = r.dst)
SELECT src, dst FROM reach;

-- 2. The quotes carry over the whole chain.
UPDATE bot_requests b
SET quoted_user_ids = (SELECT array_agg(DISTINCT v ORDER BY v)
                       FROM unnest(coalesce(b.quoted_user_ids, '{}'::bigint[]) || q.ids) AS v)
FROM (SELECT r.src AS id, array_agg(DISTINCT v) AS ids
      FROM answer_reach r
      JOIN bot_requests p ON p.id = r.dst
      CROSS JOIN LATERAL unnest(p.quoted_user_ids) AS v
      GROUP BY r.src) q
WHERE b.id = q.id;

-- 3. Answers that now turn out to carry words /forget_me has already taken: an author who used
--    it is among their quotes (after 0006 tagged only the first link of their chain, or a
--    reminder of theirs), or their chain leads to an answer already removed for that (0006
--    removed some whose authors nobody knows any more). They go where /forget_me sends them:
--    out of the archive, out of the quotes in replies to them, and out of the conversation history.
CREATE TEMP TABLE forgotten_answers AS
SELECT b.id, b.chat_id, b.answer_msg_ids
FROM bot_requests b
WHERE EXISTS (SELECT 1 FROM users u WHERE u.opted_out AND u.user_id = ANY(b.quoted_user_ids))
   OR EXISTS (SELECT 1 FROM answer_reach r JOIN bot_requests p ON p.id = r.dst
              WHERE r.src = b.id AND p.answer IS NULL AND p.answer_msg_ids IS NOT NULL);
DELETE FROM messages m USING forgotten_answers f, users u
WHERE m.chat_id = f.chat_id AND m.message_id = ANY(f.answer_msg_ids) AND u.user_id = m.user_id AND u.is_bot;
UPDATE messages m SET raw = raw - 'quote'
FROM forgotten_answers f
WHERE m.chat_id = f.chat_id AND m.raw ? 'quote'
  AND m.raw->'reply_to_message'->>'message_id' = ANY(f.answer_msg_ids::text[]);
UPDATE bot_requests b SET answer = NULL FROM forgotten_answers f WHERE b.id = f.id AND b.answer IS NOT NULL;

DROP TABLE forgotten_answers;
DROP TABLE answer_reach;
DROP TABLE answer_edges;
