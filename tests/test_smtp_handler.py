"""Tests for the SMTP handler's decision logic.

These drive the handler's hooks directly with fake aiosmtpd session and
envelope objects. That is deliberate: the hooks are where all our
*decisions* live (accept or reject a recipient, what to store, what
reply code to send), and testing them in isolation keeps those
assertions fast and precise.

A separate module, test_smtp_server.py, drives a real server over a real
socket with smtplib -- that one proves the wiring, this one proves the
rules.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ten_min_mail.address_generator import RandomAddressGenerator
from ten_min_mail.clock import FrozenClock
from ten_min_mail.repository import SqliteMailboxRepository
from ten_min_mail.service import MailboxService
from ten_min_mail.smtp import MailboxSmtpHandler

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Test doubles for the aiosmtpd protocol objects
# --------------------------------------------------------------------------- #


class FakeSession:
    """Stands in for aiosmtpd's Session (per-connection state)."""

    def __init__(self) -> None:
        self.peer = ("127.0.0.1", 54321)


class FakeEnvelope:
    """Stands in for aiosmtpd's Envelope (per-message state).

    The envelope is the SMTP-level addressing -- MAIL FROM / RCPT TO --
    which is what mail is actually routed by. The From:/To: headers
    inside the message content are just text and may say anything.
    """

    def __init__(
        self,
        *,
        mail_from: str = "alice@sender.org",
        content: bytes = b"Subject: Test\r\n\r\nBody text",
    ) -> None:
        self.mail_from = mail_from
        self.rcpt_tos: list[str] = []
        self.content = content


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


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
            rng=random.Random(7),
            checker=repository,
        ),
    )


@pytest.fixture
def handler(service: MailboxService) -> MailboxSmtpHandler:
    return MailboxSmtpHandler(service, mail_domain="localhost.test")


# --------------------------------------------------------------------------- #
# RCPT TO -- accept or reject, during the conversation
# --------------------------------------------------------------------------- #


class TestRecipientValidation:
    async def test_accepts_a_live_mailbox(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        mailbox = service.create_mailbox()
        envelope = FakeEnvelope()

        reply = await handler.handle_RCPT(
            None, FakeSession(), envelope, mailbox.address, []
        )

        assert reply.startswith("250")
        assert envelope.rcpt_tos == [mailbox.address]

    async def test_rejects_an_unknown_mailbox_with_550(
        self, handler: MailboxSmtpHandler
    ) -> None:
        # Rejecting here, mid-conversation, is the whole point: the
        # sending server learns immediately and can bounce to its user.
        # Accepting and then discarding would make the mail vanish
        # silently while the sender believes it was delivered.
        envelope = FakeEnvelope()

        reply = await handler.handle_RCPT(
            None, FakeSession(), envelope, "ghost@localhost.test", []
        )

        assert reply.startswith("550")
        assert envelope.rcpt_tos == []

    async def test_rejects_an_expired_mailbox_with_550(
        self,
        handler: MailboxSmtpHandler,
        service: MailboxService,
        clock: FrozenClock,
    ) -> None:
        mailbox = service.create_mailbox()
        clock.advance(timedelta(minutes=11))

        reply = await handler.handle_RCPT(
            None, FakeSession(), FakeEnvelope(), mailbox.address, []
        )

        assert reply.startswith("550")

    async def test_rejects_an_address_on_another_domain(
        self, handler: MailboxSmtpHandler
    ) -> None:
        # We are not an open relay. Accepting mail for domains we do not
        # host is how a server ends up on every spam blocklist.
        reply = await handler.handle_RCPT(
            None, FakeSession(), FakeEnvelope(), "someone@gmail.com", []
        )

        assert reply.startswith("550")

    async def test_recipient_matching_is_case_insensitive(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        # Per RFC 5321 the domain is case-insensitive, and in practice
        # senders uppercase local parts too. A mailbox that cannot be
        # reached because someone typed it in caps is a broken mailbox.
        mailbox = service.create_mailbox()
        envelope = FakeEnvelope()

        reply = await handler.handle_RCPT(
            None, FakeSession(), envelope, mailbox.address.upper(), []
        )

        assert reply.startswith("250")

    async def test_angle_brackets_are_tolerated(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        # Some clients pass the raw '<addr>' form through.
        mailbox = service.create_mailbox()

        reply = await handler.handle_RCPT(
            None, FakeSession(), FakeEnvelope(), f"<{mailbox.address}>", []
        )

        assert reply.startswith("250")

    async def test_several_recipients_accumulate(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        first = service.create_mailbox()
        second = service.create_mailbox()
        envelope = FakeEnvelope()

        await handler.handle_RCPT(None, FakeSession(), envelope, first.address, [])
        await handler.handle_RCPT(None, FakeSession(), envelope, second.address, [])

        assert envelope.rcpt_tos == [first.address, second.address]


# --------------------------------------------------------------------------- #
# DATA -- store the message
# --------------------------------------------------------------------------- #


class TestMessageDelivery:
    async def test_stores_the_message_for_the_recipient(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        mailbox = service.create_mailbox()
        envelope = FakeEnvelope(
            content=b"Subject: Your code\r\n\r\nIt is 1234.",
        )
        envelope.rcpt_tos = [mailbox.address]

        reply = await handler.handle_DATA(None, FakeSession(), envelope)

        assert reply.startswith("250")
        inbox = service.get_messages(mailbox.address)
        assert len(inbox) == 1
        assert inbox[0].subject == "Your code"
        assert "It is 1234." in inbox[0].body

    async def test_records_the_envelope_sender_not_the_from_header(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        # The From: header is free text and trivially forged. The
        # envelope sender is what the peer actually declared, so that is
        # what we store.
        mailbox = service.create_mailbox()
        envelope = FakeEnvelope(
            mail_from="real-sender@example.org",
            content=b"From: ceo@yourbank.com\r\nSubject: x\r\n\r\nbody",
        )
        envelope.rcpt_tos = [mailbox.address]

        await handler.handle_DATA(None, FakeSession(), envelope)

        assert service.get_messages(mailbox.address)[0].sender == (
            "real-sender@example.org"
        )

    async def test_delivers_to_every_accepted_recipient(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        first = service.create_mailbox()
        second = service.create_mailbox()
        envelope = FakeEnvelope()
        envelope.rcpt_tos = [first.address, second.address]

        await handler.handle_DATA(None, FakeSession(), envelope)

        assert len(service.get_messages(first.address)) == 1
        assert len(service.get_messages(second.address)) == 1

    async def test_a_mailbox_that_expired_mid_conversation_is_skipped(
        self,
        handler: MailboxSmtpHandler,
        service: MailboxService,
        clock: FrozenClock,
    ) -> None:
        # RCPT was accepted, then the ten minutes ran out before DATA
        # completed. Rare, but a slow sender makes it real. Delivery must
        # not raise -- the message simply has nowhere to go.
        mailbox = service.create_mailbox()
        envelope = FakeEnvelope()
        envelope.rcpt_tos = [mailbox.address]

        clock.advance(timedelta(minutes=11))
        reply = await handler.handle_DATA(None, FakeSession(), envelope)

        assert reply.startswith("250")

    async def test_an_unexpected_storage_error_does_not_lose_other_copies(
        self, service: MailboxService
    ) -> None:
        # A disk error or locked database on ONE recipient must not cost
        # the others their copy, and must not become a 5xx -- the sender
        # would then retry a message we may already have stored, and the
        # user would see it twice.
        good = service.create_mailbox()
        doomed = service.create_mailbox()

        original = service.deliver_message

        def explode_for_one(**kwargs: object) -> object:
            if kwargs["recipient"] == doomed.address:
                raise OSError("disk went away")
            return original(**kwargs)  # type: ignore[arg-type]

        service.deliver_message = explode_for_one  # type: ignore[assignment]

        handler = MailboxSmtpHandler(service, mail_domain="localhost.test")
        envelope = FakeEnvelope()
        envelope.rcpt_tos = [doomed.address, good.address]

        reply = await handler.handle_DATA(None, FakeSession(), envelope)

        service.deliver_message = original  # type: ignore[method-assign]

        assert reply.startswith("250")
        assert len(service.get_messages(good.address)) == 1

    async def test_missing_content_is_handled(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        # aiosmtpd hands over None if DATA completed with nothing in it.
        mailbox = service.create_mailbox()
        envelope = FakeEnvelope()
        envelope.content = None  # type: ignore[assignment]
        envelope.rcpt_tos = [mailbox.address]

        reply = await handler.handle_DATA(None, FakeSession(), envelope)

        assert reply.startswith("250")
        assert service.get_messages(mailbox.address)[0].subject == ""

    async def test_one_failing_recipient_does_not_lose_the_others(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        good = service.create_mailbox()
        envelope = FakeEnvelope()
        envelope.rcpt_tos = ["ghost@localhost.test", good.address]

        await handler.handle_DATA(None, FakeSession(), envelope)

        assert len(service.get_messages(good.address)) == 1

    async def test_publishes_an_event_so_the_live_inbox_updates(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        # The payoff of the event seam: SMTP calls deliver_message and a
        # watching browser updates, with neither knowing the other
        # exists.
        received: list[object] = []
        service.events.subscribe(received.append)

        mailbox = service.create_mailbox()
        envelope = FakeEnvelope()
        envelope.rcpt_tos = [mailbox.address]

        await handler.handle_DATA(None, FakeSession(), envelope)

        assert len(received) == 1

    async def test_malformed_content_is_still_accepted(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        # Bad MIME must not produce a 5xx: the sender did nothing wrong
        # at the protocol level, and our parser is required to cope.
        mailbox = service.create_mailbox()
        envelope = FakeEnvelope(content=b"\xff\xfe not remotely valid mime")
        envelope.rcpt_tos = [mailbox.address]

        reply = await handler.handle_DATA(None, FakeSession(), envelope)

        assert reply.startswith("250")
        assert len(service.get_messages(mailbox.address)) == 1

    async def test_string_content_is_handled(
        self, handler: MailboxSmtpHandler, service: MailboxService
    ) -> None:
        # aiosmtpd hands over bytes normally, but str in some
        # configurations. Coerce rather than crash.
        mailbox = service.create_mailbox()
        envelope = FakeEnvelope()
        envelope.content = "Subject: from a string\r\n\r\nbody"  # type: ignore[assignment]
        envelope.rcpt_tos = [mailbox.address]

        await handler.handle_DATA(None, FakeSession(), envelope)

        assert service.get_messages(mailbox.address)[0].subject == "from a string"
