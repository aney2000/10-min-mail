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
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field

from . import __version__
from .address_generator import RandomAddressGenerator
from .clock import SystemClock
from .repository import SqliteMailboxRepository
from .service import MailboxService

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


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #


def create_app(
    *,
    database_path: Path | str,
    mail_domain: str = DEFAULT_MAIL_DOMAIN,
) -> FastAPI:
    """Build a fully wired application.

    Args:
        database_path: Where the SQLite file lives. Tests pass a
            throwaway path; production passes a mounted volume.
        mail_domain: The domain generated addresses belong to.

    This function is the *composition root*: the single place where
    concrete implementations are chosen and wired together. Every other
    module receives its collaborators and never constructs them. Keeping
    construction in one place is what makes the rest of the codebase
    substitutable.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ---- startup ----------------------------------------------------
        # check_same_thread=False: FastAPI serves sync endpoints from a
        # thread pool, so the connection is touched from several threads.
        # Safe here because SQLite serialises writes internally and our
        # transactions are short.
        connection = sqlite3.connect(str(database_path), check_same_thread=False)

        repository = SqliteMailboxRepository(connection)
        repository.create_schema()

        app.state.service = MailboxService(
            repository=repository,
            clock=SystemClock(),
            address_generator=RandomAddressGenerator(
                domain=mail_domain,
                # No seed: production randomness must be unpredictable.
                rng=random.Random(),
                checker=repository,
            ),
        )
        app.state.connection = connection

        yield  # ---- application runs ------------------------------------

        # ---- shutdown ---------------------------------------------------
        connection.close()

    app = FastAPI(
        title="10 Minute Mail",
        version=__version__,
        summary="Disposable email addresses that expire after ten minutes.",
        lifespan=lifespan,
    )

    _register_routes(app)
    return app


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
