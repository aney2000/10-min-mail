"""Tests for the Clock abstraction.

The Clock is a tiny seam that lets the rest of the app ask 'what time is
it?' without calling `datetime.now()` directly. That indirection is what
makes time-dependent logic (expiry, extension, sweeps) testable in
microseconds instead of seconds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ten_min_mail.clock import Clock, FrozenClock, SystemClock

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# SystemClock
# --------------------------------------------------------------------------- #


class TestSystemClock:
    def test_returns_utc_aware_datetime(self) -> None:
        # Rule for the whole codebase: no naive datetimes, ever.
        result = SystemClock().now()
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)

    def test_two_consecutive_calls_do_not_go_backward(self) -> None:
        # We do not assert strict '>' because the system clock resolution
        # can produce two equal readings within the same microsecond.
        clock = SystemClock()
        first = clock.now()
        second = clock.now()
        assert second >= first

    def test_satisfies_clock_protocol(self) -> None:
        assert isinstance(SystemClock(), Clock)


# --------------------------------------------------------------------------- #
# FrozenClock
# --------------------------------------------------------------------------- #


class TestFrozenClock:
    def test_returns_the_time_it_was_initialised_with(self) -> None:
        clock = FrozenClock(T0)
        assert clock.now() == T0

    def test_returns_the_same_time_across_multiple_calls(self) -> None:
        # 'Frozen' means frozen. Two reads return identical values until
        # something explicitly advances or sets the clock.
        clock = FrozenClock(T0)
        assert clock.now() == clock.now() == T0

    def test_advance_moves_time_forward(self) -> None:
        clock = FrozenClock(T0)
        clock.advance(timedelta(minutes=5))
        assert clock.now() == T0 + timedelta(minutes=5)

    def test_advance_is_cumulative(self) -> None:
        clock = FrozenClock(T0)
        clock.advance(timedelta(seconds=30))
        clock.advance(timedelta(seconds=90))
        assert clock.now() == T0 + timedelta(minutes=2)

    def test_advance_rejects_negative_delta(self) -> None:
        # `advance` should only move forward. Going backwards is a bug
        # magnet: use `set` explicitly if you truly want to rewind.
        clock = FrozenClock(T0)
        with pytest.raises(ValueError):
            clock.advance(timedelta(seconds=-1))

    def test_set_replaces_the_current_time(self) -> None:
        clock = FrozenClock(T0)
        new_moment = T0 + timedelta(hours=3)
        clock.set(new_moment)
        assert clock.now() == new_moment

    def test_rejects_naive_datetime_on_construction(self) -> None:
        naive = datetime(2026, 1, 1, 12, 0, 0)  # no tzinfo
        with pytest.raises(ValueError):
            FrozenClock(naive)

    def test_rejects_naive_datetime_on_set(self) -> None:
        clock = FrozenClock(T0)
        with pytest.raises(ValueError):
            clock.set(datetime(2026, 1, 1))

    def test_satisfies_clock_protocol(self) -> None:
        assert isinstance(FrozenClock(T0), Clock)
