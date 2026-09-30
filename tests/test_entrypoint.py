"""Tests for the console entry point.

`main()` itself blocks forever, so what is tested here is everything up
to the point of binding a port: that settings reach the app factory,
and that the resulting server configuration matches what was asked
for. Splitting `build_uvicorn_config` out of `main` is what makes that
possible without a running server -- the same reason the clock and the
environment are injected elsewhere.
"""

from __future__ import annotations

from pathlib import Path

from ten_min_mail.__main__ import build_uvicorn_config
from ten_min_mail.config import load_settings


class TestUvicornConfig:
    def test_uses_the_configured_http_host_and_port(self, tmp_path: Path) -> None:
        settings = load_settings(
            {
                "TMM_HTTP_HOST": "0.0.0.0",
                "TMM_HTTP_PORT": "9123",
                "TMM_DATABASE_PATH": str(tmp_path / "x.db"),
            }
        )

        config = build_uvicorn_config(settings)

        assert config.host == "0.0.0.0"
        assert config.port == 9123

    def test_passes_the_log_level_through(self, tmp_path: Path) -> None:
        settings = load_settings(
            {
                "TMM_LOG_LEVEL": "DEBUG",
                "TMM_DATABASE_PATH": str(tmp_path / "x.db"),
            }
        )

        assert build_uvicorn_config(settings).log_level == "debug"

    def test_builds_an_app(self, tmp_path: Path) -> None:
        settings = load_settings({"TMM_DATABASE_PATH": str(tmp_path / "x.db")})
        config = build_uvicorn_config(settings)
        assert config.app is not None

    def test_access_log_is_disabled(self, tmp_path: Path) -> None:
        # One page and a WebSocket: per-request access logs are noise
        # that buries the application's own messages.
        settings = load_settings({"TMM_DATABASE_PATH": str(tmp_path / "x.db")})
        assert build_uvicorn_config(settings).access_log is False


class TestSmtpIsActuallyEnabled:
    """The entry point exists largely to switch SMTP on.

    Running `uvicorn ... --factory` cannot pass smtp_host, so SMTP stays
    silently off -- a mail server that accepts no mail. This is the test
    that would catch that regression.
    """

    def test_smtp_settings_reach_the_application(self, tmp_path: Path) -> None:
        import socket

        from fastapi.testclient import TestClient

        # Ask the OS for a free port rather than passing 0. Port 0 is
        # the "pick one for me" convention at the socket layer, but the
        # config validator rejects it -- correctly, because a port of 0
        # in a container's environment is a configuration mistake, not
        # a request. Working around one's own validator in a test is a
        # sign the test is wrong, not the validator.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        settings = load_settings(
            {
                "TMM_SMTP_HOST": "127.0.0.1",
                "TMM_SMTP_PORT": str(port),
                "TMM_DATABASE_PATH": str(tmp_path / "x.db"),
            }
        )
        config = build_uvicorn_config(settings)

        with TestClient(config.app) as client:  # type: ignore[arg-type]
            assert client.app.state.smtp_server is not None  # type: ignore[attr-defined]
