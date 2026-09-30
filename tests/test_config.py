"""Tests for environment-driven configuration.

Config comes from the environment rather than from code (the 12-factor
principle): the same container image then runs in development and in
production with nothing but environment variables differing, and no
settings file has to be baked into an image or committed to git.

Parsing is tested in isolation from the process environment -- the
loader takes a mapping rather than reading os.environ itself, so tests
never mutate global state and can run in parallel without interfering.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ten_min_mail.config import Settings, load_settings


class TestDefaults:
    def test_works_with_an_empty_environment(self) -> None:
        # A developer who has just cloned the repo should be able to run
        # the app with no setup at all.
        settings = load_settings({})
        assert isinstance(settings, Settings)

    def test_default_http_port_is_8000(self) -> None:
        assert load_settings({}).http_port == 8000

    def test_default_smtp_port_is_1025(self) -> None:
        # Not 25: ports below 1024 need root on Unix, and running a mail
        # server as root turns any bug in it into a host compromise.
        assert load_settings({}).smtp_port == 1025

    def test_default_mail_domain_is_reserved_for_testing(self) -> None:
        # RFC 6761 reserves .test, so generated addresses can never
        # collide with a real internet domain.
        assert load_settings({}).mail_domain == "localhost.test"

    def test_default_binds_loopback_only(self) -> None:
        # Defaulting to 0.0.0.0 would expose an unauthenticated mail
        # server to the whole network the moment someone runs it on a
        # laptop in a cafe. Containers override this deliberately.
        settings = load_settings({})
        assert settings.http_host == "127.0.0.1"
        assert settings.smtp_host == "127.0.0.1"


class TestOverrides:
    def test_reads_http_port(self) -> None:
        assert load_settings({"TMM_HTTP_PORT": "9000"}).http_port == 9000

    def test_reads_smtp_port(self) -> None:
        assert load_settings({"TMM_SMTP_PORT": "2525"}).smtp_port == 2525

    def test_reads_hosts(self) -> None:
        env = {"TMM_HTTP_HOST": "0.0.0.0", "TMM_SMTP_HOST": "0.0.0.0"}
        settings = load_settings(env)
        assert settings.http_host == "0.0.0.0"
        assert settings.smtp_host == "0.0.0.0"

    def test_reads_mail_domain(self) -> None:
        settings = load_settings({"TMM_MAIL_DOMAIN": "inbox.example.com"})
        assert settings.mail_domain == "inbox.example.com"

    def test_reads_database_path(self) -> None:
        # Compare as Path, not as a string: Path normalises separators
        # per platform, so asserting on "/data/mail.db" passes on Linux
        # and fails on Windows for a reason that has nothing to do with
        # the code under test.
        settings = load_settings({"TMM_DATABASE_PATH": "/data/mail.db"})
        assert settings.database_path == Path("/data/mail.db")

    def test_reads_sweep_interval(self) -> None:
        assert load_settings({"TMM_SWEEP_INTERVAL": "15"}).sweep_interval == 15.0

    def test_reads_log_level_case_insensitively(self) -> None:
        assert load_settings({"TMM_LOG_LEVEL": "debug"}).log_level == "DEBUG"


class TestValidation:
    """Bad configuration must fail at startup, loudly.

    A container that starts with a nonsensical port and then misbehaves
    is far harder to diagnose than one that refuses to start and says
    why. Validation belongs at the boundary where the value enters.
    """

    @pytest.mark.parametrize("value", ["", "not-a-number", "12.5", "eight"])
    def test_rejects_a_non_integer_port(self, value: str) -> None:
        with pytest.raises(ValueError, match="TMM_HTTP_PORT"):
            load_settings({"TMM_HTTP_PORT": value})

    @pytest.mark.parametrize("value", ["0", "-1", "65536", "99999"])
    def test_rejects_an_out_of_range_port(self, value: str) -> None:
        with pytest.raises(ValueError, match="TMM_HTTP_PORT"):
            load_settings({"TMM_HTTP_PORT": value})

    def test_rejects_a_non_positive_sweep_interval(self) -> None:
        # Zero would be a busy loop pinning a CPU core.
        with pytest.raises(ValueError, match="TMM_SWEEP_INTERVAL"):
            load_settings({"TMM_SWEEP_INTERVAL": "0"})

    def test_rejects_an_unknown_log_level(self) -> None:
        with pytest.raises(ValueError, match="TMM_LOG_LEVEL"):
            load_settings({"TMM_LOG_LEVEL": "VERBOSE"})

    def test_rejects_a_domain_without_a_dot(self) -> None:
        # Addresses on such a domain would fail the Mailbox validator at
        # creation time -- better to refuse at startup than to serve a
        # mailbox endpoint that always 500s.
        with pytest.raises(ValueError, match="TMM_MAIL_DOMAIN"):
            load_settings({"TMM_MAIL_DOMAIN": "localhost"})

    def test_error_message_names_the_variable(self) -> None:
        # The person reading this is looking at container logs with no
        # debugger. The message must say which variable and what it got.
        with pytest.raises(ValueError) as caught:
            load_settings({"TMM_SMTP_PORT": "banana"})

        message = str(caught.value)
        assert "TMM_SMTP_PORT" in message
        assert "banana" in message


class TestSettingsObject:
    def test_settings_are_frozen(self) -> None:
        # Configuration that can change at runtime is configuration you
        # cannot reason about from the logs.
        settings = load_settings({})
        with pytest.raises(Exception):
            settings.http_port = 1234  # type: ignore[misc]
