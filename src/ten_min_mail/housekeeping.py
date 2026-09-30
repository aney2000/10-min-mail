"""Periodic removal of expired mailboxes.

Why this exists
---------------
Until now expired mailboxes were only deleted when something happened
to call `/health`. That made a health check load-bearing, which is
backwards. Without a sweep the database grows without bound, addresses
can never be recycled, and -- for a service whose entire promise is
that mail disappears -- message content outlives the mailbox that held
it. The last point makes this a privacy feature, not just tidiness.

Why a plain loop rather than a scheduler
----------------------------------------
APScheduler or Celery are the right answers for cron expressions,
persistence across restarts, or distributed workers. For "call one
function every sixty seconds" they would add a dependency, a
configuration surface, and in Celery's case a message broker, to
replace five lines. asyncio already provides the scheduling.

Design notes
------------
`sleep` is injected for the same reason the clock is: a test that
genuinely waited a minute between sweeps would be useless. Production
gets `asyncio.sleep`; tests get a fake that returns immediately.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

logger = logging.getLogger(__name__)

#: How often to sweep, by default.
#:
#: Mailboxes live ten minutes, so a minute of lag before a dead one is
#: removed is immaterial -- the service layer already refuses expired
#: mailboxes regardless of whether their row still exists, so the sweep
#: is about reclaiming storage, not about correctness.
DEFAULT_SWEEP_INTERVAL_SECONDS = 60.0

#: Anything that can be awaited to pause. Matches `asyncio.sleep`.
SleepFunction = Callable[[float], Awaitable[None]]


class Purgeable(Protocol):
    """The only capability the sweeper needs from the service.

    Narrow by design (Interface Segregation): depending on the whole
    MailboxService would mean a test double had to implement create,
    extend, deliver and the rest just to exercise a loop.
    """

    def purge_expired(self) -> int: ...  # pragma: no cover


class ExpirySweeper:
    """Deletes expired mailboxes on a fixed interval until cancelled."""

    def __init__(
        self,
        service: Purgeable,
        *,
        interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
        sleep: SleepFunction | None = None,
    ) -> None:
        if interval_seconds <= 0:
            # A zero or negative interval is a busy loop that pins a CPU
            # core and floods the database. Fail at construction rather
            # than at three in the morning.
            raise ValueError("interval_seconds must be positive")

        self._service = service
        self._interval = interval_seconds
        self._sleep: SleepFunction = sleep if sleep is not None else asyncio.sleep

        # Observability: cheap counters beat guessing whether the task
        # is alive. Both are read by tests and could back a metric.
        self.sweep_count = 0
        self.total_removed = 0

    async def run(self) -> None:
        """Sweep forever, until the task is cancelled.

        Sleeps *before* the first sweep: startup is the worst moment to
        add database work, and nothing can have expired in the first
        millisecond of uptime.
        """
        logger.info("expiry sweeper started (every %ss)", self._interval)
        try:
            while True:
                await self._sleep(self._interval)
                self._sweep_once()
        except asyncio.CancelledError:
            # Normal shutdown. Returning rather than re-raising keeps
            # `await task` from raising in the caller's cleanup path.
            logger.info("expiry sweeper stopped")

    def _sweep_once(self) -> None:
        """Run one sweep, swallowing failures.

        This try/except is the whole reason the method exists. An
        unhandled exception would terminate the task, expiry would stop
        forever, and nothing would be logged after the first failure --
        a silent outage that only shows up as a mysteriously large
        database weeks later. A locked database or a full disk is
        transient; log it and try again next interval.
        """
        try:
            removed = self._service.purge_expired()
        except Exception:
            logger.exception("expiry sweep failed; will retry next interval")
            self.sweep_count += 1
            return

        self.sweep_count += 1
        self.total_removed += removed
        if removed:
            logger.info("expiry sweep removed %d mailbox(es)", removed)
