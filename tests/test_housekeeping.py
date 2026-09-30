"""Tests for the periodic expiry sweep.

Expired mailboxes must not linger: the database would grow without
bound, addresses could never be recycled, and -- for a service whose
entire promise is that mail disappears -- the content would outlive the
mailbox that held it. That last point makes this a privacy feature, not
just housekeeping.

The sleep function is injected for the same reason the clock is: a test
that genuinely waited sixty seconds between sweeps would be useless.
The fake sleep returns immediately and records how long it was asked to
wait, so we can assert the schedule without living through it.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ten_min_mail.address_generator import RandomAddressGenerator
from ten_min_mail.clock import FrozenClock
from ten_min_mail.housekeeping import ExpirySweeper
from ten_min_mail.repository import SqliteMailboxRepository
from ten_min_mail.service import MailboxService

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class RecordingSleep:
    """A stand-in for `asyncio.sleep` that never actually waits.

    Records each requested duration, and stops the loop after a set
    number of calls by raising CancelledError -- which is exactly what
    a real cancellation looks like to the sweeper, so the production
    shutdown path is what gets exercised.
    """

    def __init__(self, *, stop_after: int = 3) -> None:
        self.durations: list[float] = []
        self._stop_after = stop_after

    async def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)
        if len(self.durations) >= self._stop_after:
            raise asyncio.CancelledError
        # Yield control so other tasks can run, without real delay.
        await asyncio.sleep(0)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
def service(clock: FrozenClock) -> MailboxService:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    repository = SqliteMailboxRepository(connection)
    repository.create_schema()
    return MailboxService(
        repository=repository,
        clock=clock,
        address_generator=RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(5),
            checker=repository,
        ),
    )


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #


class TestSchedule:
    async def test_sweeps_repeatedly(self, service: MailboxService) -> None:
        sleeper = RecordingSleep(stop_after=4)
        sweeper = ExpirySweeper(service, interval_seconds=60, sleep=sleeper)

        await sweeper.run()

        assert len(sleeper.durations) == 4

    async def test_waits_the_configured_interval(self, service: MailboxService) -> None:
        sleeper = RecordingSleep(stop_after=2)
        sweeper = ExpirySweeper(service, interval_seconds=30, sleep=sleeper)

        await sweeper.run()

        assert sleeper.durations == [30, 30]

    async def test_sleeps_before_the_first_sweep(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        # Startup is the worst moment for extra database work, and
        # nothing can have expired in the first millisecond of uptime.
        service.create_mailbox()
        clock.advance(timedelta(minutes=11))

        sleeper = RecordingSleep(stop_after=1)  # cancels during first sleep
        sweeper = ExpirySweeper(service, interval_seconds=60, sleep=sleeper)

        await sweeper.run()

        assert sweeper.sweep_count == 0

    async def test_rejects_a_non_positive_interval(
        self, service: MailboxService
    ) -> None:
        # A zero or negative interval is a busy loop that pins a CPU.
        with pytest.raises(ValueError):
            ExpirySweeper(service, interval_seconds=0)


# --------------------------------------------------------------------------- #
# Sweeping
# --------------------------------------------------------------------------- #


class TestSweeping:
    async def test_removes_expired_mailboxes(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        from ten_min_mail.repository import MailboxNotFoundError

        mailbox = service.create_mailbox()
        clock.advance(timedelta(minutes=11))

        sleeper = RecordingSleep(stop_after=2)
        sweeper = ExpirySweeper(service, interval_seconds=60, sleep=sleeper)
        await sweeper.run()

        with pytest.raises(MailboxNotFoundError):
            service.get_mailbox(mailbox.address)

    async def test_leaves_live_mailboxes_alone(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        mailbox = service.create_mailbox()
        clock.advance(timedelta(minutes=3))

        sleeper = RecordingSleep(stop_after=2)
        await ExpirySweeper(service, interval_seconds=60, sleep=sleeper).run()

        assert service.get_mailbox(mailbox.address).address == mailbox.address

    async def test_counts_the_mailboxes_it_removed(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        service.create_mailbox()
        service.create_mailbox()
        clock.advance(timedelta(minutes=11))

        sleeper = RecordingSleep(stop_after=2)
        sweeper = ExpirySweeper(service, interval_seconds=60, sleep=sleeper)
        await sweeper.run()

        assert sweeper.total_removed == 2


# --------------------------------------------------------------------------- #
# Resilience -- a background task that dies stops working silently
# --------------------------------------------------------------------------- #


class TestResilience:
    async def test_a_failing_sweep_does_not_end_the_loop(self) -> None:
        # This is the important one. An unhandled exception terminates
        # the task, expiry stops forever, and nothing is logged after
        # the first failure -- a silent outage. The loop must survive a
        # transient database error and try again next interval.
        class BrokenService:
            def __init__(self) -> None:
                self.calls = 0

            def purge_expired(self) -> int:
                self.calls += 1
                raise sqlite3.OperationalError("database is locked")

        broken = BrokenService()
        sleeper = RecordingSleep(stop_after=3)
        # No type: ignore needed -- BrokenService satisfies the Purgeable
        # Protocol structurally, just by having purge_expired(). That is
        # the payoff of a narrow interface: a six-line fake is a valid
        # collaborator, verified by mypy rather than merely assumed.
        sweeper = ExpirySweeper(broken, interval_seconds=60, sleep=sleeper)

        await sweeper.run()  # must not propagate

        assert broken.calls == 2  # kept going after the first failure

    async def test_cancellation_stops_the_loop_cleanly(
        self, service: MailboxService
    ) -> None:
        # Shutdown cancels the task. It must exit rather than swallow
        # the cancellation and keep running.
        sweeper = ExpirySweeper(service, interval_seconds=0.01)
        task = asyncio.create_task(sweeper.run())

        await asyncio.sleep(0.05)
        task.cancel()
        await task  # must not raise

        assert task.done()

    async def test_real_sleep_is_used_by_default(self, service: MailboxService) -> None:
        # The injected sleep is a test seam, not a required argument;
        # production must work without passing one.
        sweeper = ExpirySweeper(service, interval_seconds=0.01)
        task = asyncio.create_task(sweeper.run())

        await asyncio.sleep(0.05)
        task.cancel()
        await task

        assert sweeper.sweep_count >= 1
