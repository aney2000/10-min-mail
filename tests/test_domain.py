"""Tests for the pure domain model.

These tests describe the business rules of a mailbox and a message,
independent of any database, HTTP framework, or SMTP server.

Design principle: `now` is always injected. The domain never reads the
system clock. That keeps tests deterministic and fast.
"""

from datetime import UTC, datetime, timedelta

import pytest

from ten_min_mail.domain import (
    MAX_LIFETIME,
    InvalidEmailAddressError,
    Mailbox,
    Message,
)

# A fixed reference point used across tests. Using UTC everywhere avoids
# a whole category of "works in my timezone" bugs.
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Mailbox
# --------------------------------------------------------------------------- #


class TestMailbox:
    def test_created_mailbox_exposes_its_fields(self) -> None:
        mailbox = Mailbox(
            address="abc123@example.com",
            window_started_at=T0,
            expires_at=T0 + timedelta(minutes=10),
        )
        assert mailbox.address == "abc123@example.com"
        assert mailbox.window_started_at == T0
        assert mailbox.expires_at == T0 + timedelta(minutes=10)

    def test_mailbox_is_frozen(self) -> None:
        # Immutability protects us from accidental state changes.
        mailbox = Mailbox(
            address="a@b.io",
            window_started_at=T0,
            expires_at=T0 + timedelta(minutes=10),
        )
        with pytest.raises(Exception):  # dataclasses raises FrozenInstanceError
            mailbox.address = "hacked@evil.io"  # type: ignore[misc]

    def test_is_not_expired_before_expiry(self) -> None:
        mailbox = Mailbox(
            address="a@b.io",
            window_started_at=T0,
            expires_at=T0 + timedelta(minutes=10),
        )
        assert mailbox.is_expired(now=T0 + timedelta(minutes=9)) is False

    def test_is_expired_exactly_at_expiry(self) -> None:
        # Boundary condition: expiry is inclusive — the moment `now == expires_at`,
        # the mailbox is considered gone. Documenting this in a test is how
        # future-you remembers the decision.
        mailbox = Mailbox(
            address="a@b.io",
            window_started_at=T0,
            expires_at=T0 + timedelta(minutes=10),
        )
        assert mailbox.is_expired(now=T0 + timedelta(minutes=10)) is True

    def test_is_expired_after_expiry(self) -> None:
        mailbox = Mailbox(
            address="a@b.io",
            window_started_at=T0,
            expires_at=T0 + timedelta(minutes=10),
        )
        assert mailbox.is_expired(now=T0 + timedelta(minutes=11)) is True

    def test_remaining_seconds_counts_down(self) -> None:
        mailbox = Mailbox(
            address="a@b.io",
            window_started_at=T0,
            expires_at=T0 + timedelta(minutes=10),
        )
        assert mailbox.remaining_seconds(now=T0) == 600
        assert mailbox.remaining_seconds(now=T0 + timedelta(minutes=3)) == 420

    def test_remaining_seconds_is_zero_when_expired(self) -> None:
        # We never return a negative number. Callers shouldn't have to
        # write `max(0, mailbox.remaining_seconds(...))` everywhere.
        mailbox = Mailbox(
            address="a@b.io",
            window_started_at=T0,
            expires_at=T0 + timedelta(minutes=10),
        )
        assert mailbox.remaining_seconds(now=T0 + timedelta(minutes=15)) == 0

    def test_remaining_seconds_rounds_up_partial_seconds(self) -> None:
        # A countdown must round UP. Truncating means a user who has just
        # created a mailbox sees "9:59" because a few microseconds passed
        # between creation and rendering -- the clock appears to start
        # already-running. With 599.9 seconds genuinely left, "600" is the
        # honest answer; "599" is not.
        mailbox = Mailbox(
            address="a@b.io",
            window_started_at=T0,
            expires_at=T0 + timedelta(minutes=10),
        )
        just_after = T0 + timedelta(microseconds=100)
        assert mailbox.remaining_seconds(now=just_after) == 600

    def test_remaining_seconds_rounds_up_mid_second(self) -> None:
        mailbox = Mailbox(
            address="a@b.io",
            window_started_at=T0,
            expires_at=T0 + timedelta(minutes=10),
        )
        # 1.5 seconds left should read as 2, not 1: there is still part of
        # the second second remaining.
        now = T0 + timedelta(minutes=10) - timedelta(milliseconds=1500)
        assert mailbox.remaining_seconds(now=now) == 2

    def test_remaining_seconds_is_zero_exactly_at_expiry(self) -> None:
        # Rounding up must not resurrect an expired mailbox as "1 second
        # left". At the boundary the answer is exactly zero.
        mailbox = Mailbox(
            address="a@b.io",
            window_started_at=T0,
            expires_at=T0 + timedelta(minutes=10),
        )
        assert mailbox.remaining_seconds(now=T0 + timedelta(minutes=10)) == 0

    @pytest.mark.parametrize(
        "bad_address",
        [
            "",  # empty
            "no-at-sign.com",  # missing '@'
            "@no-local.com",  # missing local part
            "no-domain@",  # missing domain
            "spaces in@x.com",  # whitespace
            "a@b",  # domain without a dot
        ],
    )
    def test_rejects_invalid_addresses(self, bad_address: str) -> None:
        with pytest.raises(InvalidEmailAddressError):
            Mailbox(
                address=bad_address,
                window_started_at=T0,
                expires_at=T0 + timedelta(minutes=10),
            )

    def test_rejects_expiry_before_creation(self) -> None:
        # Illegal state: a mailbox that expires before it was born.
        with pytest.raises(ValueError):
            Mailbox(
                address="a@b.io",
                window_started_at=T0,
                expires_at=T0 - timedelta(seconds=1),
            )

    def test_rejects_lifetime_above_maximum(self) -> None:
        # Business rule: max lifetime is 10 minutes.
        with pytest.raises(ValueError):
            Mailbox(
                address="a@b.io",
                window_started_at=T0,
                expires_at=T0 + MAX_LIFETIME + timedelta(seconds=1),
            )

    def test_maximum_lifetime_is_exactly_ten_minutes(self) -> None:
        # If someone changes MAX_LIFETIME by accident, this test screams.
        # SIM300 ("Yoda condition") is suppressed here: the constant under
        # test belongs on the left. The rule exists for `if "admin" == role`,
        # not for assertions whose subject is a named constant.
        assert MAX_LIFETIME == timedelta(minutes=10)  # noqa: SIM300


# --------------------------------------------------------------------------- #
# Message
# --------------------------------------------------------------------------- #


class TestMessage:
    def test_message_exposes_its_fields(self) -> None:
        msg = Message(
            sender="alice@somewhere.com",
            recipient="abc123@example.com",
            subject="Hello",
            body="Welcome!",
            received_at=T0,
        )
        assert msg.sender == "alice@somewhere.com"
        assert msg.recipient == "abc123@example.com"
        assert msg.subject == "Hello"
        assert msg.body == "Welcome!"
        assert msg.received_at == T0

    def test_message_is_frozen(self) -> None:
        msg = Message(
            sender="a@b.io",
            recipient="c@d.io",
            subject="s",
            body="b",
            received_at=T0,
        )
        with pytest.raises(Exception):
            msg.subject = "changed"  # type: ignore[misc]

    def test_empty_subject_and_body_are_allowed(self) -> None:
        # Real emails sometimes have no subject or no body. Not our job to reject.
        msg = Message(
            sender="a@b.io",
            recipient="c@d.io",
            subject="",
            body="",
            received_at=T0,
        )
        assert msg.subject == ""
        assert msg.body == ""
