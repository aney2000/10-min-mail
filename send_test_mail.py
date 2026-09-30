#!/usr/bin/env python3
"""Send a test message to a mailbox.

The equivalent of `swaks`, but with no dependency beyond the standard
library, so it works anywhere Python does.

    python send_test_mail.py bob@localhost.test
    python send_test_mail.py bob@localhost.test --subject "Hi" --body "There"
    python send_test_mail.py bob@localhost.test --html
    python send_test_mail.py bob@localhost.test --count 5

Exists because retyping an smtplib snippet every time you want to see a
message land is exactly the friction that stops people from testing
their own work.
"""

from __future__ import annotations

import argparse
import smtplib
import sys
from email.message import EmailMessage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send test mail to a 10 Minute Mail address.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("recipient", help="The disposable address to send to.")
    parser.add_argument(
        "--host", default="localhost", help="SMTP host (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=1025, help="SMTP port (default: 1025)"
    )
    parser.add_argument(
        "--from",
        dest="sender",
        default="tester@example.org",
        help="Envelope sender (default: tester@example.org)",
    )
    parser.add_argument("--subject", default="Test message")
    parser.add_argument(
        "--body",
        default="This is a test message.\n\nYour code is 123456.",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Also attach an HTML alternative, to exercise the parser's "
        "preference for text/plain and its tag stripping.",
    )
    parser.add_argument(
        "--unicode",
        action="store_true",
        help="Use non-ASCII text, to exercise RFC 2047 header encoding.",
    )
    parser.add_argument(
        "--count", type=int, default=1, help="How many messages to send."
    )
    return parser


def build_message(args: argparse.Namespace, index: int) -> EmailMessage:
    subject = args.subject
    body = args.body

    if args.unicode:
        subject = f"Café — {subject} ✉"
        body = f"Héllo wörld! 你好\n\n{body}"

    if args.count > 1:
        subject = f"{subject} ({index}/{args.count})"

    message = EmailMessage()
    message["From"] = args.sender
    message["To"] = args.recipient
    message["Subject"] = subject
    message.set_content(body)

    if args.html:
        # add_alternative makes this multipart/alternative -- the shape
        # nearly all real mail arrives in. The server should show the
        # plain part and ignore this one.
        message.add_alternative(
            f"<html><body>"
            f"<h1>{subject}</h1>"
            f"<p>{body}</p>"
            f"<script>alert('this must never run')</script>"
            f"</body></html>",
            subtype="html",
        )

    return message


def main() -> int:
    args = build_parser().parse_args()

    try:
        with smtplib.SMTP(args.host, args.port, timeout=15) as client:
            for index in range(1, args.count + 1):
                client.send_message(build_message(args, index))
                print(f"sent {index}/{args.count} to {args.recipient}")
    except smtplib.SMTPRecipientsRefused as exc:
        code, reason = exc.recipients[args.recipient.lower()]
        print(f"refused ({code}): {reason.decode()}", file=sys.stderr)
        print(
            "\nThe mailbox is unknown or has expired. Create a fresh one at "
            "http://localhost:8000 and try again.",
            file=sys.stderr,
        )
        return 1
    except (OSError, smtplib.SMTPException) as exc:
        print(f"could not reach {args.host}:{args.port} -- {exc}", file=sys.stderr)
        print(
            "\nIs the server running? Try `docker compose up` or "
            "`python -m ten_min_mail`.",
            file=sys.stderr,
        )
        return 1

    print("\nCheck the browser -- it should already be there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
