"""Clock abstraction -- the seam that keeps time-dependent code testable.

Rule for the whole codebase
---------------------------
Every datetime that enters the system through this module (or any other)
is timezone-aware and UTC. Naive datetimes are rejected at construction
time. This eliminates a whole category of bugs where 'now' means one
thing on the developer laptop and another on the server.

Why a Protocol
--------------
A named type (`Clock`) reads better in signatures than `Callable[[], datetime]`
and leaves room for the interface to grow (e.g., adding `sleep` or
`monotonic`) without touching call sites.

Why two implementations
-----------------------
  * SystemClock -- production. Reads the OS clock.
  * FrozenClock -- tests. Deterministic; advances only when told.

Everything else in the codebase that needs the current time should accept
a `Clock` in its constructor. It should never import `datetime.now`
directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #


@runtime_checkable
class Clock(Protocol):
    """Anything that can answer 'what time is it, in UTC?'."""

    def now(self) -> datetime: ...  # pragma: no cover


# --------------------------------------------------------------------------- #
# Production implementation
# --------------------------------------------------------------------------- #


class SystemClock:
    """Reads the operating-system clock. Always returns UTC-aware time."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


# --------------------------------------------------------------------------- #
# Test implementation
# --------------------------------------------------------------------------- #


class FrozenClock:
    """A clock that only moves when you tell it to.

    Typical use in a test:

        clock = FrozenClock(T0)
        service = MailboxService(clock=clock, ...)
        service.create()
        clock.advance(timedelta(minutes=11))
        assert service.get(...).is_expired(clock.now())
    """

    def __init__(self, initial: datetime) -> None:
        _require_aware_utc(initial)
        self._current: datetime = initial

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        """Move time forward by `delta`. Refuses negative deltas."""
        if delta < timedelta(0):
            raise ValueError("advance() only moves forward; use set() to rewind")
        self._current += delta

    def set(self, moment: datetime) -> None:
        """Replace the current time with `moment` (can be earlier or later)."""
        _require_aware_utc(moment)
        self._current = moment


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #


def _require_aware_utc(dt: datetime) -> None:
    """Guard the invariant: only UTC-aware datetimes cross this boundary."""
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError(f"datetime must be timezone-aware and UTC, got {dt!r}")
