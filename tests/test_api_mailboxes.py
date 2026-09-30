"""Tests for the mailbox REST endpoints.

These drive the HTTP layer with a service wired to a FrozenClock, using
the `dependency_overrides` seam proved out in test_api_health.py. That is
what lets a test assert "extending at minute eight still gives exactly
ten minutes" without taking eight minutes to run.

What is under test here is *translation*, not business rules: correct
status codes, correct JSON shape, domain exceptions mapped to the right
HTTP semantics. The rules themselves are already covered in
test_service.py and are not re-asserted through HTTP.
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


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
def client(clock: FrozenClock) -> Iterator[TestClient]:
    """A client whose service runs on a frozen clock and in-memory DB.

    The app is built normally (so routing, models and exception handlers
    are the real ones), then `get_service` is overridden to return a
    service we control. Nothing in `api.py` changes to accommodate the
    test -- that is the point of the seam.
    """
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    repository = SqliteMailboxRepository(connection)
    repository.create_schema()

    service = MailboxService(
        repository=repository,
        clock=clock,
        address_generator=RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(99),
            checker=repository,
        ),
    )

    app = create_app(database_path=":memory:")
    app.dependency_overrides[get_service] = lambda: service

    with TestClient(app) as test_client:
        yield test_client

    connection.close()


def create_mailbox(client: TestClient) -> str:
    """Helper: create a mailbox and return its address."""
    response = client.post("/api/mailboxes")
    assert response.status_code == 201
    address: str = response.json()["address"]
    return address


# --------------------------------------------------------------------------- #
# POST /api/mailboxes
# --------------------------------------------------------------------------- #


class TestCreateMailbox:
    def test_returns_201_created(self, client: TestClient) -> None:
        # 201, not 200: a new resource came into existence.
        assert client.post("/api/mailboxes").status_code == 201

    def test_returns_address_on_our_domain(self, client: TestClient) -> None:
        body = client.post("/api/mailboxes").json()
        assert body["address"].endswith("@localhost.test")

    def test_returns_full_ten_minutes_remaining(self, client: TestClient) -> None:
        body = client.post("/api/mailboxes").json()
        assert body["remaining_seconds"] == 600

    def test_returns_expiry_timestamp(self, client: TestClient) -> None:
        # Pydantic serialises UTC with a 'Z' suffix (RFC 3339) rather than
        # Python's '+00:00'. Both are valid ISO-8601; 'Z' is the
        # conventional form for JSON APIs and is what `new Date(...)`
        # in the browser parses most reliably, so we assert the wire
        # format explicitly rather than reproducing it with isoformat().
        body = client.post("/api/mailboxes").json()
        assert body["expires_at"] == "2026-01-01T12:10:00Z"

    def test_expiry_timestamp_is_parseable_as_utc(self, client: TestClient) -> None:
        # Guards the contract from the consumer's side: whatever spelling
        # we emit must round-trip back to the instant we meant.
        body = client.post("/api/mailboxes").json()
        parsed = datetime.fromisoformat(body["expires_at"])
        assert parsed == T0 + timedelta(minutes=10)
        assert parsed.tzinfo is not None

    def test_response_has_no_unexpected_fields(self, client: TestClient) -> None:
        # The API contract is deliberately narrower than the domain object.
        # Returning the Mailbox directly would leak internal fields and
        # couple every client to our storage model.
        body = client.post("/api/mailboxes").json()
        assert set(body) == {"address", "expires_at", "remaining_seconds"}

    def test_repeated_calls_return_distinct_addresses(self, client: TestClient) -> None:
        first = client.post("/api/mailboxes").json()["address"]
        second = client.post("/api/mailboxes").json()["address"]
        assert first != second


# --------------------------------------------------------------------------- #
# GET /api/mailboxes/{address}
# --------------------------------------------------------------------------- #


class TestGetMailbox:
    def test_returns_200_for_live_mailbox(self, client: TestClient) -> None:
        address = create_mailbox(client)
        assert client.get(f"/api/mailboxes/{address}").status_code == 200

    def test_remaining_seconds_counts_down(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        address = create_mailbox(client)
        clock.advance(timedelta(minutes=3))

        body = client.get(f"/api/mailboxes/{address}").json()
        assert body["remaining_seconds"] == 420

    def test_unknown_address_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/mailboxes/nobody@localhost.test")
        assert response.status_code == 404

    def test_expired_address_returns_410_gone(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        # 410 rather than 404: the resource *did* exist and was
        # deliberately removed. That tells a client not to retry, which a
        # bare 404 does not. This is exactly why MailboxExpiredError is a
        # distinct exception type from MailboxNotFoundError.
        address = create_mailbox(client)
        clock.advance(timedelta(minutes=10))

        response = client.get(f"/api/mailboxes/{address}")
        assert response.status_code == 410

    def test_error_response_has_a_detail_message(self, client: TestClient) -> None:
        body = client.get("/api/mailboxes/nobody@localhost.test").json()
        assert "detail" in body


# --------------------------------------------------------------------------- #
# POST /api/mailboxes/{address}/extend
# --------------------------------------------------------------------------- #


class TestExtendMailbox:
    def test_returns_200(self, client: TestClient) -> None:
        # 200, not 201: an existing resource was modified, nothing created.
        address = create_mailbox(client)
        response = client.post(f"/api/mailboxes/{address}/extend")
        assert response.status_code == 200

    def test_resets_window_to_full_ten_minutes(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        address = create_mailbox(client)
        clock.advance(timedelta(minutes=8))  # 2 minutes left

        body = client.post(f"/api/mailboxes/{address}/extend").json()
        assert body["remaining_seconds"] == 600

    def test_extension_never_stacks_beyond_ten_minutes(
        self, client: TestClient
    ) -> None:
        # The user-facing rule: the button tops you back up to ten, it does
        # not accumulate to twenty.
        address = create_mailbox(client)
        client.post(f"/api/mailboxes/{address}/extend")
        body = client.post(f"/api/mailboxes/{address}/extend").json()
        assert body["remaining_seconds"] == 600

    def test_keeps_the_same_address(self, client: TestClient) -> None:
        address = create_mailbox(client)
        body = client.post(f"/api/mailboxes/{address}/extend").json()
        assert body["address"] == address

    def test_unknown_address_returns_404(self, client: TestClient) -> None:
        response = client.post("/api/mailboxes/nobody@localhost.test/extend")
        assert response.status_code == 404

    def test_expired_address_returns_410(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        address = create_mailbox(client)
        clock.advance(timedelta(minutes=11))
        response = client.post(f"/api/mailboxes/{address}/extend")
        assert response.status_code == 410


# --------------------------------------------------------------------------- #
# GET /api/mailboxes/{address}/messages
# --------------------------------------------------------------------------- #


class TestListMessages:
    def test_new_mailbox_has_empty_inbox(self, client: TestClient) -> None:
        address = create_mailbox(client)
        response = client.get(f"/api/mailboxes/{address}/messages")
        assert response.status_code == 200
        assert response.json() == []

    def test_delivered_message_is_listed(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        address = create_mailbox(client)

        # Deliver through the service directly. SMTP is a separate delivery
        # mechanism and is tested on its own; here we only care that the
        # HTTP read path renders what the service holds.
        service = client.app.dependency_overrides[get_service]()  # type: ignore[attr-defined]
        clock.advance(timedelta(seconds=45))
        service.deliver_message(
            sender="alice@somewhere.io",
            recipient=address,
            subject="Your code",
            body="1234",
        )

        body = client.get(f"/api/mailboxes/{address}/messages").json()
        assert len(body) == 1
        assert body[0]["sender"] == "alice@somewhere.io"
        assert body[0]["subject"] == "Your code"
        assert body[0]["body"] == "1234"
        assert body[0]["received_at"] == "2026-01-01T12:00:45Z"

    def test_messages_are_listed_in_arrival_order(self, client: TestClient) -> None:
        address = create_mailbox(client)
        service = client.app.dependency_overrides[get_service]()  # type: ignore[attr-defined]
        for subject in ("first", "second", "third"):
            service.deliver_message(
                sender="a@x.io", recipient=address, subject=subject, body=""
            )

        body = client.get(f"/api/mailboxes/{address}/messages").json()
        assert [m["subject"] for m in body] == ["first", "second", "third"]

    def test_unknown_address_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/mailboxes/nobody@localhost.test/messages")
        assert response.status_code == 404

    def test_expired_address_returns_410(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        address = create_mailbox(client)
        clock.advance(timedelta(minutes=10))
        response = client.get(f"/api/mailboxes/{address}/messages")
        assert response.status_code == 410


# --------------------------------------------------------------------------- #
# Contract documentation
# --------------------------------------------------------------------------- #


class TestOpenApiContract:
    def test_all_mailbox_routes_are_documented(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/mailboxes" in paths
        assert "/api/mailboxes/{address}" in paths
        assert "/api/mailboxes/{address}/extend" in paths
        assert "/api/mailboxes/{address}/messages" in paths

    def test_documents_the_404_and_410_responses(self, client: TestClient) -> None:
        # Error codes belong in the published contract, not just in the
        # implementation -- a client author should not have to guess.
        spec = client.get("/openapi.json").json()
        responses = spec["paths"]["/api/mailboxes/{address}"]["get"]["responses"]
        assert "404" in responses
        assert "410" in responses
