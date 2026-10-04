-- 0006 tagged old answers with the authors they quoted, but carried quotes from one answer to
-- another a single link at a time. An answer that refers to an answer that refers to a third,
-- or a follow-up to a follow-up further than six turns up, inherits from all of them, as the
-- bot does now. Running it again changes nothing.

-- The links: a reference to a message of the bot's own earlier answer, read as in 0006, and
-- a follow-up in a conversation.
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

WITH RECURSIVE reach(src, dst) AS (       -- UNION drops pairs already seen: cycles end
    SELECT src, dst FROM answer_edges
    UNION
    SELECT r.src, e.dst FROM reach r JOIN answer_edges e ON e.src = r.dst)
UPDATE bot_requests b
SET quoted_user_ids = (SELECT array_agg(DISTINCT v ORDER BY v)
                       FROM unnest(coalesce(b.quoted_user_ids, '{}'::bigint[]) || q.ids) AS v)
FROM (SELECT r.src AS id, array_agg(DISTINCT v) AS ids
      FROM reach r
      JOIN bot_requests p ON p.id = r.dst
      CROSS JOIN LATERAL unnest(p.quoted_user_ids) AS v
      GROUP BY r.src) q
WHERE b.id = q.id;

DROP TABLE answer_edges;
