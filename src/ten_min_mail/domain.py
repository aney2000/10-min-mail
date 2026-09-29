"""Pure domain model for the disposable mail service.

This module has ZERO dependencies on FastAPI, SQL, or SMTP. It describes
the business rules only. Any framework code (HTTP layer, database layer,
SMTP handler) will *import from* this module, never the other way around.

That direction of dependency is the essence of Clean / Hexagonal architecture:
the core is stable; the details (frameworks) are replaceable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# --------------------------------------------------------------------------- #
# Business constants
# --------------------------------------------------------------------------- #

#: The maximum lifetime any mailbox is allowed to have.
#: Extending a mailbox may never push `expires_at` further than
#: `now + MAX_LIFETIME`. This is a hard cap enforced at construction time.
MAX_LIFETIME: timedelta = timedelta(minutes=10)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class InvalidEmailAddressError(ValueError):
    """Raised when an address does not look like a usable email address.

    We subclass ValueError so callers can `except ValueError` generically
    if they don't care about the specific reason.
    """


# --------------------------------------------------------------------------- #
# Email address validation
# --------------------------------------------------------------------------- #

# Deliberately simple. Real RFC 5321 is a nightmare and irrelevant here:
# we are the ones generating addresses, so we control the shape completely.
# Rules:
#   - non-empty local part (letters, digits, ._%+-)
#   - single '@'
#   - domain with at least one dot (e.g. 'example.com')
#   - no whitespace anywhere
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _is_valid_email(address: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(address))


# --------------------------------------------------------------------------- #
# Mailbox
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Mailbox:
    """An email address that is valid for a limited window of time.

    Immutable by design (`frozen=True`). To "extend" a mailbox we build a
    new instance rather than mutating an old one — see the service layer.
    """

    address: str
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        # `__post_init__` runs after the dataclass __init__ assigns fields.
        # This is where we enforce invariants that "make illegal states
        # unrepresentable": if construction succeeds, the object is valid.
        if not _is_valid_email(self.address):
            raise InvalidEmailAddressError(
                f"Not a valid email address: {self.address!r}"
            )
        if self.expires_at < self.created_at:
            raise ValueError("expires_at must be >= created_at")
        if self.expires_at - self.created_at > MAX_LIFETIME:
            raise ValueError(f"lifetime exceeds maximum of {MAX_LIFETIME}")

    # --- Time-dependent queries ---------------------------------------------
    # These methods take `now` as a parameter instead of calling
    # datetime.now() internally. That is a *deliberate* design choice:
    # it makes the domain trivially unit-testable and deterministic.

    def is_expired(self, now: datetime) -> bool:
        """Return True once `now` has reached or passed `expires_at`."""
        return now >= self.expires_at

    def remaining_seconds(self, now: datetime) -> int:
        """Seconds left before expiry, clamped at 0 (never negative)."""
        delta = (self.expires_at - now).total_seconds()
        if delta < 0:
            return 0
        return int(delta)


# --------------------------------------------------------------------------- #
# Message
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Message:
    """An email delivered to one of our mailboxes.

    Kept intentionally minimal for now. Attachments, HTML bodies, headers
    other than From/To/Subject, and MIME parts can be added later without
    breaking existing code — the field set only grows.
    """

    sender: str
    recipient: str
    subject: str
    body: str
    received_at: datetime
