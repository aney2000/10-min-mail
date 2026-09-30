"""Tests for the WebSocket connection registry.

Tracking which sockets are watching which mailbox is its own
responsibility, separate from the endpoint that accepts them and from
the service that produces events. It has real behaviour worth testing on
its own: many watchers per address, unpredictable disconnects, and
broadcasts that must survive a dead socket.

The registry is tested against a fake socket rather than a real one --
it only ever needs something with `send_json`, so requiring a live
WebSocket here would test Starlette rather than our logic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ten_min_mail.domain import Message
from ten_min_mail.events import MessageDelivered
from ten_min_mail.websocket import ConnectionRegistry, InboxBroadcaster

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_message(recipient: str = "a@localhost.test") -> Message:
    return Message(
        sender="alice@somewhere.io",
        recipient=recipient,
        subject="Hello",
        body="Hi there",
        received_at=T0,
    )


class FakeSocket:
    """Minimal stand-in for a WebSocket: records what was sent to it."""

    def __init__(self, *, fails: bool = False) -> None:
        self.sent: list[dict[str, object]] = []
        self._fails = fails

    async def send_json(self, payload: dict[str, object]) -> None:
        if self._fails:
            raise ConnectionError("socket is dead")
        self.sent.append(payload)


class TestRegistration:
    def test_starts_empty(self) -> None:
        registry = ConnectionRegistry()
        assert registry.connection_count("a@localhost.test") == 0

    def test_add_registers_a_watcher(self) -> None:
        registry = ConnectionRegistry()
        registry.add("a@localhost.test", FakeSocket())
        assert registry.connection_count("a@localhost.test") == 1

    def test_several_sockets_can_watch_one_address(self) -> None:
        # Two browser tabs on the same mailbox is normal, not an error.
        registry = ConnectionRegistry()
        registry.add("a@localhost.test", FakeSocket())
        registry.add("a@localhost.test", FakeSocket())
        assert registry.connection_count("a@localhost.test") == 2

    def test_addresses_are_tracked_independently(self) -> None:
        registry = ConnectionRegistry()
        registry.add("a@localhost.test", FakeSocket())
        registry.add("b@localhost.test", FakeSocket())
        assert registry.connection_count("a@localhost.test") == 1
        assert registry.connection_count("b@localhost.test") == 1

    def test_remove_deregisters_a_watcher(self) -> None:
        registry = ConnectionRegistry()
        socket = FakeSocket()
        registry.add("a@localhost.test", socket)
        registry.remove("a@localhost.test", socket)
        assert registry.connection_count("a@localhost.test") == 0

    def test_removing_an_unknown_socket_is_harmless(self) -> None:
        # Disconnect handling runs in a finally block; it must not need
        # to check whether registration ever succeeded.
        registry = ConnectionRegistry()
        registry.remove("a@localhost.test", FakeSocket())

    def test_empty_address_entries_are_cleaned_up(self) -> None:
        # Without this, a long-running server accumulates one empty list
        # per address it has ever seen -- a slow memory leak.
        registry = ConnectionRegistry()
        socket = FakeSocket()
        registry.add("a@localhost.test", socket)
        registry.remove("a@localhost.test", socket)
        assert registry.tracked_addresses() == []


class TestBroadcast:
    async def test_sends_payload_to_the_watcher(self) -> None:
        registry = ConnectionRegistry()
        socket = FakeSocket()
        registry.add("a@localhost.test", socket)

        await registry.broadcast("a@localhost.test", {"type": "ping"})

        assert socket.sent == [{"type": "ping"}]

    async def test_sends_to_every_watcher_of_that_address(self) -> None:
        registry = ConnectionRegistry()
        first, second = FakeSocket(), FakeSocket()
        registry.add("a@localhost.test", first)
        registry.add("a@localhost.test", second)

        await registry.broadcast("a@localhost.test", {"type": "ping"})

        assert first.sent == [{"type": "ping"}]
        assert second.sent == [{"type": "ping"}]

    async def test_does_not_send_to_watchers_of_other_addresses(self) -> None:
        # Leaking one mailbox's mail to another mailbox's watcher would
        # be a privacy breach, not merely a bug.
        registry = ConnectionRegistry()
        watcher = FakeSocket()
        eavesdropper = FakeSocket()
        registry.add("a@localhost.test", watcher)
        registry.add("b@localhost.test", eavesdropper)

        await registry.broadcast("a@localhost.test", {"secret": True})

        assert watcher.sent == [{"secret": True}]
        assert eavesdropper.sent == []

    async def test_broadcast_to_unwatched_address_is_a_no_op(self) -> None:
        # Mail often arrives for a mailbox nobody has open.
        registry = ConnectionRegistry()
        await registry.broadcast("nobody@localhost.test", {"type": "ping"})

    async def test_dead_socket_does_not_block_the_others(self) -> None:
        registry = ConnectionRegistry()
        dead = FakeSocket(fails=True)
        alive = FakeSocket()
        registry.add("a@localhost.test", dead)
        registry.add("a@localhost.test", alive)

        await registry.broadcast("a@localhost.test", {"type": "ping"})

        assert alive.sent == [{"type": "ping"}]

    async def test_dead_socket_is_dropped_from_the_registry(self) -> None:
        # A socket that fails a send is gone. Keeping it means retrying
        # a corpse on every future broadcast.
        registry = ConnectionRegistry()
        dead = FakeSocket(fails=True)
        registry.add("a@localhost.test", dead)

        await registry.broadcast("a@localhost.test", {"type": "ping"})

        assert registry.connection_count("a@localhost.test") == 0


class TestBroadcasterThreadBridge:
    """The broadcaster is called from whatever thread delivered the mail.

    `EventPublisher.publish` is synchronous and gets called from the SMTP
    handler, from FastAPI's sync-endpoint thread pool, and from plain
    test code -- none of which run inside the event loop that owns the
    WebSockets. Scheduling with `asyncio.get_running_loop()` therefore
    fails in exactly the situations that matter, and the frame is
    silently dropped.

    These tests pin the bridge: the broadcaster must remember the loop
    it was attached to and hand work to it across the thread boundary.
    """

    async def test_broadcasts_when_called_from_another_thread(self) -> None:
        import asyncio

        registry = ConnectionRegistry()
        socket = FakeSocket()
        registry.add("a@localhost.test", socket)

        broadcaster = InboxBroadcaster(registry)
        broadcaster.bind_loop(asyncio.get_running_loop())

        event = MessageDelivered(message=make_message("a@localhost.test"))

        # Deliver from a worker thread, as SMTP and sync endpoints do.
        await asyncio.to_thread(broadcaster, event)
        await asyncio.sleep(0.05)  # let the scheduled send run

        assert len(socket.sent) == 1
        assert socket.sent[0]["subject"] == "Hello"

    async def test_broadcasts_when_called_on_the_loop_thread(self) -> None:
        import asyncio

        registry = ConnectionRegistry()
        socket = FakeSocket()
        registry.add("a@localhost.test", socket)

        broadcaster = InboxBroadcaster(registry)
        broadcaster.bind_loop(asyncio.get_running_loop())

        broadcaster(MessageDelivered(message=make_message("a@localhost.test")))
        await asyncio.sleep(0.05)

        assert len(socket.sent) == 1

    def test_unbound_broadcaster_drops_the_frame_quietly(self) -> None:
        # With no loop bound there is nothing to schedule onto and nobody
        # could be listening. Dropping the frame is correct; raising would
        # fail a delivery that already succeeded.
        registry = ConnectionRegistry()
        registry.add("a@localhost.test", FakeSocket())

        broadcaster = InboxBroadcaster(registry)
        broadcaster(MessageDelivered(message=make_message("a@localhost.test")))


class TestIntrospection:
    def test_tracked_addresses_lists_watched_mailboxes(self) -> None:
        registry = ConnectionRegistry()
        registry.add("a@localhost.test", FakeSocket())
        registry.add("b@localhost.test", FakeSocket())
        assert sorted(registry.tracked_addresses()) == [
            "a@localhost.test",
            "b@localhost.test",
        ]

    @pytest.mark.parametrize("count", [1, 3, 5])
    def test_connection_count_is_accurate(self, count: int) -> None:
        registry = ConnectionRegistry()
        for _ in range(count):
            registry.add("a@localhost.test", FakeSocket())
        assert registry.connection_count("a@localhost.test") == count
