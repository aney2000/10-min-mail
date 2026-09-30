"""Turn raw RFC 5322 bytes into a subject and a plain-text body.

Scope and non-goals
-------------------
Pure logic: bytes in, two strings out. No sockets, no database, no
framework -- which is why it can be tested exhaustively in
milliseconds, and why the SMTP handler that uses it stays thin.

We extract *text only*. HTML is stripped rather than stored as markup:
the inbox renders message bodies in a browser, and storing raw HTML
from an untrusted sender would be a cross-site-scripting hole. Choosing
not to support rich rendering is a security decision, not a shortcut.

Why this is harder than it looks
--------------------------------
Real email is messy in ways the tutorials skip:

  * Non-ASCII headers are encoded per RFC 2047 ('=?utf-8?B?...?='), so
    reading the raw header shows the user base64 rather than text.
  * Most real mail is multipart: the same content as plain text and as
    HTML, sometimes nested inside another multipart for attachments.
  * Charsets other than UTF-8 are still in use; assuming UTF-8 yields
    mojibake.
  * Every header is optional, including Subject.
  * Malformed encoding is routine. A mail server that raises on bad
    input is a mail server that can be killed by one crafted message.

Python's stdlib `email` package handles most of this correctly, but
only if you ask it the right way -- and it still raises on inputs that
occur in the wild, so every extraction below is defensive.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import Message

logger = logging.getLogger(__name__)

#: Maximum characters we keep of a message body.
#:
#: Unbounded bodies are a denial-of-service vector: one message could
#: otherwise fill the disk and lock up the browser tab rendering it.
#: 64k of text is far more than any human reads in a disposable inbox.
DEFAULT_MAX_BODY_CHARS = 64_000

#: Appended when we cut a body short. Silently dropping content is worse
#: than dropping it loudly -- the reader needs to know they are not
#: seeing everything.
_TRUNCATION_NOTICE = "\n\n[... message truncated ...]"


@dataclass(frozen=True, slots=True)
class ParsedMail:
    """The two fields we keep from a raw message."""

    subject: str
    body: str


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def parse_message(
    raw: bytes,
    *,
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
) -> ParsedMail:
    """Extract a subject and plain-text body from raw message bytes.

    Never raises. A mail server that can be crashed by a malformed
    message is a mail server that can be taken down by anyone who can
    reach port 25, so every failure mode degrades to empty or
    placeholder text and is logged instead.
    """
    try:
        message = message_from_bytes(raw, policy=policy.default)
    except Exception:
        logger.exception("could not parse message; storing it as empty")
        return ParsedMail(subject="", body="")

    return ParsedMail(
        subject=_extract_subject(message),
        body=_truncate(_extract_body(message), max_body_chars),
    )


# --------------------------------------------------------------------------- #
# Subject
# --------------------------------------------------------------------------- #


def _extract_subject(message: Message) -> str:
    """Decode the Subject header into displayable text.

    Handles RFC 2047 encoded words, headers that interleave encoded and
    literal text, and folded (multi-line) headers.
    """
    raw_subject = message.get("Subject")
    if raw_subject is None:
        return ""

    try:
        # make_header(decode_header(...)) joins the mix of encoded words
        # and literal fragments a header may contain. str() then yields
        # the fully decoded text.
        decoded = str(make_header(decode_header(str(raw_subject))))
    except Exception:
        # A broken encoded word should cost us the decoding, not the
        # message. Fall back to the raw header text.
        logger.debug("undecodable Subject header; using it raw")
        decoded = str(raw_subject)

    # Folded headers arrive with embedded newlines; collapse all runs of
    # whitespace so the value is the single logical line it represents.
    return re.sub(r"\s+", " ", decoded).strip()


# --------------------------------------------------------------------------- #
# Body
# --------------------------------------------------------------------------- #


def _extract_body(message: Message) -> str:
    """Return the best available plain-text rendering of the body.

    Preference order:
      1. a text/plain part (what the sender wrote for text readers)
      2. a text/html part, with tags stripped
      3. empty string

    Attachments are skipped: their decoded bytes are not body text, and
    dumping a base64 PDF into the inbox helps nobody.
    """
    if not message.is_multipart():
        return _render_single_part(message)

    html_fallback = ""

    # walk() traverses nested multiparts, which is what real clients
    # produce: multipart/mixed wrapping multipart/alternative.
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue  # container, not content
        if _is_attachment(part):
            continue

        content_type = part.get_content_type()
        if content_type == "text/plain":
            text = _decode_part(part)
            if text.strip():
                return text  # first real text part wins
        elif content_type == "text/html" and not html_fallback:
            html_fallback = _decode_part(part)

    # Only reached when there was no usable text/plain part. HTML-only
    # mail is common from marketing systems; stripped text beats an
    # empty inbox entry.
    return _strip_html(html_fallback) if html_fallback else ""


def _render_single_part(message: Message) -> str:
    text = _decode_part(message)
    if message.get_content_type() == "text/html":
        return _strip_html(text)
    return text


def _is_attachment(part: Message) -> bool:
    disposition = str(part.get("Content-Disposition", ""))
    return "attachment" in disposition.lower()


def _decode_part(part: Message) -> str:
    """Decode one part's payload to text, honouring its declared charset.

    `get_payload(decode=True)` undoes the transfer encoding (base64,
    quoted-printable). The charset then converts bytes to characters.
    Both steps can fail on real-world mail, so both are guarded.
    """
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        logger.debug("could not decode transfer encoding for a part")
        return ""

    if payload is None:
        # Some parts carry a string payload directly.
        content = part.get_payload()
        return content if isinstance(content, str) else ""

    if not isinstance(payload, bytes):  # defensive; stdlib types are loose
        return str(payload)

    charset = part.get_content_charset() or "utf-8"
    try:
        # errors='replace': undecodable bytes become U+FFFD rather than
        # raising. One malformed message must not break delivery.
        return payload.decode(charset, errors="replace")
    except LookupError:
        # The sender declared a charset Python has never heard of.
        logger.debug("unknown charset %r; falling back to utf-8", charset)
        return payload.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# HTML handling
# --------------------------------------------------------------------------- #

# Script and style content is removed entirely -- it is code, not text,
# and their contents would otherwise survive tag stripping as visible
# gibberish.
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_BLOCK_BREAK_RE = re.compile(
    r"</(p|div|h[1-6]|li|tr)>|<br\s*/?>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RUN_RE = re.compile(r"[ \t]{2,}")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _strip_html(markup: str) -> str:
    """Reduce HTML to readable plain text.

    Deliberately regex-based rather than a full parser: we are not
    rendering this HTML, only salvaging its text, and adding a parser
    dependency to display a disposable inbox is not a trade worth
    making. The output is plain text by construction, so the usual
    "never parse HTML with regex" objection -- that you will mis-handle
    nesting -- does not apply: worst case a stray character survives as
    literal text, which is harmless.
    """
    if not markup:
        return ""

    text = _SCRIPT_STYLE_RE.sub("", markup)
    # Turn block boundaries into newlines before deleting tags, so the
    # result keeps some of the original structure.
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)

    # Entities must be unescaped *after* tag removal, so that an escaped
    # '&lt;script&gt;' in the source cannot become a real tag afterwards.
    text = html.unescape(text)
    text = _WHITESPACE_RUN_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# Size limiting
# --------------------------------------------------------------------------- #


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_NOTICE


__all__ = ["DEFAULT_MAX_BODY_CHARS", "ParsedMail", "parse_message"]
