"""Use-case layer: what the application actually *does*.

The service composes the four building blocks -- domain rules, storage,
clock, address generation -- into operations a user cares about:

    create_mailbox()    -- hand out a fresh disposable address
    get_mailbox()       -- look one up (refusing expired ones)
    extend_mailbox()    -- reset the validity window to a full 10 minutes
    deliver_message()   -- accept an incoming email
    get_messages()      -- read the inbox
    purge_expired()     -- housekeeping sweep

What this module deliberately does NOT know about
-------------------------------------------------
HTTP, JSON, WebSockets, SMTP, FastAPI. Those are *delivery mechanisms*.
Keeping them out means the same service instance serves both the web API
and the SMTP receiver, and every rule below is testable without starting
a server or opening a socket.

All collaborators are injected through the constructor. Nothing here
constructs its own dependencies, reads the system clock, or touches
global state.
"""

from __future__ import annotations

from .address_generator import RandomAddressGenerator
from .clock import Clock
from .domain import MAX_LIFETIME, Mailbox, Message
from .repository import SqliteMailboxRepository

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class MailboxExpiredError(LookupError):
    """The mailbox exists in storage but its validity window has closed.

    Subclasses LookupError (like MailboxNotFoundError) because from the
    caller's point of view an expired mailbox is effectively absent --
    but we keep it a distinct type so the HTTP layer can return a more
    informative status/message ("this mailbox expired") rather than a
    bare 404.
    """


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


class MailboxService:
    """Orchestrates mailbox lifecycle operations."""

    def __init__(
        self,
        *,
        repository: SqliteMailboxRepository,
        clock: Clock,
        address_generator: RandomAddressGenerator,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._generator = address_generator

    # ------------------------------------------------------------------ #
    # Creation
    # ------------------------------------------------------------------ #

    def create_mailbox(self) -> Mailbox:
        """Generate a free address and open a full 10-minute window on it."""
        now = self._clock.now()
        mailbox = Mailbox(
            address=self._generator.generate(),
            window_started_at=now,
            expires_at=now + MAX_LIFETIME,
        )
        self._repository.add(mailbox)
        return mailbox

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #

    def get_mailbox(self, address: str) -> Mailbox:
        """Return a live mailbox.

        Raises MailboxNotFoundError if it never existed, or
        MailboxExpiredError if its window has closed. Expired rows may
        linger in storage until the next purge; callers must never see
        them, so the check happens here rather than relying on the sweep.
        """
        return self._require_live_mailbox(address)

    def get_messages(self, address: str) -> list[Message]:
        """Return the inbox, oldest first. Refuses expired mailboxes."""
        self._require_live_mailbox(address)
        return self._repository.list_messages(address)

    # ------------------------------------------------------------------ #
    # Extension -- the core business rule
    # ------------------------------------------------------------------ #

    def extend_mailbox(self, address: str) -> Mailbox:
        """Reset the mailbox's validity window to a full MAX_LIFETIME.

        Semantics (as specified): the '+10 minutes' button does not
        *accumulate* time, it *resets* the window. A mailbox with two
        minutes left goes back to ten; a mailbox with nine minutes left
        also goes to ten. The remaining time is therefore capped at ten
        minutes no matter how often the button is pressed.

        Expiry is final: an already-expired mailbox cannot be revived,
        because its address may since have been recycled or its contents
        purged. Callers get MailboxExpiredError.
        """
        mailbox = self._require_live_mailbox(address)
        now = self._clock.now()

        # Mailbox is immutable, so extension means building a new value.
        # Moving `window_started_at` forward alongside `expires_at` keeps
        # the domain's `expires_at - window_started_at <= MAX_LIFETIME`
        # invariant satisfied -- and is precisely why that field is not
        # called `created_at`.
        extended = Mailbox(
            address=mailbox.address,
            window_started_at=now,
            expires_at=now + MAX_LIFETIME,
        )
        self._repository.replace(extended)
        return extended

    # ------------------------------------------------------------------ #
    # Delivery
    # ------------------------------------------------------------------ #

    def deliver_message(
        self,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
    ) -> Message:
        """Accept an incoming email for a live mailbox.

        The arrival timestamp comes from our clock, not from the caller.
        An SMTP peer must not be able to backdate or postdate mail by
        sending us a crafted Date header -- the receiving system is the
        authority on when something arrived.
        """
        self._require_live_mailbox(recipient)

        message = Message(
            sender=sender,
            recipient=recipient,
            subject=subject,
            body=body,
            received_at=self._clock.now(),
        )
        self._repository.add_message(message)
        return message

    # ------------------------------------------------------------------ #
    # Housekeeping
    # ------------------------------------------------------------------ #

    def purge_expired(self) -> int:
        """Delete every mailbox whose window has closed.

        Returns how many were removed. Messages cascade away with their
        mailbox via the schema's foreign key. Intended to be called
        periodically by a background task.
        """
        return self._repository.delete_expired(now=self._clock.now())

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _require_live_mailbox(self, address: str) -> Mailbox:
        """Fetch a mailbox, rejecting it if the window has already closed.

        Every public read/write path funnels through here, so the expiry
        rule is enforced in exactly one place. If it were duplicated in
        each method, one of them would eventually forget.
        """
        mailbox = self._repository.get(address)  # raises MailboxNotFoundError
        if mailbox.is_expired(now=self._clock.now()):
            raise MailboxExpiredError(address)
        return mailbox
