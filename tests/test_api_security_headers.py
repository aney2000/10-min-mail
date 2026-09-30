"""Tests for the security response headers.

The inbox renders text that arrived from a stranger over SMTP. Two
defences already exist -- the backend strips HTML before storing, and
the frontend inserts everything with textContent -- and these headers
are the third, independent layer.

Layering matters because the first two are code we maintain and could
change. A Content-Security-Policy is enforced by the browser regardless
of what our JavaScript does, so it holds even if someone later
"simplifies" an innerHTML back in.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ten_min_mail.api import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(database_path=tmp_path / "headers.db")
    with TestClient(app) as test_client:
        yield test_client


class TestContentSecurityPolicy:
    def test_csp_is_present(self, client: TestClient) -> None:
        assert "content-security-policy" in client.get("/").headers

    def test_default_src_is_self(self, client: TestClient) -> None:
        # Nothing loads from another origin. An injected
        # <script src="//evil.com/x.js"> is refused by the browser even
        # if it somehow reached the DOM.
        csp = client.get("/").headers["content-security-policy"]
        assert "default-src 'self'" in csp

    def test_inline_script_is_not_allowed(self, client: TestClient) -> None:
        # The single most valuable directive here. Our own JS lives in
        # app.js, so we never need 'unsafe-inline' -- and without it,
        # an injected <script>...</script> simply does not execute.
        csp = client.get("/").headers["content-security-policy"]
        assert "'unsafe-inline'" not in _directive(csp, "script-src")

    def test_objects_are_blocked(self, client: TestClient) -> None:
        # <object>/<embed> are legacy plugin vectors with no use here.
        csp = client.get("/").headers["content-security-policy"]
        assert "object-src 'none'" in csp

    def test_framing_is_blocked_by_csp(self, client: TestClient) -> None:
        # Stops the inbox being embedded in an attacker's page and
        # clickjacked.
        csp = client.get("/").headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp

    def test_websocket_connections_are_allowed(self, client: TestClient) -> None:
        # A policy that blocks our own WebSocket would break the live
        # inbox -- and the failure appears only in a browser, never in
        # the Python tests. Worth pinning.
        csp = client.get("/").headers["content-security-policy"]
        connect = _directive(csp, "connect-src")
        assert "'self'" in connect
        assert "ws:" in connect or "wss:" in connect


class TestOtherSecurityHeaders:
    def test_nosniff_is_set(self, client: TestClient) -> None:
        # Stops a browser guessing that a text/plain response is really
        # HTML and rendering it.
        headers = client.get("/").headers
        assert headers["x-content-type-options"] == "nosniff"

    def test_frame_options_denies_embedding(self, client: TestClient) -> None:
        # Redundant with frame-ancestors for modern browsers, kept for
        # older ones that do not implement it.
        assert client.get("/").headers["x-frame-options"] == "DENY"

    def test_referrer_policy_does_not_leak_the_address(
        self, client: TestClient
    ) -> None:
        # The mailbox address is the only secret in this system, and it
        # appears in URLs. Without a referrer policy, clicking any
        # outbound link would send that URL to the destination site.
        policy = client.get("/").headers["referrer-policy"]
        assert policy in {"no-referrer", "same-origin", "strict-origin"}

    def test_server_header_does_not_advertise_the_stack(
        self, client: TestClient
    ) -> None:
        # Not a vulnerability, but free reconnaissance for an attacker
        # looking for a known bug in a specific version.
        assert "uvicorn" not in client.get("/").headers.get("server", "").lower()


class TestHeadersApplyEverywhere:
    """A policy applied to one route is not a policy."""

    @pytest.mark.parametrize("path", ["/", "/app.js", "/app.css", "/health"])
    def test_headers_are_on_every_response(self, client: TestClient, path: str) -> None:
        headers = client.get(path).headers
        assert "content-security-policy" in headers
        assert headers["x-content-type-options"] == "nosniff"

    def test_headers_are_on_api_responses(self, client: TestClient) -> None:
        response = client.post("/api/mailboxes")
        assert response.status_code == 201
        assert "content-security-policy" in response.headers

    def test_headers_are_on_error_responses(self, client: TestClient) -> None:
        # Error pages render attacker-influenced content too (the
        # requested address is echoed in the detail message), so they
        # need the policy just as much as successful ones.
        response = client.get("/api/mailboxes/nobody@localhost.test")
        assert response.status_code == 404
        assert "content-security-policy" in response.headers


class TestNothingIsBroken:
    """The policy must not break the application it protects."""

    def test_the_page_still_loads(self, client: TestClient) -> None:
        assert client.get("/").status_code == 200

    def test_the_api_still_works(self, client: TestClient) -> None:
        address = client.post("/api/mailboxes").json()["address"]
        assert client.get(f"/api/mailboxes/{address}").status_code == 200

    def test_the_websocket_still_connects(self, client: TestClient) -> None:
        address = client.post("/api/mailboxes").json()["address"]
        with client.websocket_connect(f"/ws/{address}") as socket:
            assert socket.receive_json()["type"] == "connected"


def _directive(csp: str, name: str) -> str:
    """Return one directive from a CSP string, or '' if absent."""
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith(f"{name} "):
            return part
    return ""
