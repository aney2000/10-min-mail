"""End-to-end tests for the SMTP server over a real socket.

test_smtp_handler.py proves the *rules* by calling the hooks directly.
This module proves the *wiring*: a real server bound to a real port,
driven by Python's own smtplib, speaking actual SMTP.

That distinction matters. Hook-level tests cannot catch a server that
never starts, a port that is not listening, a protocol error, or a
handler that aiosmtpd refuses to call. Those are exactly the failures
that make a mail server look fine in CI and dead in production.
"""

from __future__ import annotations

import asyncio
import random
import smtplib
import socket
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from ten_min_mail.address_generator import RandomAddressGenerator
from ten_min_mail.clock import FrozenClock
from ten_min_mail.repository import SqliteMailboxRepository
from ten_min_mail.service import MailboxService
from ten_min_mail.smtp import SmtpServer

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def free_port() -> int:
    """Ask the OS for an unused port.

    Hardcoding a port makes tests fail when something else happens to be
    listening, and makes parallel test runs collide.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
def service() -> MailboxService:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    repository = SqliteMailboxRepository(connection)
    repository.create_schema()
    return MailboxService(
        repository=repository,
        clock=FrozenClock(T0),
        address_generator=RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(11),
            checker=repository,
        ),
    )


@pytest.fixture
async def smtp_server(service: MailboxService) -> AsyncIterator[SmtpServer]:
    server = SmtpServer(
        service=service,
        mail_domain="localhost.test",
        host="127.0.0.1",
        port=free_port(),
    )
    await server.start()
    yield server
    await server.stop()


async def send_mail(
    server: SmtpServer,
    *,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> None:
    """Send a message with smtplib, off the event loop.

    smtplib is blocking, so calling it directly inside an async test
    would deadlock: the server needs the loop in order to answer, and
    the blocking call is holding it. `to_thread` is the fix -- and it
    also mirrors production, where mail arrives from a foreign thread.
    """
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = recipient
    mail["Subject"] = subject
    mail.set_content(body)

    def _send() -> None:
        with smtplib.SMTP(server.host, server.port, timeout=10) as client:
            client.send_message(mail)

    await asyncio.to_thread(_send)


class TestServerLifecycle:
    async def test_server_listens_on_its_port(self, smtp_server: SmtpServer) -> None:
        def _connect() -> str:
            with smtplib.SMTP(smtp_server.host, smtp_server.port, timeout=10) as client:
                return client.docmd("NOOP")[1].decode()

        await asyncio.to_thread(_connect)

    async def test_greets_with_our_banner(self, smtp_server: SmtpServer) -> None:
        def _banner() -> bytes:
            client = smtplib.SMTP(timeout=10)
            code, msg = client.connect(smtp_server.host, smtp_server.port)
            client.quit()
            assert code == 220
            return msg

        banner = await asyncio.to_thread(_banner)
        assert b"10 Minute Mail" in banner

    async def test_stop_releases_the_port(self, service: MailboxService) -> None:
        # A server that does not release its port on shutdown makes
        # restarts fail with "address already in use".
        port = free_port()
        server = SmtpServer(
            service=service,
            mail_domain="localhost.test",
            host="127.0.0.1",
            port=port,
        )
        await server.start()
        await server.stop()

        # Rebinding proves the listener really let go.
        second = SmtpServer(
            service=service,
            mail_domain="localhost.test",
            host="127.0.0.1",
            port=port,
        )
        await second.start()
        await second.stop()


class TestDeliveryOverTheWire:
    async def test_message_reaches_the_mailbox(
        self, smtp_server: SmtpServer, service: MailboxService
    ) -> None:
        mailbox = service.create_mailbox()

        await send_mail(
            smtp_server,
            sender="alice@sender.org",
            recipient=mailbox.address,
            subject="Your code",
            body="It is 1234.",
        )

        inbox = service.get_messages(mailbox.address)
        assert len(inbox) == 1
        assert inbox[0].subject == "Your code"
        assert "It is 1234." in inbox[0].body
        assert inbox[0].sender == "alice@sender.org"

    async def test_unicode_survives_the_wire(
        self, smtp_server: SmtpServer, service: MailboxService
    ) -> None:
        mailbox = service.create_mailbox()

        await send_mail(
            smtp_server,
            sender="alice@sender.org",
            recipient=mailbox.address,
            subject="Caf\u00e9 \u2014 c\u00f3digo",
            body="H\u00e9llo w\u00f6rld",
        )

        inbox = service.get_messages(mailbox.address)
        assert inbox[0].subject == "Caf\u00e9 \u2014 c\u00f3digo"
        assert "H\u00e9llo w\u00f6rld" in inbox[0].body

    async def test_unknown_recipient_is_rejected_during_the_conversation(
        self, smtp_server: SmtpServer
    ) -> None:
        # The sender must learn *now*, not by never hearing anything.
        mail = EmailMessage()
        mail["From"] = "alice@sender.org"
        mail["To"] = "ghost@localhost.test"
        mail["Subject"] = "hello"
        mail.set_content("body")

        def _send() -> None:
            with smtplib.SMTP(smtp_server.host, smtp_server.port, timeout=10) as client:
                client.send_message(mail)

        with pytest.raises(smtplib.SMTPRecipientsRefused) as caught:
            await asyncio.to_thread(_send)

        code = caught.value.recipients["ghost@localhost.test"][0]
        assert code == 550

    async def test_foreign_domain_is_refused(self, smtp_server: SmtpServer) -> None:
        # Being an open relay is how a mail server ends up blocklisted.
        mail = EmailMessage()
        mail["From"] = "alice@sender.org"
        mail["To"] = "victim@gmail.com"
        mail["Subject"] = "relay me"
        mail.set_content("body")

        def _send() -> None:
            with smtplib.SMTP(smtp_server.host, smtp_server.port, timeout=10) as client:
                client.send_message(mail)

        with pytest.raises(smtplib.SMTPRecipientsRefused):
            await asyncio.to_thread(_send)

    async def test_several_messages_accumulate_in_order(
        self, smtp_server: SmtpServer, service: MailboxService
    ) -> None:
        mailbox = service.create_mailbox()

        for subject in ("first", "second", "third"):
            await send_mail(
                smtp_server,
                sender="alice@sender.org",
                recipient=mailbox.address,
                subject=subject,
                body="x",
            )

        subjects = [m.subject for m in service.get_messages(mailbox.address)]
        assert subjects == ["first", "second", "third"]

    async def test_html_mail_is_stored_as_text(
        self, smtp_server: SmtpServer, service: MailboxService
    ) -> None:
        mailbox = service.create_mailbox()

        mail = EmailMessage()
        mail["From"] = "marketing@sender.org"
        mail["To"] = mailbox.address
        mail["Subject"] = "Promo"
        mail.set_content("Plain fallback.")
        mail.add_alternative("<p>Fancy <b>HTML</b></p>", subtype="html")

        def _send() -> None:
            with smtplib.SMTP(smtp_server.host, smtp_server.port, timeout=10) as client:
                client.send_message(mail)

        await asyncio.to_thread(_send)

        body = service.get_messages(mailbox.address)[0].body
        assert "Plain fallback." in body
        assert "<b>" not in body


class TestLiveUpdateIntegration:
    async def test_delivery_over_smtp_publishes_an_event(
        self, smtp_server: SmtpServer, service: MailboxService
    ) -> None:
        # The architectural payoff, proven end to end: mail arrives on a
        # socket, and a subscriber that knows nothing about SMTP is
        # notified. In production that subscriber is the WebSocket
        # broadcaster, and the browser inbox updates by itself.
        events: list[object] = []
        service.events.subscribe(events.append)

        mailbox = service.create_mailbox()
        await send_mail(
            smtp_server,
            sender="alice@sender.org",
            recipient=mailbox.address,
            subject="live",
            body="update",
        )

        assert len(events) == 1
