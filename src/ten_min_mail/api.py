"""HTTP layer: the FastAPI application.

This module is a *delivery mechanism*. It translates HTTP requests into
service calls and service results back into HTTP responses. It contains
no business rules -- those all live in `service.py` and `domain.py`, and
would be identical if we served the app over gRPC instead.

Three patterns are worth understanding here, because every later
endpoint builds on them.

1. Application factory
   `create_app()` is a function, not a module-level `app = FastAPI()`.
   A global app is constructed at import time, which means tests cannot
   configure it, importing the module has side effects, and only one
   configuration can exist per process. A factory fixes all three.

2. Lifespan
   An async context manager owns startup and shutdown. Everything before
   `yield` runs on boot, everything after runs on shutdown -- so the
   cleanup sits next to the setup it undoes and is hard to forget. This
   replaces the deprecated `@app.on_event("startup")` decorator.

3. Dependency injection with an override seam
   Routes ask for a `MailboxService` via `Depends(get_service)` rather
   than reaching for a global. In tests, `app.dependency_overrides` can
   swap that function for one returning a service wired to a FrozenClock,
   and the route code does not change at all.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .address_generator import RandomAddressGenerator
from .clock import SystemClock
from .domain import Mailbox, Message
from .repository import MailboxNotFoundError, SqliteMailboxRepository
from .service import MailboxExpiredError, MailboxService
from .websocket import ConnectionRegistry, attach_broadcaster

#: WebSocket close code for "your request was refused on policy grounds"
#: (RFC 6455 section 7.4.1). Used when a mailbox is unknown or expired.
_WS_CLOSE_POLICY_VIOLATION = 1008

#: Domain we hand out addresses on. `.test` is reserved by RFC 6761 for
#: testing, so it can never collide with a real internet domain.
DEFAULT_MAIL_DOMAIN = "localhost.test"


# --------------------------------------------------------------------------- #
# Response models
# --------------------------------------------------------------------------- #
# Declaring a Pydantic model buys three things from one definition:
# runtime validation of what we return, an OpenAPI schema entry, and a
# static type mypy can check against the route's annotation.


class HealthResponse(BaseModel):
    """Liveness/readiness payload."""

    status: str = Field(description="'ok' when the service can serve traffic.")
    version: str = Field(description="Running application version.")


class MailboxResponse(BaseModel):
    """Public view of a mailbox.

    Deliberately NOT the `Mailbox` domain object. Returning domain
    objects directly would make every internal field part of the public
    contract, so renaming a field would break clients, and it would leave
    nowhere to put derived values like `remaining_seconds` (which the
    client needs for its countdown but the domain does not store).

    The route's job is translation; this model is the target of it.
    """

    address: str = Field(description="The disposable email address.")
    expires_at: datetime = Field(description="UTC instant the mailbox dies.")
    remaining_seconds: int = Field(
        description="Seconds left before expiry. Never negative.",
    )

    @classmethod
    def from_domain(cls, mailbox: Mailbox, *, now: datetime) -> MailboxResponse:
        """Map a domain object to its wire representation."""
        return cls(
            address=mailbox.address,
            expires_at=mailbox.expires_at,
            remaining_seconds=mailbox.remaining_seconds(now=now),
        )


class MessageResponse(BaseModel):
    """Public view of a received email."""

    sender: str = Field(description="Envelope sender address.")
    subject: str = Field(description="Subject line; may be empty.")
    body: str = Field(description="Plain-text body; may be empty.")
    received_at: datetime = Field(
        description="UTC instant we accepted the message.",
    )

    @classmethod
    def from_domain(cls, message: Message) -> MessageResponse:
        return cls(
            sender=message.sender,
            subject=message.subject,
            body=message.body,
            received_at=message.received_at,
        )


class ErrorResponse(BaseModel):
    """Shape of every error body, so clients can parse failures uniformly."""

    detail: str = Field(description="Human-readable explanation.")


# --------------------------------------------------------------------------- #
# Dependency providers
# --------------------------------------------------------------------------- #


def get_service(request: Request) -> MailboxService:
    """Return the singleton service built during startup.

    Reading it off `request.app.state` (rather than a module global)
    is what keeps the app self-contained and overridable in tests.
    """
    service: MailboxService = request.app.state.service
    return service


#: The dependency, expressed as a reusable type alias.
#:
#: Writing `service: ServiceDep` in a route is equivalent to the older
#: `service: MailboxService = Depends(get_service)`, with two advantages:
#: the dependency is part of the *type* rather than a default value, and
#: it can be declared once and reused by every endpoint instead of
#: repeating the `Depends(...)` call. It also sidesteps ruff's B008
#: ("no function calls in argument defaults") honestly, rather than
#: suppressing the rule on every route.
ServiceDep = Annotated[MailboxService, Depends(get_service)]

#: A mailbox address taken from the URL path.
#:
#: Bounded in length so a pathological URL is rejected by the framework
#: before it ever reaches a database query. Email addresses contain '@'
#: and '.', both legal in a path segment, so no special encoding is
#: needed.
AddressParam = Annotated[
    str,
    PathParam(
        min_length=3,
        max_length=254,  # RFC 5321 maximum length of an email address
        description="The disposable mailbox address.",
    ),
]

#: Shared OpenAPI documentation for the two ways a mailbox lookup fails.
#: Declared once and reused, so the published contract cannot drift
#: between endpoints -- a client author should not have to guess which
#: errors an endpoint can return.
_NOT_FOUND_OR_GONE: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse, "description": "No such mailbox."},
    410: {"model": ErrorResponse, "description": "The mailbox has expired."},
}


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #


def create_app(
    *,
    database_path: Path | str,
    mail_domain: str = DEFAULT_MAIL_DOMAIN,
    service: MailboxService | None = None,
) -> FastAPI:
    """Build a fully wired application.

    Args:
        database_path: Where the SQLite file lives. Tests pass a
            throwaway path; production passes a mounted volume. Ignored
            when `service` is supplied.
        mail_domain: The domain generated addresses belong to.
        service: A pre-built service to use instead of constructing one.
            Tests pass a service on a FrozenClock so they can advance
            time. It must be supplied here rather than patched in
            afterwards: the WebSocket broadcaster is attached to the
            service's own event publisher during startup, so a service
            swapped in later would publish to a publisher nobody is
            listening to, and live updates would silently stop working.

    This function is the *composition root*: the single place where
    concrete implementations are chosen and wired together. Every other
    module receives its collaborators and never constructs them. Keeping
    construction in one place is what makes the rest of the codebase
    substitutable.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ---- startup ----------------------------------------------------
        # The registry of live WebSockets, and the broadcaster that turns
        # delivery events into frames. The service publishes events; only
        # the broadcaster knows those become WebSocket traffic.
        registry = ConnectionRegistry()
        connection: sqlite3.Connection | None = None

        if service is not None:
            active_service = service
        else:
            # check_same_thread=False: FastAPI serves sync endpoints from
            # a thread pool, so the connection is touched from several
            # threads. Safe here because SQLite serialises writes
            # internally and our transactions are short.
            connection = sqlite3.connect(str(database_path), check_same_thread=False)
            repository = SqliteMailboxRepository(connection)
            repository.create_schema()

            active_service = MailboxService(
                repository=repository,
                clock=SystemClock(),
                address_generator=RandomAddressGenerator(
                    domain=mail_domain,
                    # Production randomness must be unpredictable.
                    rng=random.Random(),
                    checker=repository,
                ),
            )

        # Attach to whichever service we ended up with, from inside the
        # running loop -- that is where the broadcaster captures the loop
        # it will later hand frames to from other threads.
        broadcaster = attach_broadcaster(active_service.events, registry)

        app.state.service = active_service
        app.state.connection = connection
        app.state.connections = registry

        yield  # ---- application runs ------------------------------------

        # ---- shutdown ---------------------------------------------------
        active_service.events.unsubscribe(broadcaster)
        if connection is not None:
            connection.close()

    app = FastAPI(
        title="10 Minute Mail",
        version=__version__,
        summary="Disposable email addresses that expire after ten minutes.",
        lifespan=lifespan,
    )

    _register_exception_handlers(app)
    _register_routes(app)
    return app


# --------------------------------------------------------------------------- #
# Exception handling
# --------------------------------------------------------------------------- #


def _register_exception_handlers(app: FastAPI) -> None:
    """Map domain exceptions to HTTP responses, once, in one place.

    The alternative -- a try/except block in every route -- duplicates
    the mapping at each endpoint and guarantees that the fifth endpoint
    forgets a case. Registering handlers centrally means routes contain
    only the happy path, and the error policy is stated exactly once.
    """

    @app.exception_handler(MailboxNotFoundError)
    async def _not_found(request: Request, exc: MailboxNotFoundError) -> JSONResponse:
        # 404: as far as anyone can tell, this address never existed.
        return JSONResponse(
            status_code=404,
            content={"detail": f"No mailbox for address {exc.args[0]!r}."},
        )

    @app.exception_handler(MailboxExpiredError)
    async def _expired(request: Request, exc: MailboxExpiredError) -> JSONResponse:
        # 410 Gone, not 404: the resource existed and was deliberately
        # retired. That distinction tells a client the address is dead
        # for good and retrying is pointless -- information a bare 404
        # cannot convey. It is the whole reason MailboxExpiredError is a
        # separate type from MailboxNotFoundError.
        return JSONResponse(
            status_code=410,
            content={
                "detail": (
                    f"Mailbox {exc.args[0]!r} has expired. "
                    "Create a new one; expired addresses are never revived."
                )
            },
        )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


def _register_routes(app: FastAPI) -> None:
    """Attach endpoints to the app.

    Split out from `create_app` so the factory stays readable as the
    route list grows.
    """

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health(service: ServiceDep) -> HealthResponse:
        """Report whether the service can actually serve traffic.

        This deliberately performs a real database query. A health check
        that returns a hardcoded "ok" is worse than none: it reports
        healthy while the database is unreachable, and monitoring built
        on it will stay silent during an outage.
        """
        service.purge_expired()  # cheap, and proves the DB is writable
        return HealthResponse(status="ok", version=__version__)

    # ------------------------------------------------------------------ #
    # Mailboxes
    # ------------------------------------------------------------------ #

    @app.post(
        "/api/mailboxes",
        response_model=MailboxResponse,
        status_code=201,
        tags=["mailboxes"],
        summary="Create a disposable mailbox",
    )
    def create_mailbox(service: ServiceDep) -> MailboxResponse:
        """Hand out a fresh address, valid for ten minutes.

        201 rather than 200: a new resource came into existence.
        """
        mailbox = service.create_mailbox()
        return MailboxResponse.from_domain(mailbox, now=service.now())

    @app.get(
        "/api/mailboxes/{address}",
        response_model=MailboxResponse,
        tags=["mailboxes"],
        summary="Read mailbox status",
        responses=_NOT_FOUND_OR_GONE,
    )
    def get_mailbox(service: ServiceDep, address: AddressParam) -> MailboxResponse:
        """Report how much life the mailbox has left."""
        mailbox = service.get_mailbox(address)
        return MailboxResponse.from_domain(mailbox, now=service.now())

    @app.post(
        "/api/mailboxes/{address}/extend",
        response_model=MailboxResponse,
        tags=["mailboxes"],
        summary="Reset the mailbox window to ten minutes",
        responses=_NOT_FOUND_OR_GONE,
    )
    def extend_mailbox(service: ServiceDep, address: AddressParam) -> MailboxResponse:
        """Top the mailbox back up to a full ten minutes.

        This resets the window rather than adding to it: ten minutes is
        the ceiling, so pressing the button twice in a row still leaves
        ten minutes, never twenty.

        200 rather than 201: an existing resource was modified, nothing
        new was created.
        """
        mailbox = service.extend_mailbox(address)
        return MailboxResponse.from_domain(mailbox, now=service.now())

    @app.get(
        "/api/mailboxes/{address}/messages",
        response_model=list[MessageResponse],
        tags=["messages"],
        summary="List received messages",
        responses=_NOT_FOUND_OR_GONE,
    )
    def list_messages(
        service: ServiceDep, address: AddressParam
    ) -> list[MessageResponse]:
        """Return the inbox, oldest message first."""
        messages = service.get_messages(address)
        return [MessageResponse.from_domain(m) for m in messages]

    # ------------------------------------------------------------------ #
    # Live inbox
    # ------------------------------------------------------------------ #

    @app.websocket("/ws/{address}")
    async def live_inbox(websocket: WebSocket, address: str) -> None:
        """Push new mail to a watching client as it arrives.

        Unlike HTTP, this connection stays open and the *server* speaks
        first when mail shows up -- which is what makes the inbox live
        without the browser polling.

        Note the dependency is read from `app.state` rather than through
        `Depends`: WebSocket routes cannot use HTTP exception handlers,
        so validation and error signalling are handled explicitly below.
        """
        service: MailboxService = websocket.app.state.service
        registry: ConnectionRegistry = websocket.app.state.connections

        # Validate BEFORE accepting. Refusing the handshake outright is
        # the honest signal for "this mailbox does not exist"; accepting
        # and then closing would look to the client like a connection
        # that worked and then broke.
        try:
            mailbox = service.get_mailbox(address)
        except (MailboxNotFoundError, MailboxExpiredError):
            await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION)
            return

        await websocket.accept()
        registry.add(address, websocket)

        try:
            # Greet with the remaining time so the client can start its
            # countdown immediately, without a second HTTP round-trip.
            await websocket.send_json(
                {
                    "type": "connected",
                    "address": mailbox.address,
                    "remaining_seconds": mailbox.remaining_seconds(now=service.now()),
                }
            )

            # We expect no client traffic; this await simply parks the
            # coroutine until the peer disconnects. Without it the
            # function would return and Starlette would close the socket
            # immediately.
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            # Always deregister. A leaked registration is a slow memory
            # leak and makes every later broadcast retry a dead socket.
            registry.remove(address, websocket)
