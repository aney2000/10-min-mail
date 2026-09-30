"""Environment-driven configuration.

Config comes from the environment rather than from code -- the
12-factor principle. One container image then runs in development and
in production with nothing but environment variables differing, and no
settings file is baked into an image or committed to git.

`load_settings` takes a mapping instead of reading `os.environ`
directly. That is the same injection habit used for the clock and the
random source: tests pass a dict, never mutate global state, and can
run in parallel without interfering with each other.

Every value is validated here, at the boundary where it enters the
program. A container that starts with a nonsensical port and then
misbehaves is far harder to diagnose than one that refuses to start and
says exactly which variable was wrong -- the person reading the failure
has container logs and no debugger.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved application configuration.

    Frozen: configuration that can change at runtime is configuration
    you cannot reason about from a log line.
    """

    http_host: str
    http_port: int
    smtp_host: str
    smtp_port: int
    mail_domain: str
    database_path: Path
    sweep_interval: float
    log_level: str


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from an environment mapping.

    Defaults to `os.environ`, but accepts any mapping so tests need not
    touch the real environment.

    Defaults are chosen for a developer who has just cloned the repo and
    wants to run the thing: everything works with no variables set.
    Notably both listeners bind loopback only. Defaulting to 0.0.0.0
    would expose an unauthenticated mail server to the entire network
    the moment someone starts it on a laptop in a cafe; the container
    overrides it deliberately, where the exposure is the operator's
    explicit choice.
    """
    source = os.environ if env is None else env

    return Settings(
        http_host=source.get("TMM_HTTP_HOST", "127.0.0.1"),
        http_port=_read_port(source, "TMM_HTTP_PORT", default=8000),
        smtp_host=source.get("TMM_SMTP_HOST", "127.0.0.1"),
        # 1025, not 25: ports below 1024 require root on Unix, and
        # running a mail server as root turns any bug in it into a host
        # compromise. Docker maps 25 -> 1025 outside the process if the
        # standard port is wanted.
        smtp_port=_read_port(source, "TMM_SMTP_PORT", default=1025),
        mail_domain=_read_domain(source, "TMM_MAIL_DOMAIN"),
        database_path=Path(source.get("TMM_DATABASE_PATH", "ten_min_mail.db")),
        sweep_interval=_read_positive_float(source, "TMM_SWEEP_INTERVAL", default=60.0),
        log_level=_read_log_level(source, "TMM_LOG_LEVEL"),
    )


# --------------------------------------------------------------------------- #
# Readers -- each validates one value and reports the variable by name
# --------------------------------------------------------------------------- #


def _read_port(source: Mapping[str, str], name: str, *, default: int) -> int:
    raw = source.get(name)
    if raw is None:
        return default

    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None

    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535, got {port}")
    return port


def _read_positive_float(
    source: Mapping[str, str], name: str, *, default: float
) -> float:
    raw = source.get(name)
    if raw is None:
        return default

    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None

    if value <= 0:
        # Zero would be a busy loop pinning a CPU core.
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _read_log_level(source: Mapping[str, str], name: str) -> str:
    level = source.get(name, "INFO").upper()
    if level not in _VALID_LOG_LEVELS:
        valid = ", ".join(sorted(_VALID_LOG_LEVELS))
        raise ValueError(f"{name} must be one of {valid}; got {level!r}")
    return level


def _read_domain(source: Mapping[str, str], name: str) -> str:
    domain = source.get(name, "localhost.test")
    if "." not in domain.strip("."):
        # Addresses on a dotless domain fail the Mailbox validator, so
        # the mailbox endpoint would 500 on every request. Refusing to
        # start is far kinder than serving a permanently broken API.
        raise ValueError(
            f"{name} must contain a dot (e.g. 'localhost.test'); got {domain!r}"
        )
    return domain
