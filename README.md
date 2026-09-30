# 10 Minute Mail

A local, disposable email service — built for learning FastAPI, SMTP, TDD, and
clean architecture. Runs entirely on your machine.

Generates a throwaway address, receives real email over SMTP, and shows it in
your browser the instant it arrives. Everything expires after ten minutes.

## Run it with Docker

```bash
docker compose up --build
```

Then open <http://localhost:8000>.

Send a test message to the address it shows you:

```bash
python -c "
import smtplib
from email.message import EmailMessage
m = EmailMessage()
m['From'] = 'someone@example.org'
m['To']   = 'PASTE_THE_ADDRESS_HERE'
m['Subject'] = 'Hello'
m.set_content('It works.')
smtplib.SMTP('localhost', 1025).send_message(m)
"
```

It appears in the inbox immediately — no refresh, no polling.

| Port | Purpose |
|---|---|
| 8000 | Web UI and REST API |
| 1025 | SMTP |

```bash
docker compose down      # stop, keep mailboxes
docker compose down -v   # stop and delete the data volume
```

### Docker Engine on WSL2 (no Docker Desktop)

Docker Desktop requires a paid licence for business use. Docker *Engine* is
free (Apache 2.0) and runs fine inside WSL2:

```bash
wsl -d Ubuntu
sudo apt install docker.io docker-compose-v2
sudo usermod -aG docker "$USER"   # then restart the shell
```

One catch: the plain engine publishes ports on the **WSL VM**, not on Windows
`localhost` — Docker Desktop is what normally bridges those. Either run
`curl`/the browser from inside WSL, or enable mirrored networking once, in
`C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

Then `wsl --shutdown` and restart. `localhost:8000` now works from Windows.

## Develop it locally

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -e ".[dev]"

python -m ten_min_mail     # starts HTTP, SMTP and the expiry sweeper
```

### Configuration

All settings come from the environment (12-factor), so the same image runs
anywhere with only variables changing.

| Variable | Default | Meaning |
|---|---|---|
| `TMM_HTTP_HOST` | `127.0.0.1` | Web interface bind address |
| `TMM_HTTP_PORT` | `8000` | Web port |
| `TMM_SMTP_HOST` | `127.0.0.1` | SMTP bind address |
| `TMM_SMTP_PORT` | `1025` | SMTP port (not 25 — see below) |
| `TMM_MAIL_DOMAIN` | `localhost.test` | Domain for generated addresses |
| `TMM_DATABASE_PATH` | `ten_min_mail.db` | SQLite file |
| `TMM_SWEEP_INTERVAL` | `60` | Seconds between expiry sweeps |
| `TMM_LOG_LEVEL` | `INFO` | Logging verbosity |

Both hosts default to loopback deliberately: binding `0.0.0.0` by default would
expose an unauthenticated mail server to the whole network the moment someone
starts it on a laptop in a cafe. The container overrides it, where the exposure
is the operator's explicit choice.

SMTP defaults to **1025, not 25**, because ports below 1024 need root on Unix —
and running a mail server as root turns any bug in it into a host compromise.
Publish `25:1025` in compose if you want the standard port; the privileged bind
then happens in Docker's networking layer, not in this process.

## The commit gate

Every commit must pass lint, type checking, and tests. One command:

```bash
make check              # macOS/Linux, or Git Bash on Windows
.\check.ps1             # Windows PowerShell
```

Individually:

| Command | What it does |
|---|---|
| `ruff check .` | Lint — catches bugs and bad patterns |
| `ruff format .` | Format — consistent style, no debate |
| `mypy` | Type check in `--strict` mode |
| `pytest` | Run the test suite |

### Why these tools

- **Ruff** replaces flake8 + isort + pyupgrade + black in a single fast binary.
  Free and open source.
- **Mypy** in strict mode is what actually *verifies* the `Protocol` contracts
  the architecture relies on. Without it, a `Protocol` is only documentation.
- **Pytest** with in-memory SQLite and a frozen clock: ~290 tests run in about
  five seconds, so there is never a reason to skip them.

## Architecture

Dependencies point inward. The domain knows nothing about frameworks.

```
        +----------+    +----------+
        | FastAPI  |    |   SMTP   |   delivery mechanisms
        +----+-----+    +----+-----+
             +---------------+
                     |
               +-----v------+
               |  service   |          use cases
               +-----+------+
                     |
       +---------+---+----+----------+
       | domain  |  repo  |  clock   |  building blocks
       +---------+--------+----------+
```

| Module | Responsibility |
|---|---|
| `domain.py` | `Mailbox`, `Message`, the 10-minute rule. Zero dependencies. |
| `clock.py` | `Clock` protocol + system/frozen implementations. |
| `address_generator.py` | Readable random addresses, collision-checked. |
| `repository.py` | SQLite persistence. Translates storage errors to domain errors. |
| `events.py` | Domain events + publisher. The service announces; it never listens. |
| `service.py` | Use cases: create, extend, deliver, read, purge. |
| `mail_parsing.py` | Raw RFC 5322 bytes into subject + plain-text body. |
| `smtp.py` | SMTP receiver. Rejects unknown recipients at `RCPT` with 550. |
| `websocket.py` | Connection registry + broadcaster for the live inbox. |
| `housekeeping.py` | Periodic sweep of expired mailboxes. |
| `api.py` | FastAPI app factory, routes, and the composition root. |
