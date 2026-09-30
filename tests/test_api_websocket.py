"""Tests for the live-inbox WebSocket endpoint.

These exercise the full path: a client connects, mail is delivered
through the service, and the frame arrives on the socket without the
client having asked for it. That is the whole point of the feature --
no polling.

TestClient.websocket_connect drives the ASGI app in-process, so there is
still no real network involved.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ten_min_mail.address_generator import RandomAddressGenerator
from ten_min_mail.api import create_app, get_service
from ten_min_mail.clock import FrozenClock
from ten_min_mail.repository import SqliteMailboxRepository
from ten_min_mail.service import MailboxService

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


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
            rng=random.Random(2024),
            checker=repository,
        ),
    )


@pytest.fixture
def client(service: MailboxService) -> Iterator[TestClient]:
    # The service is injected into the factory rather than patched in
    # afterwards. The broadcaster is attached to the service's event
    # publisher during startup, so a service swapped in later would
    # publish to a publisher nobody is listening to -- the sockets would
    # connect fine and then never receive anything.
    app = create_app(database_path=":memory:", service=service)
    app.dependency_overrides[get_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client


class TestConnection:
    def test_can_connect_to_a_live_mailbox(
        self, client: TestClient, service: MailboxService
    ) -> None:
        mailbox = service.create_mailbox()
        with client.websocket_connect(f"/ws/{mailbox.address}") as socket:
            greeting = socket.receive_json()
            assert greeting["type"] == "connected"

    def test_greeting_reports_remaining_time(
        self, client: TestClient, service: MailboxService, clock: FrozenClock
    ) -> None:
        # The client needs to start its countdown immediately, without a
        # second HTTP round-trip.
        mailbox = service.create_mailbox()
        clock.advance(timedelta(minutes=4))

        with client.websocket_connect(f"/ws/{mailbox.address}") as socket:
            greeting = socket.receive_json()
            assert greeting["remaining_seconds"] == 360

    def test_unknown_mailbox_is_rejected(self, client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws/ghost@localhost.test") as socket,
        ):
            socket.receive_json()

    def test_expired_mailbox_is_rejected(
        self, client: TestClient, service: MailboxService, clock: FrozenClock
    ) -> None:
        from starlette.websockets import WebSocketDisconnect

        mailbox = service.create_mailbox()
        clock.advance(timedelta(minutes=11))

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"/ws/{mailbox.address}") as socket,
        ):
            socket.receive_json()


class TestLiveDelivery:
    def test_delivered_message_is_pushed_to_the_client(
        self, client: TestClient, service: MailboxService
    ) -> None:
        # The heart of the feature: the client never asked for this.
        mailbox = service.create_mailbox()

        with client.websocket_connect(f"/ws/{mailbox.address}") as socket:
            socket.receive_json()  # greeting

            service.deliver_message(
                sender="alice@somewhere.io",
                recipient=mailbox.address,
                subject="Your code",
                body="1234",
            )

            frame = socket.receive_json()

        assert frame["type"] == "message"
        assert frame["sender"] == "alice@somewhere.io"
        assert frame["subject"] == "Your code"
        assert frame["body"] == "1234"

    def test_several_messages_arrive_in_order(
        self, client: TestClient, service: MailboxService
    ) -> None:
        mailbox = service.create_mailbox()

        with client.websocket_connect(f"/ws/{mailbox.address}") as socket:
            socket.receive_json()  # greeting

            for subject in ("first", "second"):
                service.deliver_message(
                    sender="a@x.io",
                    recipient=mailbox.address,
                    subject=subject,
                    body="",
                )

            subjects = [socket.receive_json()["subject"] for _ in range(2)]

        assert subjects == ["first", "second"]

    def test_client_does_not_receive_other_mailboxes_mail(
        self, client: TestClient, service: MailboxService
    ) -> None:
        # Cross-delivery here would be a privacy breach, not just a bug.
        watched = service.create_mailbox()
        other = service.create_mailbox()

        with client.websocket_connect(f"/ws/{watched.address}") as socket:
            socket.receive_json()  # greeting

            service.deliver_message(
                sender="a@x.io", recipient=other.address, subject="not yours", body=""
            )
            service.deliver_message(
                sender="a@x.io", recipient=watched.address, subject="yours", body=""
            )

            frame = socket.receive_json()

        assert frame["subject"] == "yours"


class TestDisconnection:
    def test_registry_is_emptied_when_the_client_leaves(
        self, client: TestClient, service: MailboxService
    ) -> None:
        # Leaked registrations are a slow memory leak and cause sends to
        # dead sockets forever after.
        mailbox = service.create_mailbox()
        registry = client.app.state.connections  # type: ignore[attr-defined]

        with client.websocket_connect(f"/ws/{mailbox.address}") as socket:
            socket.receive_json()
            assert registry.connection_count(mailbox.address) == 1

        assert registry.connection_count(mailbox.address) == 0

    def test_two_clients_can_watch_the_same_mailbox(
        self, client: TestClient, service: MailboxService
    ) -> None:
        mailbox = service.create_mailbox()

        with (
            client.websocket_connect(f"/ws/{mailbox.address}") as first,
            client.websocket_connect(f"/ws/{mailbox.address}") as second,
        ):
            first.receive_json()
            second.receive_json()

            service.deliver_message(
                sender="a@x.io", recipient=mailbox.address, subject="hi", body=""
            )

            assert first.receive_json()["subject"] == "hi"
            assert second.receive_json()["subject"] == "hi"
