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
python send_test_mail.py PASTE_THE_ADDRESS_HERE
```

It appears in the inbox immediately — no refresh, no polling.

The helper has a few switches worth knowing:

```bash
python send_test_mail.py ADDRESS --unicode      # RFC 2047 encoded headers
python send_test_mail.py ADDRESS --html         # multipart/alternative
python send_test_mail.py ADDRESS --count 5      # a burst
```

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
- **Pytest** with in-memory SQLite and a frozen clock: ~300 tests run in about
  five seconds, so there is never a reason to skip them.
- **Coverage** is measured with a 95% floor. It is a regression alarm, not a
  goal — a test with no assertions reports 100% and proves nothing. The few
  `# pragma: no cover` marks are defensive branches guarding against stdlib
  behaviour that cannot be provoked from a test; each says so and says why it
  stays.

CI runs exactly this gate on every push, across Python 3.11–3.13 on Linux plus
one Windows job. Nothing more: anything worth asserting belongs in `tests/`,
where pytest runs it and ruff and mypy check it, not in a YAML file nobody
maintains.

What CI adds over running `make check` yourself is the *clean machine*. A
developer virtualenv accumulates packages installed once and never declared;
tests then pass against a dependency the project does not actually require.
That is not hypothetical here — it is how the `httpx`/`httpx2` mismatch was
found, on the very first CI run.

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
| `config.py` | Environment parsing and validation. |
| `api.py` | FastAPI app factory, routes, and the composition root. |
| `__main__.py` | Entry point: starts HTTP, SMTP and the sweeper together. |

### How a message travels

```
  browser                server                     sender
     |                      |                          |
     |-- POST /api/mailboxes -->                        |
     |<-- swift-otter-4271@localhost.test --            |
     |                      |                          |
     |-- WS /ws/{address} -->|                          |
     |<----- connected ------|                          |
     |                      |<---- SMTP: RCPT TO -------|
     |                      |----- 250 OK ------------->|
     |                      |<---- SMTP: DATA ----------|
     |                      |                          |
     |                      | parse -> store -> publish |
     |<===== message frame ==|   (no polling)           |
```

The sender and the browser never know about each other. SMTP calls
`service.deliver_message()`, which publishes a `MessageDelivered` event; a
subscriber turns that into a WebSocket frame. Swapping either end — a different
mail receiver, server-sent events instead of WebSockets — touches nothing in
the service or the domain.

### Design decisions worth knowing

| Decision | Reasoning |
|---|---|
| Time, randomness and storage are injected | Tests are deterministic and run in milliseconds. `is_expired(now)` never reads the clock itself. |
| Extension *resets* the window | `expires_at = now + 10min`, always. Exceeding the cap is structurally impossible rather than guarded against. |
| Expiry is final | Reviving a dead address would let a new owner reclaim one already handed out. A security property, not strictness. |
| `.test` TLD (RFC 6761) | Reserved for testing, so generated addresses cannot collide with real internet mail. |
| Reject at `RCPT`, not `DATA` | The sender learns immediately. Accept-then-discard makes mail vanish while the sender believes it arrived. |
| Envelope over headers | `MAIL FROM` is what the peer declared; `From:` is free text and trivially forged. Arrival time comes from our clock, never `Date:`. |
| HTML stripped, never stored | The inbox renders in a browser. Storing attacker-controlled markup would be stored XSS. The frontend then uses `textContent` as a second, independent layer. |
| SMTP on 1025, not 25 | Sub-1024 ports need root. A mail server running as root turns any bug into a host compromise. |
| `410 Gone` for expired, `404` for unknown | 410 tells a client the address is dead for good and retrying is pointless. |

## Licence

MIT.
