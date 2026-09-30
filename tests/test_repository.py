"""Tests for the SQLite-backed mailbox/message repository.

We use an in-memory SQLite database (`:memory:`) for tests. It is a *real*
SQL engine -- same semantics as a file-backed DB -- but has no filesystem
I/O, so tests stay fast and hermetic. Each test gets a fresh DB via the
`repo` fixture.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ten_min_mail.domain import Mailbox, Message
from ten_min_mail.repository import (
    MailboxAlreadyExistsError,
    MailboxNotFoundError,
    SqliteMailboxRepository,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def repo() -> SqliteMailboxRepository:
    """A fresh in-memory repository, schema already created.

    `check_same_thread=False` is safe here because tests are single-threaded;
    it also matches what the FastAPI wiring will need later.
    """
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    repository = SqliteMailboxRepository(connection)
    repository.create_schema()
    return repository


def make_mailbox(address: str = "abc@localhost.test") -> Mailbox:
    return Mailbox(
        address=address,
        window_started_at=T0,
        expires_at=T0 + timedelta(minutes=10),
    )


def make_message(recipient: str = "abc@localhost.test", subject: str = "Hi") -> Message:
    return Message(
        sender="alice@somewhere.io",
        recipient=recipient,
        subject=subject,
        body="hello",
        received_at=T0 + timedelta(seconds=30),
    )


# --------------------------------------------------------------------------- #
# Mailbox CRUD
# --------------------------------------------------------------------------- #


class TestMailboxPersistence:
    def test_get_unknown_address_raises(self, repo: SqliteMailboxRepository) -> None:
        with pytest.raises(MailboxNotFoundError):
            repo.get("nobody@localhost.test")

    def test_add_and_get_roundtrip(self, repo: SqliteMailboxRepository) -> None:
        original = make_mailbox()
        repo.add(original)
        loaded = repo.get(original.address)
        # Frozen dataclasses compare structurally; equality proves every field
        # round-tripped correctly (including timezone-aware datetimes).
        assert loaded == original

    def test_adding_same_address_twice_raises(
        self, repo: SqliteMailboxRepository
    ) -> None:
        mailbox = make_mailbox()
        repo.add(mailbox)
        with pytest.raises(MailboxAlreadyExistsError):
            repo.add(mailbox)

    def test_is_taken_true_after_add(self, repo: SqliteMailboxRepository) -> None:
        mailbox = make_mailbox()
        repo.add(mailbox)
        assert repo.is_taken(mailbox.address) is True

    def test_is_taken_false_before_add(self, repo: SqliteMailboxRepository) -> None:
        assert repo.is_taken("nothing@localhost.test") is False

    def test_delete_removes_mailbox(self, repo: SqliteMailboxRepository) -> None:
        mailbox = make_mailbox()
        repo.add(mailbox)
        repo.delete(mailbox.address)
        assert repo.is_taken(mailbox.address) is False
        with pytest.raises(MailboxNotFoundError):
            repo.get(mailbox.address)

    def test_delete_unknown_address_raises(self, repo: SqliteMailboxRepository) -> None:
        with pytest.raises(MailboxNotFoundError):
            repo.delete("ghost@localhost.test")

    def test_replace_updates_expiry(self, repo: SqliteMailboxRepository) -> None:
        # Mailboxes are immutable in the domain, so "extending" one means
        # constructing a new instance and asking the repo to replace the row.
        # We move window_started_at forward too, so the new mailbox still respects
        # the 10-minute MAX_LIFETIME invariant.
        original = make_mailbox()
        repo.add(original)

        extended = Mailbox(
            address=original.address,
            window_started_at=T0 + timedelta(minutes=5),
            expires_at=T0 + timedelta(minutes=15),
        )
        repo.replace(extended)
        assert repo.get(original.address) == extended

    def test_replace_unknown_address_raises(
        self, repo: SqliteMailboxRepository
    ) -> None:
        with pytest.raises(MailboxNotFoundError):
            repo.replace(make_mailbox())


# --------------------------------------------------------------------------- #
# Message persistence
# --------------------------------------------------------------------------- #


class TestMessagePersistence:
    def test_add_message_requires_existing_mailbox(
        self, repo: SqliteMailboxRepository
    ) -> None:
        # Foreign key enforcement: no orphan messages allowed.
        with pytest.raises(MailboxNotFoundError):
            repo.add_message(make_message(recipient="ghost@localhost.test"))

    def test_list_messages_empty_for_new_mailbox(
        self, repo: SqliteMailboxRepository
    ) -> None:
        repo.add(make_mailbox())
        assert repo.list_messages("abc@localhost.test") == []

    def test_added_messages_are_retrievable_in_order(
        self, repo: SqliteMailboxRepository
    ) -> None:
        repo.add(make_mailbox())
        first = Message(
            sender="a@x.io",
            recipient="abc@localhost.test",
            subject="one",
            body="1",
            received_at=T0 + timedelta(seconds=10),
        )
        second = Message(
            sender="b@x.io",
            recipient="abc@localhost.test",
            subject="two",
            body="2",
            received_at=T0 + timedelta(seconds=20),
        )
        repo.add_message(first)
        repo.add_message(second)

        messages = repo.list_messages("abc@localhost.test")
        assert messages == [first, second]  # newest last, chronological

    def test_deleting_mailbox_cascades_to_messages(
        self, repo: SqliteMailboxRepository
    ) -> None:
        repo.add(make_mailbox())
        repo.add_message(make_message())
        repo.delete("abc@localhost.test")

        # Recreate the mailbox and confirm no ghost messages remain.
        repo.add(make_mailbox())
        assert repo.list_messages("abc@localhost.test") == []


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #


class TestHousekeeping:
    def test_delete_expired_removes_only_expired_mailboxes(
        self, repo: SqliteMailboxRepository
    ) -> None:
        alive = Mailbox(
            address="alive@localhost.test",
            window_started_at=T0,
            expires_at=T0 + timedelta(minutes=10),
        )
        dead = Mailbox(
            address="dead@localhost.test",
            window_started_at=T0 - timedelta(minutes=20),
            expires_at=T0 - timedelta(minutes=10),
        )
        repo.add(alive)
        repo.add(dead)

        removed = repo.delete_expired(now=T0)

        assert removed == 1
        assert repo.is_taken("alive@localhost.test") is True
        assert repo.is_taken("dead@localhost.test") is False


# --------------------------------------------------------------------------- #
# Protocol conformance
# --------------------------------------------------------------------------- #


class TestChecker:
    def test_repo_satisfies_availability_checker_protocol(
        self, repo: SqliteMailboxRepository
    ) -> None:
        # The address generator only needs `is_taken`. The repo must be
        # usable as an AddressAvailabilityChecker without adaptation.
        from ten_min_mail.address_generator import AddressAvailabilityChecker

        assert isinstance(repo, AddressAvailabilityChecker)
