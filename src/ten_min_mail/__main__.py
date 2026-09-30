"""Console entry point: `python -m ten_min_mail`.

Starts the HTTP API, the SMTP receiver and the expiry sweeper as one
process, configured entirely from the environment.

Why a module rather than a long uvicorn command
-----------------------------------------------
`uvicorn ten_min_mail.api:create_app --factory --port ...` works, but
it cannot pass the SMTP settings the factory needs, so SMTP silently
stays off. One command that starts the whole application is also what
a container image needs as its CMD -- and what a person needs when
they come back to the project in six months.
"""

from __future__ import annotations

import logging

import uvicorn

from .api import create_app
from .config import Settings, load_settings


def build_uvicorn_config(settings: Settings) -> uvicorn.Config:
    """Assemble the server configuration from resolved settings.

    Separated from `main` so it can be inspected in a test without
    starting a server and binding real ports.
    """
    app = create_app(
        database_path=settings.database_path,
        mail_domain=settings.mail_domain,
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        sweep_interval=settings.sweep_interval,
    )

    return uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level=settings.log_level.lower(),
        # Access logs are noise for a service whose traffic is one page
        # and a WebSocket; the application logs what matters.
        access_log=False,
    )


def main() -> None:  # pragma: no cover
    """Load configuration, announce it, and run until interrupted.

    Excluded from coverage: this function blocks forever by design, so
    a unit test can only call it by starting a subprocess -- at which
    point the coverage tool, running in the parent, sees nothing.

    It is still verified, just not by this measurement. Everything up
    to binding a port lives in `build_uvicorn_config`, which is tested
    directly, and the Docker verification starts the real process and
    exercises the API and SMTP through it. The uncovered lines here are
    logging calls and one `.run()`.
    """
    settings = load_settings()

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logger = logging.getLogger("ten_min_mail")

    # Log the effective configuration at startup. When something is
    # misconfigured in a container, this line is usually the whole
    # diagnosis -- and it costs one log entry.
    logger.info("10 Minute Mail starting")
    logger.info("  HTTP      http://%s:%s", settings.http_host, settings.http_port)
    logger.info("  SMTP      %s:%s", settings.smtp_host, settings.smtp_port)
    logger.info("  domain    %s", settings.mail_domain)
    logger.info("  database  %s", settings.database_path)

    uvicorn.Server(build_uvicorn_config(settings)).run()


if __name__ == "__main__":  # pragma: no cover
    main()
