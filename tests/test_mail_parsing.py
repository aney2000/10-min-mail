"""Tests for turning raw RFC 5322 bytes into a subject and a body.

This is pure logic -- bytes in, two strings out -- so it is tested
without any socket, server, or database. Real-world email is far messier
than the tutorials suggest, and nearly every test below corresponds to a
class of message that actually arrives in practice:

  * non-ASCII subjects are encoded (RFC 2047), not sent as raw UTF-8
  * most real mail is multipart: the same content as plain text AND HTML
  * charsets other than UTF-8 still exist
  * headers are optional; a message with no Subject is perfectly legal
  * malformed encoding is common and must not crash the mail server

Getting this wrong means the inbox shows mojibake, base64 soup, or
nothing at all -- so each case is pinned by a test.
"""

from __future__ import annotations

from ten_min_mail.mail_parsing import parse_message


def build(headers: str, body: str = "") -> bytes:
    """Assemble a raw message the way a real client would send it.

    A blank line separates headers from body -- that separator *is* the
    structure of an email.
    """
    return f"{headers}\r\n\r\n{body}".encode()


# --------------------------------------------------------------------------- #
# Subject
# --------------------------------------------------------------------------- #


class TestSubject:
    def test_reads_a_plain_subject(self) -> None:
        parsed = parse_message(build("Subject: Hello there"))
        assert parsed.subject == "Hello there"

    def test_missing_subject_becomes_empty_string(self) -> None:
        # Legal, and common for automated mail. Must not be None: the UI
        # would then have to special-case it everywhere.
        parsed = parse_message(build("From: a@b.io"))
        assert parsed.subject == ""

    def test_decodes_rfc2047_base64_subject(self) -> None:
        # Non-ASCII subjects travel encoded. Showing the raw header would
        # display '=?utf-8?B?...?=' to the user.
        parsed = parse_message(build("Subject: =?utf-8?B?SGVsbG8gd29ybGQ=?="))
        assert parsed.subject == "Hello world"

    def test_decodes_rfc2047_quoted_printable_subject(self) -> None:
        parsed = parse_message(build("Subject: =?utf-8?Q?Caf=C3=A9?="))
        assert parsed.subject == "Café"

    def test_decodes_mixed_encoded_and_plain_subject(self) -> None:
        # A header can interleave encoded words and literal text.
        raw = build("Subject: Re: =?utf-8?B?SGVsbG8=?= (urgent)")
        assert parse_message(raw).subject == "Re: Hello (urgent)"

    def test_folded_subject_is_unfolded(self) -> None:
        # Long headers are wrapped across lines with leading whitespace.
        # The value is the joined text, not two separate lines.
        raw = build("Subject: This is a very long subject\r\n that was folded")
        assert parse_message(raw).subject == (
            "This is a very long subject that was folded"
        )

    def test_subject_whitespace_is_trimmed(self) -> None:
        assert parse_message(build("Subject:    padded   ")).subject == "padded"


# --------------------------------------------------------------------------- #
# Plain-text bodies
# --------------------------------------------------------------------------- #


class TestPlainTextBody:
    def test_reads_a_simple_body(self) -> None:
        parsed = parse_message(build("Subject: x", "Hello, world!"))
        assert parsed.body == "Hello, world!"

    def test_missing_body_becomes_empty_string(self) -> None:
        assert parse_message(build("Subject: x")).body == ""

    def test_multiline_body_is_preserved(self) -> None:
        parsed = parse_message(build("Subject: x", "line one\r\nline two"))
        assert "line one" in parsed.body
        assert "line two" in parsed.body

    def test_decodes_quoted_printable_body(self) -> None:
        raw = build(
            "Subject: x\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Transfer-Encoding: quoted-printable",
            "Caf=C3=A9 au lait",
        )
        assert parse_message(raw).body == "Café au lait"

    def test_decodes_base64_body(self) -> None:
        raw = build(
            "Subject: x\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Transfer-Encoding: base64",
            "SGVsbG8gZnJvbSBiYXNlNjQ=",
        )
        assert parse_message(raw).body == "Hello from base64"

    def test_crlf_line_endings_are_normalised_to_lf(self) -> None:
        # SMTP mandates CRLF on the wire, so every body arrives with
        # '\r\n'. Storing the carriage returns leaks a wire-protocol
        # detail into the UI, where browsers render them as stray
        # characters and text comparisons stop matching. Normalise once,
        # here, rather than making every consumer strip them.
        raw = build("Subject: x", "line one\r\nline two\r\nline three")
        assert parse_message(raw).body == "line one\nline two\nline three"

    def test_lone_carriage_returns_are_normalised_too(self) -> None:
        # Classic-Mac line endings still turn up in mail from old
        # systems and badly written scripts.
        raw = build("Subject: x", "line one\rline two")
        assert parse_message(raw).body == "line one\nline two"

    def test_trailing_whitespace_is_trimmed_from_the_body(self) -> None:
        # Senders routinely append a trailing newline; showing an empty
        # line at the end of every message is noise.
        raw = build("Subject: x", "the content\r\n\r\n")
        assert parse_message(raw).body == "the content"

    def test_honours_a_non_utf8_charset(self) -> None:
        # latin-1 is still out there. Decoding it as UTF-8 yields mojibake.
        raw = (
            b"Subject: x\r\nContent-Type: text/plain; charset=iso-8859-1\r\n\r\nCaf\xe9"
        )
        assert parse_message(raw).body == "Café"


# --------------------------------------------------------------------------- #
# Multipart
# --------------------------------------------------------------------------- #


class TestMultipart:
    def test_prefers_the_plain_text_alternative(self) -> None:
        # The overwhelmingly common real-world shape: the same message as
        # both text and HTML. We want the text part.
        raw = build(
            'Subject: x\r\nContent-Type: multipart/alternative; boundary="BOUND"',
            "--BOUND\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "The plain version.\r\n"
            "--BOUND\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><body><p>The HTML version.</p></body></html>\r\n"
            "--BOUND--\r\n",
        )
        parsed = parse_message(raw)
        assert "The plain version." in parsed.body
        assert "HTML version" not in parsed.body

    def test_falls_back_to_html_when_there_is_no_text_part(self) -> None:
        # HTML-only mail is common from marketing systems. Better to show
        # stripped text than an empty inbox entry.
        raw = build(
            'Subject: x\r\nContent-Type: multipart/alternative; boundary="B"',
            "--B\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><body><p>Only HTML here.</p></body></html>\r\n"
            "--B--\r\n",
        )
        assert "Only HTML here." in parse_message(raw).body

    def test_html_tags_are_stripped_not_rendered(self) -> None:
        # We store text, never markup. Handing raw HTML to the browser
        # would be a cross-site-scripting hole in the inbox view.
        raw = build(
            "Subject: x\r\nContent-Type: text/html; charset=utf-8",
            "<p>Hello <script>alert('xss')</script>world</p>",
        )
        body = parse_message(raw).body
        assert "<script>" not in body
        assert "<p>" not in body

    def test_script_contents_are_removed_not_merely_untagged(self) -> None:
        # Stripping only the tags would leave "alert('xss')" sitting in
        # the body as visible gibberish. Script and style bodies are code,
        # not text, so the whole element goes.
        raw = build(
            "Subject: x\r\nContent-Type: text/html; charset=utf-8",
            "<p>Hi</p><script>alert('xss')</script>",
        )
        body = parse_message(raw).body
        assert "alert" not in body
        assert "Hi" in body

    def test_escaped_markup_does_not_become_real_markup(self) -> None:
        # Entities are unescaped only AFTER tags are removed. If the order
        # were reversed, '&lt;script&gt;' in the source would turn into a
        # genuine '<script>' tag in our stored "plain text" -- which the
        # inbox then renders. Pinning the order here because the bug it
        # prevents is invisible in normal use.
        raw = build(
            "Subject: x\r\nContent-Type: text/html; charset=utf-8",
            "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>",
        )
        body = parse_message(raw).body
        assert "<script>" in body  # inert text, never re-parsed as markup
        assert body.count("<script>") == 1

    def test_attachments_are_ignored_in_the_body(self) -> None:
        # An attachment's bytes must not be dumped into the body text.
        raw = build(
            'Subject: x\r\nContent-Type: multipart/mixed; boundary="B"',
            "--B\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "See attached.\r\n"
            "--B\r\n"
            "Content-Type: application/pdf\r\n"
            'Content-Disposition: attachment; filename="doc.pdf"\r\n'
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n"
            "JVBERi0xLjQK\r\n"
            "--B--\r\n",
        )
        body = parse_message(raw).body
        assert "See attached." in body
        assert "JVBERi0xLjQK" not in body

    def test_nested_multipart_is_traversed(self) -> None:
        # multipart/mixed wrapping multipart/alternative is what you get
        # from most real clients when there is an attachment.
        raw = build(
            'Subject: x\r\nContent-Type: multipart/mixed; boundary="OUT"',
            "--OUT\r\n"
            'Content-Type: multipart/alternative; boundary="IN"\r\n'
            "\r\n"
            "--IN\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Nested plain text.\r\n"
            "--IN--\r\n"
            "--OUT--\r\n",
        )
        assert "Nested plain text." in parse_message(raw).body


# --------------------------------------------------------------------------- #
# Robustness -- a mail server may never crash on bad input
# --------------------------------------------------------------------------- #


class TestMalformedInput:
    def test_empty_input_does_not_raise(self) -> None:
        parsed = parse_message(b"")
        assert parsed.subject == ""
        assert parsed.body == ""

    def test_headers_with_no_body_separator_do_not_raise(self) -> None:
        parse_message(b"Subject: truncated")

    def test_invalid_utf8_is_replaced_not_fatal(self) -> None:
        # Undecodable bytes must degrade to replacement characters. A
        # UnicodeDecodeError here would let one bad message kill delivery.
        raw = b"Subject: x\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n\xff\xfe"
        parse_message(raw)

    def test_declared_charset_that_does_not_exist_is_survivable(self) -> None:
        raw = (
            b"Subject: x\r\n"
            b"Content-Type: text/plain; charset=not-a-real-charset\r\n"
            b"\r\n"
            b"body text"
        )
        assert "body text" in parse_message(raw).body

    def test_broken_encoded_word_in_subject_is_survivable(self) -> None:
        parse_message(build("Subject: =?utf-8?B?!!!not-base64!!!?="))

    def test_multipart_with_a_missing_boundary_is_survivable(self) -> None:
        raw = build(
            'Subject: x\r\nContent-Type: multipart/alternative; boundary="NOPE"',
            "there is no boundary marker anywhere in this body",
        )
        parse_message(raw)

    def test_corrupt_base64_body_does_not_raise(self) -> None:
        # A part that declares base64 and then carries something else is
        # a real thing spam senders produce. The decode must fail softly:
        # this is the `except` around get_payload(decode=True), and it is
        # the difference between one bad message and a dead mail server.
        raw = build(
            "Subject: x\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Transfer-Encoding: base64",
            "!!!!! definitely not base64 !!!!!",
        )
        parse_message(raw)

    def test_part_with_no_payload_does_not_raise(self) -> None:
        # An empty part inside a multipart -- produced by some broken
        # clients -- yields None rather than bytes.
        raw = build(
            'Subject: x\r\nContent-Type: multipart/mixed; boundary="B"',
            "--B\r\nContent-Type: text/plain\r\n\r\n--B--\r\n",
        )
        parse_message(raw)

    def test_attachment_only_message_yields_an_empty_body(self) -> None:
        # Every part is an attachment, so there is no body text at all.
        # The `return ""` at the end of _extract_body covers this, and
        # the result must be empty rather than the attachment's bytes.
        raw = build(
            'Subject: x\r\nContent-Type: multipart/mixed; boundary="B"',
            "--B\r\n"
            "Content-Type: application/pdf\r\n"
            'Content-Disposition: attachment; filename="a.pdf"\r\n'
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n"
            "JVBERi0xLjQK\r\n"
            "--B--\r\n",
        )
        assert parse_message(raw).body == ""

    def test_empty_html_part_is_survivable(self) -> None:
        # _strip_html's early return for empty input.
        raw = build("Subject: x\r\nContent-Type: text/html; charset=utf-8", "")
        assert parse_message(raw).body == ""


# --------------------------------------------------------------------------- #
# Round-trip against messages built the way real clients build them
# --------------------------------------------------------------------------- #


class TestRoundTripWithStdlibComposer:
    """Parse messages composed by `email.message.EmailMessage`.

    The tests above hand-write raw bytes, which is precise but risks
    encoding messages the way *we* imagine clients do. Composing with
    the stdlib exercises the real encoder -- it chooses transfer
    encodings and RFC 2047 header encoding on its own -- so these tests
    catch assumptions the hand-written fixtures would not.
    """

    def test_unicode_subject_and_body_survive_a_round_trip(self) -> None:
        from email.message import EmailMessage

        mail = EmailMessage()
        mail["Subject"] = "Caf\u00e9 \u2014 your code is 1234"
        mail.set_content("H\u00e9llo,\n\nYour code is 1234.\n")

        parsed = parse_message(bytes(mail))

        assert parsed.subject == "Caf\u00e9 \u2014 your code is 1234"
        assert parsed.body.strip() == "H\u00e9llo,\n\nYour code is 1234."

    def test_stdlib_multipart_prefers_the_plain_part(self) -> None:
        from email.message import EmailMessage

        mail = EmailMessage()
        mail["Subject"] = "Newsletter"
        mail.set_content("Plain text version.")
        mail.add_alternative(
            "<html><body><p>HTML version.</p></body></html>", subtype="html"
        )

        parsed = parse_message(bytes(mail))

        assert parsed.body.strip() == "Plain text version."

    def test_stdlib_attachment_is_not_dumped_into_the_body(self) -> None:
        from email.message import EmailMessage

        mail = EmailMessage()
        mail["Subject"] = "Invoice"
        mail.set_content("See attached invoice.")
        mail.add_attachment(
            b"%PDF-1.4 fake pdf bytes",
            maintype="application",
            subtype="pdf",
            filename="invoice.pdf",
        )

        parsed = parse_message(bytes(mail))

        assert parsed.body.strip() == "See attached invoice."
        assert "PDF" not in parsed.body


# --------------------------------------------------------------------------- #
# Body size
# --------------------------------------------------------------------------- #


class TestBodyLimit:
    def test_oversized_body_is_truncated(self) -> None:
        # Unbounded bodies are a denial-of-service vector: a single
        # message could otherwise fill the disk and the browser tab.
        raw = build("Subject: x", "A" * 200_000)
        body = parse_message(raw, max_body_chars=1_000).body
        assert len(body) <= 1_100  # limit plus the truncation notice

    def test_truncation_is_announced_to_the_reader(self) -> None:
        # Silently dropping content is worse than saying you dropped it.
        raw = build("Subject: x", "A" * 5_000)
        body = parse_message(raw, max_body_chars=100).body
        assert "truncated" in body.lower()

    def test_body_within_the_limit_is_untouched(self) -> None:
        raw = build("Subject: x", "short body")
        assert parse_message(raw, max_body_chars=1_000).body == "short body"
