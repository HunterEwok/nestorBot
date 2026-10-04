-- Answers now quote the messages they refer to (nestor.bot.markup puts the author's words
-- into a link to the message). The authors quoted are kept with the request, so that
-- /forget_me also removes those answers from the archive and from the conversation history.
ALTER TABLE bot_requests ADD COLUMN quoted_user_ids BIGINT[];
CREATE INDEX bot_requests_quoted_idx ON bot_requests USING GIN (quoted_user_ids);
