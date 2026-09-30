"""Tests for serving the web UI.

Scope, stated honestly: these cover the *serving* of the frontend --
that the files exist, are reachable, and do not shadow the API. They do
not cover the JavaScript's behaviour, because there is no browser in
this test stack. Pretending otherwise with assertions on file contents
would be theatre.

Testing the UI properly means Playwright or Selenium: a real browser,
real clicks, real WebSocket frames. That is a worthwhile next step and
deliberately out of scope here -- it would add a heavyweight dependency
and a second test runtime for a single page.

What these tests *do* protect is the mistake that is easy to make and
silent when made: mounting the static catch-all before the API routes,
so that `/api/mailboxes` starts returning index.html.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ten_min_mail.api import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(database_path=tmp_path / "static.db")
    with TestClient(app) as test_client:
        yield test_client


class TestStaticFiles:
    def test_root_serves_the_page(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_page_mentions_the_product(self, client: TestClient) -> None:
        assert "10 Minute Mail" in client.get("/").text

    def test_stylesheet_is_served(self, client: TestClient) -> None:
        response = client.get("/app.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]

    def test_script_is_served(self, client: TestClient) -> None:
        response = client.get("/app.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]

    def test_unknown_static_path_is_404(self, client: TestClient) -> None:
        assert client.get("/does-not-exist.png").status_code == 404


class TestApiIsNotShadowed:
    """The static mount must not swallow the API.

    A catch-all mounted at '/' matches everything. Registered in the
    wrong order it silently intercepts /api and /ws, and the symptom is
    an API that returns HTML -- which looks like a frontend bug and
    wastes an afternoon.
    """

    def test_api_routes_still_work(self, client: TestClient) -> None:
        response = client.post("/api/mailboxes")
        assert response.status_code == 201
        assert response.json()["address"].endswith("@localhost.test")

    def test_health_still_works(self, client: TestClient) -> None:
        assert client.get("/health").json()["status"] == "ok"

    def test_openapi_still_works(self, client: TestClient) -> None:
        assert "/api/mailboxes" in client.get("/openapi.json").json()["paths"]

    def test_docs_still_work(self, client: TestClient) -> None:
        assert client.get("/docs").status_code == 200

    def test_websocket_route_still_works(self, client: TestClient) -> None:
        address = client.post("/api/mailboxes").json()["address"]
        with client.websocket_connect(f"/ws/{address}") as socket:
            assert socket.receive_json()["type"] == "connected"


class TestPackaging:
    """The frontend must be inside the installed package.

    `[tool.setuptools.package-data]` ships static/* in the wheel.
    Without it setuptools installs only .py files, and the app imports,
    starts, and returns 404 for every page -- a failure that appears
    exclusively in a real install and never in editable-mode
    development, because an editable install reads the source tree.

    Asserting it here rather than in a CI job keeps it somewhere pytest
    runs it, ruff lints it and mypy checks it. A check that only exists
    in YAML is a check nobody maintains.
    """

    def test_static_files_live_inside_the_package(self) -> None:
        import ten_min_mail

        static_dir = Path(ten_min_mail.__file__).parent / "static"
        shipped = sorted(p.name for p in static_dir.iterdir())

        assert shipped == ["app.css", "app.js", "index.html"]

    def test_package_data_is_declared(self) -> None:
        # The assertion above passes in an editable install even when
        # package-data is missing, because the source tree is right
        # there. This one checks the declaration itself, which is what
        # actually governs the wheel.
        import tomllib

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))

        package_data = config["tool"]["setuptools"]["package-data"]
        assert "static/*" in package_data["ten_min_mail"]


class TestPageWiring:
    """Minimal checks that the HTML and JS refer to the same things.

    These are cheap guards against a rename in one file that is not
    mirrored in the other -- a class of bug a Python test suite can
    catch even without a browser.
    """

    def test_html_loads_the_script_and_stylesheet(self, client: TestClient) -> None:
        page = client.get("/").text
        assert "app.js" in page
        assert "app.css" in page

    @pytest.mark.parametrize(
        "element_id",
        [
            "address",
            "copy-button",
            "countdown",
            "extend-button",
            "inbox",
            "status",
        ],
    )
    def test_script_references_every_element_the_page_defines(
        self, client: TestClient, element_id: str
    ) -> None:
        page = client.get("/").text
        script = client.get("/app.js").text
        assert f'id="{element_id}"' in page, f"{element_id} missing from HTML"
        assert element_id in script, f"{element_id} unused by JS"
