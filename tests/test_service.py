"""Tests for the MailboxService -- the use-case layer.

This is where the four building blocks (domain, repository, clock,
address generator) are composed into things a *user* wants to do:
create a mailbox, read its inbox, extend it, let it expire.

Note what these tests do NOT need: no web server, no network, no
sleeping, no real filesystem. Every collaborator is either an in-memory
implementation or a frozen clock, so the full lifecycle of a mailbox --
creation, extension, expiry -- runs in microseconds.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ten_min_mail.address_generator import RandomAddressGenerator
from ten_min_mail.clock import FrozenClock
from ten_min_mail.domain import MAX_LIFETIME
from ten_min_mail.repository import MailboxNotFoundError, SqliteMailboxRepository
from ten_min_mail.service import MailboxExpiredError, MailboxService

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures -- assemble a fully wired service from real (but in-memory) parts
# --------------------------------------------------------------------------- #


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
def repository() -> SqliteMailboxRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    repo = SqliteMailboxRepository(connection)
    repo.create_schema()
    return repo


@pytest.fixture
def service(clock: FrozenClock, repository: SqliteMailboxRepository) -> MailboxService:
    # We use the real generator and the real repository here, not fakes.
    # They are fast and in-memory, so there is no reason to fake them --
    # and testing against the real collaborators catches integration bugs
    # that mock-heavy tests famously miss.
    generator = RandomAddressGenerator(
        domain="localhost.test",
        rng=random.Random(1234),
        checker=repository,
    )
    return MailboxService(
        repository=repository,
        clock=clock,
        address_generator=generator,
    )


# --------------------------------------------------------------------------- #
# Creating mailboxes
# --------------------------------------------------------------------------- #


class TestCreateMailbox:
    def test_returns_a_mailbox_on_our_domain(self, service: MailboxService) -> None:
        mailbox = service.create_mailbox()
        assert mailbox.address.endswith("@localhost.test")

    def test_new_mailbox_is_valid_for_the_full_ten_minutes(
        self, service: MailboxService
    ) -> None:
        mailbox = service.create_mailbox()
        assert mailbox.window_started_at == T0
        assert mailbox.expires_at == T0 + MAX_LIFETIME
        assert mailbox.remaining_seconds(now=T0) == 600

    def test_created_mailbox_is_persisted(
        self, service: MailboxService, repository: SqliteMailboxRepository
    ) -> None:
        mailbox = service.create_mailbox()
        assert repository.get(mailbox.address) == mailbox

    def test_two_mailboxes_get_different_addresses(
        self, service: MailboxService
    ) -> None:
        first = service.create_mailbox()
        second = service.create_mailbox()
        assert first.address != second.address


# --------------------------------------------------------------------------- #
# Reading mailboxes
# --------------------------------------------------------------------------- #


class TestGetMailbox:
    def test_returns_the_stored_mailbox(self, service: MailboxService) -> None:
        created = service.create_mailbox()
        assert service.get_mailbox(created.address) == created

    def test_unknown_address_raises_not_found(self, service: MailboxService) -> None:
        with pytest.raises(MailboxNotFoundError):
            service.get_mailbox("nobody@localhost.test")

    def test_expired_mailbox_raises_expired_not_found(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        # An expired mailbox may still physically be in the database (the
        # sweeper runs periodically, not instantly). The service must treat
        # it as gone regardless -- storage lag is not the caller's problem.
        created = service.create_mailbox()
        clock.advance(MAX_LIFETIME)
        with pytest.raises(MailboxExpiredError):
            service.get_mailbox(created.address)


# --------------------------------------------------------------------------- #
# Extending mailboxes -- the core business rule
# --------------------------------------------------------------------------- #


class TestExtendMailbox:
    def test_extension_resets_the_window_to_a_full_ten_minutes(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        created = service.create_mailbox()
        clock.advance(timedelta(minutes=8))  # 2 minutes left

        extended = service.extend_mailbox(created.address)

        assert extended.remaining_seconds(now=clock.now()) == 600
        assert extended.expires_at == T0 + timedelta(minutes=18)

    def test_extension_never_exceeds_ten_minutes_of_remaining_time(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        # Extending immediately after creation must NOT stack to 20 minutes.
        # This is the exact rule the user asked for: the button resets the
        # window, it does not accumulate.
        created = service.create_mailbox()
        extended = service.extend_mailbox(created.address)

        assert extended.remaining_seconds(now=clock.now()) == 600
        assert extended.expires_at == created.expires_at

    def test_repeated_extensions_still_cap_at_ten_minutes(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        created = service.create_mailbox()
        for _ in range(5):
            clock.advance(timedelta(minutes=1))
            extended = service.extend_mailbox(created.address)
            assert extended.remaining_seconds(now=clock.now()) == 600

    def test_extension_is_persisted(
        self,
        service: MailboxService,
        repository: SqliteMailboxRepository,
        clock: FrozenClock,
    ) -> None:
        created = service.create_mailbox()
        clock.advance(timedelta(minutes=4))
        extended = service.extend_mailbox(created.address)

        assert repository.get(created.address) == extended

    def test_extension_keeps_the_same_address(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        created = service.create_mailbox()
        clock.advance(timedelta(minutes=2))
        extended = service.extend_mailbox(created.address)
        assert extended.address == created.address

    def test_cannot_extend_an_expired_mailbox(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        # Design decision: expiry is final. Resurrecting a dead address
        # would let someone reclaim a mailbox that has already been handed
        # out to (or read by) somebody else.
        created = service.create_mailbox()
        clock.advance(MAX_LIFETIME + timedelta(seconds=1))
        with pytest.raises(MailboxExpiredError):
            service.extend_mailbox(created.address)

    def test_cannot_extend_unknown_mailbox(self, service: MailboxService) -> None:
        with pytest.raises(MailboxNotFoundError):
            service.extend_mailbox("nobody@localhost.test")


# --------------------------------------------------------------------------- #
# Inbox
# --------------------------------------------------------------------------- #


class TestMessages:
    def test_new_mailbox_has_empty_inbox(self, service: MailboxService) -> None:
        created = service.create_mailbox()
        assert service.get_messages(created.address) == []

    def test_delivered_message_appears_in_inbox(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        created = service.create_mailbox()
        clock.advance(timedelta(seconds=30))

        service.deliver_message(
            sender="alice@somewhere.io",
            recipient=created.address,
            subject="Hello there",
            body="Your code is 1234",
        )

        inbox = service.get_messages(created.address)
        assert len(inbox) == 1
        assert inbox[0].sender == "alice@somewhere.io"
        assert inbox[0].subject == "Hello there"
        assert inbox[0].body == "Your code is 1234"
        # The service stamps the arrival time from the clock -- callers
        # (the SMTP handler) do not get to invent timestamps.
        assert inbox[0].received_at == T0 + timedelta(seconds=30)

    def test_messages_are_returned_in_arrival_order(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        created = service.create_mailbox()

        service.deliver_message(
            sender="a@x.io", recipient=created.address, subject="1", body=""
        )
        clock.advance(timedelta(seconds=10))
        service.deliver_message(
            sender="b@x.io", recipient=created.address, subject="2", body=""
        )

        subjects = [m.subject for m in service.get_messages(created.address)]
        assert subjects == ["1", "2"]

    def test_delivery_to_unknown_mailbox_raises(self, service: MailboxService) -> None:
        with pytest.raises(MailboxNotFoundError):
            service.deliver_message(
                sender="a@x.io",
                recipient="nobody@localhost.test",
                subject="s",
                body="b",
            )

    def test_delivery_to_expired_mailbox_raises(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        created = service.create_mailbox()
        clock.advance(MAX_LIFETIME)
        with pytest.raises(MailboxExpiredError):
            service.deliver_message(
                sender="a@x.io",
                recipient=created.address,
                subject="s",
                body="b",
            )

    def test_reading_inbox_of_expired_mailbox_raises(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        created = service.create_mailbox()
        clock.advance(MAX_LIFETIME)
        with pytest.raises(MailboxExpiredError):
            service.get_messages(created.address)


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #


class TestPurgeExpired:
    def test_purge_removes_expired_mailboxes_only(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        old = service.create_mailbox()
        clock.advance(timedelta(minutes=6))
        fresh = service.create_mailbox()

        # Now advance past `old`'s expiry but not past `fresh`'s.
        clock.advance(timedelta(minutes=5))  # T0 + 11min

        removed = service.purge_expired()

        assert removed == 1
        assert service.get_mailbox(fresh.address).address == fresh.address
        with pytest.raises(MailboxNotFoundError):
            service.get_mailbox(old.address)

    def test_purge_returns_zero_when_nothing_expired(
        self, service: MailboxService
    ) -> None:
        service.create_mailbox()
        assert service.purge_expired() == 0

    def test_purge_also_removes_messages(
        self, service: MailboxService, clock: FrozenClock
    ) -> None:
        created = service.create_mailbox()
        service.deliver_message(
            sender="a@x.io", recipient=created.address, subject="s", body="b"
        )
        clock.advance(MAX_LIFETIME)
        service.purge_expired()

        with pytest.raises(MailboxNotFoundError):
            service.get_messages(created.address)
