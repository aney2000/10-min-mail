"""Live inbox delivery over WebSockets.

This module bridges two worlds:

  * the service layer, which publishes plain `MessageDelivered` events
    synchronously and knows nothing about sockets
  * Starlette's WebSocket API, which is async

`ConnectionRegistry` tracks who is watching what. `InboxBroadcaster`
subscribes to the event publisher and turns events into frames.

The async boundary
------------------
`EventPublisher.publish` is synchronous, because the service and the
SQLite driver under it are synchronous and making them async to emit an
event would be a large change for no benefit. WebSocket sends are async.

The broadcaster bridges the two by scheduling the send on the running
event loop rather than awaiting it. Delivery therefore returns
immediately and the frames go out on the next loop iteration -- which is
what we want anyway: an SMTP peer should not wait on a browser socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

from .domain import Message
from .events import EventPublisher, MessageDelivered

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #


class JsonSocket(Protocol):
    """The only thing the registry needs from a WebSocket.

    Narrow on purpose (Interface Segregation): the registry never
    accepts, closes, or reads from a socket, so depending on the full
    Starlette WebSocket type would make it untestable without one.
    """

    async def send_json(self, payload: dict[str, object]) -> None: ...


# --------------------------------------------------------------------------- #
# Connection registry
# --------------------------------------------------------------------------- #


class ConnectionRegistry:
    """Tracks which sockets are watching which mailbox.

    Separate from the endpoint that accepts connections and from the
    broadcaster that uses it, because "who is watching what" is a
    distinct responsibility with real behaviour of its own: several
    watchers per address, unpredictable disconnects, and broadcasts that
    must survive a dead socket.
    """

    def __init__(self) -> None:
        self._watchers: dict[str, list[JsonSocket]] = {}

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def add(self, address: str, socket: JsonSocket) -> None:
        """Register `socket` as a watcher of `address`."""
        self._watchers.setdefault(address, []).append(socket)

    def remove(self, address: str, socket: JsonSocket) -> None:
        """Deregister a watcher. Silent if it was never registered.

        Disconnect handling runs in a `finally` block, so this must not
        require the caller to know whether registration succeeded.
        """
        sockets = self._watchers.get(address)
        if sockets is None:
            return

        with contextlib.suppress(ValueError):
            sockets.remove(socket)

        # Drop the key once nobody is watching. Without this a
        # long-running server keeps one empty list per address it has
        # ever seen -- a slow leak that only shows up in production.
        if not sockets:
            del self._watchers[address]

    # ------------------------------------------------------------------ #
    # Broadcasting
    # ------------------------------------------------------------------ #

    async def broadcast(self, address: str, payload: dict[str, object]) -> None:
        """Send `payload` to every watcher of `address`.

        Sockets that fail are dropped: a socket that raised on send is
        gone, and keeping it means retrying a corpse on every future
        broadcast. One dead watcher never prevents the others from
        receiving the frame.
        """
        # Iterate a copy: sends can fail and mutate the list underneath us.
        for socket in list(self._watchers.get(address, ())):
            try:
                await socket.send_json(payload)
            except Exception:
                logger.debug("dropping dead websocket for %s", address)
                self.remove(address, socket)

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def connection_count(self, address: str) -> int:
        return len(self._watchers.get(address, ()))

    def tracked_addresses(self) -> list[str]:
        return list(self._watchers)


# --------------------------------------------------------------------------- #
# Payload shaping
# --------------------------------------------------------------------------- #


def message_frame(message: Message) -> dict[str, object]:
    """Render a message as the JSON frame clients receive.

    Deliberately mirrors `MessageResponse` from the REST API so the
    browser can use one rendering function for both the initial inbox
    fetch and subsequent live updates. The `type` field lets clients
    switch on frame kind as more are added.
    """
    return {
        "type": "message",
        "sender": message.sender,
        "subject": message.subject,
        "body": message.body,
        "received_at": message.received_at.isoformat().replace("+00:00", "Z"),
    }


# --------------------------------------------------------------------------- #
# Broadcaster
# --------------------------------------------------------------------------- #


class InboxBroadcaster:
    """Turns `MessageDelivered` events into WebSocket frames.

    Subscribes to the service's publisher at startup. The service stays
    unaware that WebSockets exist; this class is the only thing that
    knows both vocabularies.

    Crossing the thread boundary
    ----------------------------
    `EventPublisher.publish` is synchronous and is called from whichever
    thread happened to deliver the mail: the SMTP handler's thread,
    FastAPI's worker pool for sync endpoints, or plain test code. None
    of those run inside the event loop that owns the WebSockets.

    So the broadcaster cannot ask for "the running loop" at call time --
    in exactly the cases that matter there isn't one, and the frame would
    be dropped silently. Instead it is *bound* to the application's loop
    at startup and uses `run_coroutine_threadsafe`, the supported way to
    hand work to a loop from outside it.
    """

    def __init__(self, registry: ConnectionRegistry) -> None:
        self._registry = registry
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the loop that owns the WebSockets.

        Called once during application startup, from inside that loop.
        """
        self._loop = loop

    def __call__(self, event: MessageDelivered) -> None:
        """Handle an event. Sync signature, async work scheduled.

        Returns immediately without waiting for the frame to be written:
        an SMTP peer must not be kept waiting on a browser socket.

        With no loop bound there is nothing to schedule onto and nobody
        could be listening, so the frame is dropped. Raising here would
        fail a delivery whose message is already safely stored.
        """
        loop = self._loop
        if loop is None:
            logger.debug("broadcaster has no bound loop; dropping frame")
            return

        coroutine = self._registry.broadcast(
            event.recipient,
            message_frame(event.message),
        )

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            # Already on the loop thread: schedule directly. Calling
            # run_coroutine_threadsafe from inside its own loop would
            # deadlock if anyone ever waited on the returned future.
            loop.create_task(coroutine)
        else:
            # Off-thread: the supported hand-off. Fire and forget -- we
            # deliberately do not block the delivering thread on the
            # result.
            asyncio.run_coroutine_threadsafe(coroutine, loop)


def attach_broadcaster(
    publisher: EventPublisher, registry: ConnectionRegistry
) -> InboxBroadcaster:
    """Wire a broadcaster to a publisher and return it.

    Binds the currently running loop, so this must be called from inside
    the application's event loop (the lifespan handler does exactly
    that). Returned so the caller can unsubscribe it on shutdown.
    """
    broadcaster = InboxBroadcaster(registry)
    with contextlib.suppress(RuntimeError):
        broadcaster.bind_loop(asyncio.get_running_loop())
    publisher.subscribe(broadcaster)
    return broadcaster
