"""SMTP receiver: accepts real email for our disposable mailboxes.

How SMTP works, briefly
-----------------------
SMTP is a plain-text conversation over TCP::

    C: EHLO sender.org
    C: MAIL FROM:<alice@sender.org>     <- envelope sender
    C: RCPT TO:<bob@localhost.test>     <- envelope recipient
    S: 250 OK              ... or ...   S: 550 No such user
    C: DATA
    C: Subject: Hello
    C:
    C: the body
    C: .                                <- lone dot ends the message
    S: 250 Message accepted

Envelope versus headers
-----------------------
MAIL FROM / RCPT TO are the *envelope* -- the addressing the server
actually routes on. The From: and To: lines inside DATA are just text
in the letter and may say anything at all; that is how mailing lists
work, and how spoofing works. We route on the envelope and record the
envelope sender, never the From: header.

Why rejection happens at RCPT
-----------------------------
The alternative -- accept every recipient, then silently drop the
message at DATA if the mailbox does not exist -- makes the sending
server believe delivery succeeded. The mail disappears and nobody is
told. Replying 550 during RCPT means the sender learns immediately and
can bounce to its user, which is why a mistyped address comes back to
you in seconds rather than vanishing.

Rejecting early also means never receiving a body we intend to discard:
cheaper, and less surface for abuse.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiosmtpd.smtp import SMTP

from .mail_parsing import parse_message
from .repository import MailboxNotFoundError
from .service import MailboxExpiredError, MailboxService

logger = logging.getLogger(__name__)

#: Shown in the 220 greeting. Identifying the software is conventional
#: and makes debugging with `telnet` or `swaks` far less confusing.
_SMTP_BANNER = "10 Minute Mail SMTP"

# SMTP reply codes we use. Spelled out because the numbers alone are
# opaque to anyone who has not memorised RFC 5321.
_ACCEPTED = "250 OK"
_MESSAGE_ACCEPTED = "250 Message accepted for delivery"
_NO_SUCH_USER = "550 No such user here"
_MAILBOX_EXPIRED = "550 Mailbox has expired"
_NOT_OUR_DOMAIN = "550 Relaying denied; not a local domain"


class MailboxSmtpHandler:
    """aiosmtpd handler that stores mail in our mailboxes.

    Deliberately thin. Parsing lives in `mail_parsing`, storage and
    business rules live in `MailboxService`; this class only translates
    between the SMTP conversation and those two. That is the same
    delivery-mechanism role `api.py` plays for HTTP, which is why both
    can drive the identical service.
    """

    def __init__(self, service: MailboxService, *, mail_domain: str) -> None:
        self._service = service
        # Stored lowercase: domain comparison is case-insensitive per
        # RFC 5321, and normalising once here avoids repeating .lower()
        # at every comparison site.
        self._domain = mail_domain.lower()

    # ------------------------------------------------------------------ #
    # RCPT TO
    # ------------------------------------------------------------------ #

    async def handle_RCPT(  # uppercase name required by aiosmtpd
        self,
        server: Any,
        session: Any,
        envelope: Any,
        address: str,
        rcpt_options: list[str],
    ) -> str:
        """Decide whether we will accept mail for `address`.

        Returning a 5xx string here rejects the recipient in-band, which
        is the honest signal. Appending to `envelope.rcpt_tos` is how
        aiosmtpd records an accepted recipient.
        """
        normalised = _normalise_address(address)

        if not normalised.endswith(f"@{self._domain}"):
            # We are not an open relay. Accepting mail for domains we do
            # not host is how a server ends up on every spam blocklist.
            logger.info("refused relay attempt for %s", normalised)
            return _NOT_OUR_DOMAIN

        try:
            self._service.get_mailbox(normalised)
        except MailboxNotFoundError:
            logger.info("rejected mail for unknown mailbox %s", normalised)
            return _NO_SUCH_USER
        except MailboxExpiredError:
            logger.info("rejected mail for expired mailbox %s", normalised)
            return _MAILBOX_EXPIRED

        envelope.rcpt_tos.append(normalised)
        return _ACCEPTED

    # ------------------------------------------------------------------ #
    # DATA
    # ------------------------------------------------------------------ #

    async def handle_DATA(  # uppercase name required by aiosmtpd
        self,
        server: Any,
        session: Any,
        envelope: Any,
    ) -> str:
        """Store the received message for every accepted recipient."""
        parsed = parse_message(_as_bytes(envelope.content))
        sender = _normalise_address(envelope.mail_from or "")

        for recipient in envelope.rcpt_tos:
            try:
                self._service.deliver_message(
                    sender=sender,
                    recipient=recipient,
                    subject=parsed.subject,
                    body=parsed.body,
                )
            except (MailboxNotFoundError, MailboxExpiredError):
                # The mailbox was alive at RCPT and died before DATA
                # finished -- rare, but a slow sender makes it real.
                # Nothing to deliver to; the other recipients still get
                # their copy.
                logger.info("mailbox %s vanished before delivery", recipient)
            except Exception:
                # One failing recipient must not cost the others their
                # copy, and must not turn into a 5xx that makes the
                # sender retry a message we may already have stored.
                logger.exception("failed to store message for %s", recipient)

        return _MESSAGE_ACCEPTED


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _normalise_address(address: str) -> str:
    """Reduce an SMTP address to the form we store and compare.

    Strips the angle brackets some clients leave on, and lowercases.
    RFC 5321 makes the domain case-insensitive and leaves the local part
    up to the receiving server; we generate lowercase addresses, so a
    mailbox that could not be reached because a sender uppercased it
    would simply be a broken mailbox.
    """
    return address.strip().strip("<>").strip().lower()


def _as_bytes(content: bytes | str | None) -> bytes:
    """Coerce envelope content to bytes.

    aiosmtpd hands over bytes in the configuration we use, but str in
    others. Coercing costs one line; crashing on a type we could have
    handled costs a message.
    """
    if content is None:
        return b""
    if isinstance(content, bytes):
        return content
    return content.encode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #


class SmtpServer:
    """Runs the SMTP listener on the caller's asyncio event loop.

    Why not aiosmtpd's `Controller`
    -------------------------------
    `Controller` starts its own thread with its own event loop, which is
    the right answer for a standalone script. We already have a loop --
    FastAPI's -- and running on it directly means the SMTP handler, the
    HTTP endpoints, and the WebSocket broadcaster all share one loop and
    one service instance. No cross-loop hand-off, no second scheduler.

    Ports below 1024 need root on Unix. Running a mail server as root so
    it can bind port 25 turns any bug in it into a full system
    compromise, so the default here is 1025 and real deployments
    forward 25 -> 1025 outside the process.
    """

    def __init__(
        self,
        *,
        service: MailboxService,
        mail_domain: str,
        host: str = "127.0.0.1",
        port: int = 1025,
    ) -> None:
        self.host = host
        self.port = port
        self._handler = MailboxSmtpHandler(service, mail_domain=mail_domain)
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        """Bind the port and begin accepting connections."""
        loop = asyncio.get_running_loop()
        self._server = await loop.create_server(
            lambda: SMTP(self._handler, ident=_SMTP_BANNER),
            host=self.host,
            port=self.port,
        )
        logger.info("SMTP server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        """Stop accepting and release the port.

        `wait_closed` matters: without it the socket can linger and the
        next bind fails with "address already in use", which turns a
        clean restart into a mystery.
        """
        if self._server is None:
            return

        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("SMTP server stopped")
