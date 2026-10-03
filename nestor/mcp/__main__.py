"""stdio MCP server started by Hermes (profile `group`):

    mcp_servers:
      nestor:
        command: /opt/nestor/.venv/bin/python
        args: ["-m", "nestor.mcp"]
        env: {READER_DATABASE_URL: "postgresql://hermes_reader:...@db/nestor"}
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import asyncpg
from mcp.server.mcpserver import MCPServer

from . import tools

_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(_server):
    global _pool
    _pool = await asyncpg.create_pool(os.environ["READER_DATABASE_URL"], min_size=0, max_size=2,
                                      timeout=10, command_timeout=20)
    try:
        yield None
    finally:
        await _pool.close()


server = MCPServer(
    "nestor-archive",
    instructions="Read-only archive of the current Telegram group. Every tool needs the ticket "
                 "given in the system prompt; it only returns messages of that group.",
    lifespan=lifespan,
)


async def _call(fn, **kwargs) -> str:
    async with _pool.acquire() as conn:
        try:
            return await fn(conn, **kwargs)
        except tools.ToolError as e:
            return str(e)


@server.tool()
async def search_messages(ticket: str, query: str, days: int = 365, author: str | None = None, limit: int = 30) -> str:
    """Full-text search (Russian morphology) in this group's archive. `author` is @username or a name."""
    return await _call(tools.search_messages, ticket=ticket, query=query, days=days, author=author, limit=limit)


@server.tool()
async def get_thread(ticket: str, message_id: int, depth: int = 10) -> str:
    """A message with the chain it replies to and the replies below it."""
    return await _call(tools.get_thread, ticket=ticket, message_id=message_id, depth=depth)


@server.tool()
async def get_user_messages(ticket: str, user: str, days: int = 1, limit: int = 30) -> str:
    """Recent messages of one participant (@username or name) in this group."""
    return await _call(tools.get_user_messages, ticket=ticket, user=user, days=days, limit=limit)


@server.tool()
async def get_recent(ticket: str, limit: int = 50) -> str:
    """The latest messages of this group (of the current forum topic, if the request came from one)."""
    return await _call(tools.get_recent, ticket=ticket, limit=limit)


if __name__ == "__main__":
    server.run("stdio")
