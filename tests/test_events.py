"""Tests for the event-publishing seam.

The service must be able to announce "mail arrived for this address"
without knowing that anything is listening, let alone that the listener
is a WebSocket. That keeps the web layer out of the use-case layer and
keeps service tests free of WebSocket scaffolding.

This is the Observer pattern, and it is the last dependency-inversion
seam in the project.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ten_min_mail.domain import Message
from ten_min_mail.events import EventPublisher, MessageDelivered

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_message(recipient: str = "abc@localhost.test") -> Message:
    return Message(
        sender="alice@somewhere.io",
        recipient=recipient,
        subject="Hello",
        body="Hi there",
        received_at=T0,
    )


class TestEventPublisher:
    def test_publishing_with_no_subscribers_is_a_no_op(self) -> None:
        # A service running without a web layer attached must not crash.
        publisher = EventPublisher()
        publisher.publish(MessageDelivered(message=make_message()))

    def test_subscriber_receives_published_event(self) -> None:
        publisher = EventPublisher()
        received: list[MessageDelivered] = []
        publisher.subscribe(received.append)

        event = MessageDelivered(message=make_message())
        publisher.publish(event)

        assert received == [event]

    def test_all_subscribers_receive_the_event(self) -> None:
        publisher = EventPublisher()
        first: list[MessageDelivered] = []
        second: list[MessageDelivered] = []
        publisher.subscribe(first.append)
        publisher.subscribe(second.append)

        event = MessageDelivered(message=make_message())
        publisher.publish(event)

        assert first == [event]
        assert second == [event]

    def test_unsubscribe_stops_delivery(self) -> None:
        publisher = EventPublisher()
        received: list[MessageDelivered] = []
        publisher.subscribe(received.append)
        publisher.unsubscribe(received.append)

        publisher.publish(MessageDelivered(message=make_message()))

        assert received == []

    def test_unsubscribing_an_unknown_subscriber_is_harmless(self) -> None:
        # Teardown code should not have to check first.
        publisher = EventPublisher()
        publisher.unsubscribe(lambda event: None)

    def test_a_failing_subscriber_does_not_stop_the_others(self) -> None:
        # One broken listener must not silence every other listener --
        # a dropped WebSocket should not stop a log sink from recording.
        publisher = EventPublisher()
        reached: list[str] = []

        def explodes(event: MessageDelivered) -> None:
            raise RuntimeError("subscriber is broken")

        publisher.subscribe(explodes)
        publisher.subscribe(lambda event: reached.append("second"))

        publisher.publish(MessageDelivered(message=make_message()))

        assert reached == ["second"]

    def test_a_failing_subscriber_does_not_propagate_to_the_publisher(
        self,
    ) -> None:
        # Publishing is a side effect of delivering mail. If a listener
        # throws, the mail has still been stored successfully and the
        # caller must not see an error.
        publisher = EventPublisher()

        def explodes(event: MessageDelivered) -> None:
            raise RuntimeError("subscriber is broken")

        publisher.subscribe(explodes)
        publisher.publish(MessageDelivered(message=make_message()))


class TestMessageDeliveredEvent:
    def test_event_carries_the_message(self) -> None:
        message = make_message()
        event = MessageDelivered(message=message)
        assert event.message is message

    def test_event_exposes_the_recipient_for_routing(self) -> None:
        # Subscribers need to know which mailbox to notify without
        # reaching into the message themselves.
        event = MessageDelivered(message=make_message("box@localhost.test"))
        assert event.recipient == "box@localhost.test"

    def test_event_is_frozen(self) -> None:
        event = MessageDelivered(message=make_message())
        try:
            event.message = make_message()  # type: ignore[misc]
        except Exception:
            return
        raise AssertionError("event should be immutable")
