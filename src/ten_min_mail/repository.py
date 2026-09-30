"""SQLite-backed persistence for mailboxes and messages.

Why raw sqlite3, not an ORM?
----------------------------
For a project this size, raw SQL is:
  - transparent -- you see exactly what runs
  - zero-dependency (sqlite3 ships with Python)
  - a genuine learning opportunity in SQL and transactions

If the schema grows or we need query composition, we introduce an ORM
*then*. Adding complexity we do not yet need is a form of technical debt
too. This is a real design choice; document it and move on.

Datetime storage
----------------
SQLite has no native datetime type. We serialise to ISO-8601 (with
timezone) and parse back on read. All datetimes in the domain are
timezone-aware UTC, so round-tripping is lossless.

Concurrency
-----------
The connection is created outside this class and passed in. FastAPI's
startup handler will own its lifecycle. `check_same_thread=False` is
required when the same connection is shared across async tasks -- which
is safe here because SQLite serialises writes internally and our
transactions are short.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .domain import Mailbox, Message

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class MailboxNotFoundError(LookupError):
    """No mailbox exists for the given address."""


class MailboxAlreadyExistsError(ValueError):
    """A mailbox with this address is already stored."""


# --------------------------------------------------------------------------- #
# SQL (kept as module-level constants so they read like a schema file)
# --------------------------------------------------------------------------- #

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mailboxes (
    address     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient     TEXT NOT NULL,
    sender        TEXT NOT NULL,
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    FOREIGN KEY(recipient) REFERENCES mailboxes(address) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient);
"""


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #


class SqliteMailboxRepository:
    """CRUD for mailboxes and their messages, backed by a SQLite connection.

    Satisfies the `AddressAvailabilityChecker` Protocol structurally by
    providing an `is_taken(address)` method -- no explicit inheritance,
    so the address_generator module has zero coupling to this one.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        # Foreign keys are OFF by default in SQLite. Turn them on so the
        # ON DELETE CASCADE actually cascades.
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Row factory: rows behave like dicts, safer than integer indexing.
        self._conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #

    def create_schema(self) -> None:
        """Idempotent: safe to call at every application startup."""
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    # ------------------------------------------------------------------ #
    # Mailbox operations
    # ------------------------------------------------------------------ #

    def is_taken(self, address: str) -> bool:
        """Structural conformance with AddressAvailabilityChecker."""
        row = self._conn.execute(
            "SELECT 1 FROM mailboxes WHERE address = ? LIMIT 1",
            (address,),
        ).fetchone()
        return row is not None

    def add(self, mailbox: Mailbox) -> None:
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO mailboxes(address, created_at, expires_at)"
                    " VALUES (?, ?, ?)",
                    (
                        mailbox.address,
                        _dt_to_iso(mailbox.created_at),
                        _dt_to_iso(mailbox.expires_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            # The PRIMARY KEY constraint gives us free uniqueness enforcement;
            # translate the storage exception into a domain-shaped one so
            # callers never have to know we use SQLite.
            raise MailboxAlreadyExistsError(mailbox.address) from exc

    def get(self, address: str) -> Mailbox:
        row = self._conn.execute(
            "SELECT address, created_at, expires_at FROM mailboxes WHERE address = ?",
            (address,),
        ).fetchone()
        if row is None:
            raise MailboxNotFoundError(address)
        return _row_to_mailbox(row)

    def replace(self, mailbox: Mailbox) -> None:
        """Overwrite an existing mailbox's fields (used for extension)."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE mailboxes SET created_at = ?, expires_at = ? WHERE address = ?",
                (
                    _dt_to_iso(mailbox.created_at),
                    _dt_to_iso(mailbox.expires_at),
                    mailbox.address,
                ),
            )
            if cursor.rowcount == 0:
                raise MailboxNotFoundError(mailbox.address)

    def delete(self, address: str) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM mailboxes WHERE address = ?",
                (address,),
            )
            if cursor.rowcount == 0:
                raise MailboxNotFoundError(address)

    def delete_expired(self, now: datetime) -> int:
        """Remove every mailbox whose expires_at is <= now.

        Returns the number of mailboxes removed. Messages cascade
        automatically via the FOREIGN KEY constraint.
        """
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM mailboxes WHERE expires_at <= ?",
                (_dt_to_iso(now),),
            )
            return cursor.rowcount

    # ------------------------------------------------------------------ #
    # Message operations
    # ------------------------------------------------------------------ #

    def add_message(self, message: Message) -> None:
        if not self.is_taken(message.recipient):
            # Fail loudly and early instead of relying on a FK error at
            # commit time -- the caller gets a domain-shaped error.
            raise MailboxNotFoundError(message.recipient)

        with self._conn:
            self._conn.execute(
                "INSERT INTO messages"
                "(recipient, sender, subject, body, received_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    message.recipient,
                    message.sender,
                    message.subject,
                    message.body,
                    _dt_to_iso(message.received_at),
                ),
            )

    def list_messages(self, address: str) -> list[Message]:
        rows = self._conn.execute(
            "SELECT sender, recipient, subject, body, received_at"
            " FROM messages WHERE recipient = ?"
            " ORDER BY id ASC",
            (address,),
        ).fetchall()
        return [_row_to_message(r) for r in rows]


# --------------------------------------------------------------------------- #
# Private conversions (single source of truth for (de)serialisation)
# --------------------------------------------------------------------------- #


def _dt_to_iso(dt: datetime) -> str:
    # isoformat() preserves tzinfo, which we require to be UTC-aware.
    return dt.isoformat()


def _iso_to_dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _row_to_mailbox(row: sqlite3.Row) -> Mailbox:
    return Mailbox(
        address=row["address"],
        created_at=_iso_to_dt(row["created_at"]),
        expires_at=_iso_to_dt(row["expires_at"]),
    )


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        sender=row["sender"],
        recipient=row["recipient"],
        subject=row["subject"],
        body=row["body"],
        received_at=_iso_to_dt(row["received_at"]),
    )
