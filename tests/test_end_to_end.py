"""The full stack, exercised the way a user exercises it.

Every other test file covers one layer. This one covers the seam
between all of them:

    POST /api/mailboxes   ->  an address exists
    SMTP mail to it       ->  the message is stored
    GET .../messages      ->  the browser can read it

If the layers are wired wrongly -- SMTP holding a different service
instance from HTTP, the event publisher pointing somewhere nobody
listens -- every unit test still passes and only this one fails. That
is exactly what an end-to-end test is for, and why there is only one of
them: they are the slowest and most brittle tests we own, so they earn
their place by covering wiring, not logic.
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

# httpx2, not httpx. Commit 8 moved the test HTTP client to httpx2
# because Starlette deprecated the httpx 0.x backend; importing plain
# `httpx` here worked only because it was still lying around in a
# developer virtualenv from before that switch. A clean install has no
# such leftovers, which is exactly how CI caught it.
from httpx2 import ASGITransport, AsyncClient

from ten_min_mail.address_generator import RandomAddressGenerator
from ten_min_mail.api import create_app
from ten_min_mail.clock import FrozenClock
from ten_min_mail.repository import SqliteMailboxRepository
from ten_min_mail.service import MailboxService

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def free_port() -> int:
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
            rng=random.Random(2026),
            checker=repository,
        ),
    )


@pytest.fixture
async def stack(service: MailboxService) -> AsyncIterator[tuple[AsyncClient, int]]:
    """The whole application: HTTP over ASGI, SMTP on a real port."""
    port = free_port()
    app = create_app(
        database_path=":memory:",
        service=service,
        smtp_host="127.0.0.1",
        smtp_port=port,
    )

    # LifespanManager equivalent: entering the app's router lifespan is
    # what starts the SMTP listener, so we drive it explicitly.
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client, port


async def send_over_smtp(
    port: int, *, sender: str, recipient: str, subject: str, body: str
) -> None:
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = recipient
    mail["Subject"] = subject
    mail.set_content(body)

    def _send() -> None:
        with smtplib.SMTP("127.0.0.1", port, timeout=10) as client:
            client.send_message(mail)

    # smtplib blocks; running it on the loop would deadlock the server
    # that has to answer it. Off-thread also mirrors production, where
    # mail arrives from a thread that is not the event loop.
    await asyncio.to_thread(_send)


class TestFullJourney:
    async def test_mail_sent_over_smtp_is_readable_over_http(
        self, stack: tuple[AsyncClient, int]
    ) -> None:
        client, smtp_port = stack

        # 1. A user asks for a disposable address.
        created = await client.post("/api/mailboxes")
        assert created.status_code == 201
        address = created.json()["address"]

        # 2. Somebody sends real mail to it, over real SMTP.
        await send_over_smtp(
            smtp_port,
            sender="alice@sender.org",
            recipient=address,
            subject="Your verification code",
            body="It is 4821.",
        )

        # 3. The browser reads the inbox.
        inbox = await client.get(f"/api/mailboxes/{address}/messages")
        assert inbox.status_code == 200

        messages = inbox.json()
        assert len(messages) == 1
        assert messages[0]["sender"] == "alice@sender.org"
        assert messages[0]["subject"] == "Your verification code"
        assert "It is 4821." in messages[0]["body"]

    async def test_mail_to_an_unknown_address_is_refused_and_not_stored(
        self, stack: tuple[AsyncClient, int]
    ) -> None:
        client, smtp_port = stack

        with pytest.raises(smtplib.SMTPRecipientsRefused):
            await send_over_smtp(
                smtp_port,
                sender="alice@sender.org",
                recipient="ghost@localhost.test",
                subject="nobody home",
                body="x",
            )

        # And the address still does not exist afterwards.
        response = await client.get("/api/mailboxes/ghost@localhost.test")
        assert response.status_code == 404

    async def test_extended_mailbox_still_receives_mail(
        self, stack: tuple[AsyncClient, int]
    ) -> None:
        client, smtp_port = stack

        address = (await client.post("/api/mailboxes")).json()["address"]

        extended = await client.post(f"/api/mailboxes/{address}/extend")
        assert extended.status_code == 200

        await send_over_smtp(
            smtp_port,
            sender="alice@sender.org",
            recipient=address,
            subject="after extension",
            body="still working",
        )

        messages = (await client.get(f"/api/mailboxes/{address}/messages")).json()
        assert messages[0]["subject"] == "after extension"
