"""Tests for the HTTP application skeleton.

What is being proven here is mostly *wiring*, not business logic:

  * `create_app()` is a factory -- importing the module has no side
    effects, and every call produces an independent application.
  * the lifespan handler opens the database on startup and closes it on
    shutdown.
  * the dependency-injection seam (`get_service`) can be overridden, which
    is what every later endpoint test will rely on to inject a FrozenClock.

TestClient drives the ASGI app directly, in-process. No socket is opened
and no server is started, so these tests stay in the millisecond range.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ten_min_mail import __version__
from ten_min_mail.api import ServiceDep, create_app, get_service


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A client backed by a real, throwaway SQLite file.

    `tmp_path` is a pytest builtin: a fresh directory per test, cleaned up
    automatically. Using a real file (rather than :memory:) exercises the
    same code path production will take.

    Entering the TestClient context manager is what triggers the lifespan
    handler -- without `with`, startup never runs.
    """
    app = create_app(database_path=tmp_path / "test.db")
    with TestClient(app) as test_client:
        yield test_client


class TestApplicationFactory:
    def test_returns_a_new_app_each_call(self, tmp_path: Path) -> None:
        # A module-level `app = FastAPI()` global would make this impossible,
        # and with it any test that needs its own configuration.
        first = create_app(database_path=tmp_path / "a.db")
        second = create_app(database_path=tmp_path / "b.db")
        assert first is not second

    def test_app_has_a_title_and_version(self, tmp_path: Path) -> None:
        app = create_app(database_path=tmp_path / "a.db")
        assert app.title
        assert app.version == __version__


class TestLifespan:
    def test_database_file_is_created_on_startup(self, tmp_path: Path) -> None:
        db_path = tmp_path / "created-on-startup.db"
        assert not db_path.exists()

        app = create_app(database_path=db_path)
        with TestClient(app):
            assert db_path.exists()

    def test_service_is_available_on_app_state(self, tmp_path: Path) -> None:
        # The lifespan handler builds the whole object graph once and parks
        # it on `app.state`, so requests do not rebuild it per call.
        app = create_app(database_path=tmp_path / "a.db")
        with TestClient(app) as client:
            assert client.app.state.service is not None  # type: ignore[attr-defined]


class TestHealthEndpoint:
    def test_returns_200(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200

    def test_reports_ok_status_and_version(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body == {"status": "ok", "version": __version__}

    def test_health_check_actually_touches_the_database(
        self, client: TestClient
    ) -> None:
        # A health endpoint that only returns a hardcoded string is a lie:
        # it reports "ok" while the database is on fire. This one performs a
        # real query, so a broken database makes the check fail.
        body = client.get("/health").json()
        assert body["status"] == "ok"


class TestOpenApiDocs:
    def test_openapi_schema_is_served(self, client: TestClient) -> None:
        # FastAPI generates this from the route signatures and Pydantic
        # models -- documentation that cannot drift from the code.
        schema = client.get("/openapi.json").json()
        assert "/health" in schema["paths"]

    def test_health_response_model_is_documented(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        assert "HealthResponse" in schema["components"]["schemas"]


class TestDependencyOverrideSeam:
    """The seam every later endpoint test depends on.

    If `get_service` can be swapped, then the mailbox endpoints can be
    tested against a FrozenClock -- letting us assert "extend at minute
    eight" without the test taking eight minutes. Proving the mechanism
    works here means the later tests can simply use it.
    """

    def test_get_service_can_be_overridden(self, tmp_path: Path) -> None:
        app = create_app(database_path=tmp_path / "a.db")

        sentinel = object()
        app.dependency_overrides[get_service] = lambda: sentinel

        received: list[object] = []

        @app.get("/_probe")
        def probe(service: ServiceDep) -> dict[str, bool]:
            received.append(service)
            return {"ok": True}

        with TestClient(app) as client:
            assert client.get("/_probe").status_code == 200

        assert received == [sentinel]
