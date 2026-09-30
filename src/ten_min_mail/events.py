"""Domain events and the publisher that fans them out.

Why this module exists
----------------------
The service must be able to announce "mail arrived" without knowing who
is listening. If `deliver_message` reached into a WebSocket registry
directly, the use-case layer would import web-layer code, every service
test would need WebSocket scaffolding, and adding a second listener
(a log sink, a metrics counter) would mean editing the service again.

Instead the service publishes an event. Whether that becomes a
WebSocket frame, a log line, or nothing at all is not its concern.
This is the Observer pattern, and the last dependency-inversion seam
in the project.

Failure policy
--------------
Publishing happens *after* the mail is safely stored. A listener that
throws must therefore not fail the delivery, and must not prevent the
remaining listeners from running: a dropped browser socket should never
stop a log sink from recording, nor make an SMTP peer think delivery
failed. Exceptions are swallowed and logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .domain import Message

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MessageDelivered:
    """Raised when a message has been stored for a mailbox.

    Immutable: subscribers receive the same instance, and one of them
    mutating it would corrupt the view of every other.
    """

    message: Message

    @property
    def recipient(self) -> str:
        """Which mailbox to notify.

        Exposed on the event so subscribers can route without reaching
        into the message payload themselves.
        """
        return self.message.recipient


#: A subscriber is any callable taking an event. Deliberately a plain
#: function type rather than an interface: listeners are usually
#: one-liners, and `list.append` should be a valid subscriber in tests.
Subscriber = Callable[[MessageDelivered], None]


# --------------------------------------------------------------------------- #
# Publisher
# --------------------------------------------------------------------------- #


class EventPublisher:
    """Fans events out to registered subscribers.

    Synchronous by design. Subscribers that need to do async work (such
    as writing to a WebSocket) schedule it on the running event loop
    themselves -- see `websocket.py`. Keeping the publisher sync means
    the service layer, and the sync SQLite driver underneath it, do not
    have to become async to emit an event.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: Subscriber) -> None:
        """Remove a subscriber. Silent if it was never registered.

        Teardown code should not have to check first -- an unsubscribe
        that can raise turns every cleanup path into a try/except.
        """
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    def publish(self, event: MessageDelivered) -> None:
        """Deliver `event` to every subscriber.

        Iterates over a copy so a subscriber may unsubscribe itself
        during dispatch (a dead WebSocket does exactly this) without
        mutating the list mid-iteration.
        """
        for subscriber in list(self._subscribers):
            try:
                subscriber(event)
            except Exception:
                # The mail is already stored; a broken listener must not
                # fail the delivery or silence the other listeners.
                logger.exception(
                    "event subscriber failed for recipient %s", event.recipient
                )
