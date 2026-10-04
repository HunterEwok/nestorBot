"""The loop for what the bot does by itself, inside the collector process: due reminders,
the morning digest and the wisdom of the day. Everything it sends goes through the same
Answerer: the per-chat queue, the daily token limit and the bot_requests log."""
from __future__ import annotations

import asyncio
import logging

from ..collector.app import _TRANSIENT

log = logging.getLogger("nestor.scheduler")


class Scheduler:
    def __init__(self, answerer, tick_s: float = 30):
        self.answerer = answerer
        self.tick_s = tick_s

    async def run(self) -> None:
        while True:
            for name, step in (("reminders", self.answerer.send_due_reminders),
                               ("daily_posts", self.answerer.run_daily_posts)):
                try:
                    await step()
                except _TRANSIENT as e:  # database down: the next tick tries again
                    log.warning("scheduler step postponed", extra={"step": name, "error": type(e).__name__})
                except Exception:  # noqa: BLE001 - a bug in one step must not stop the other
                    log.exception("scheduler step failed", extra={"step": name})
            await asyncio.sleep(self.tick_s)
