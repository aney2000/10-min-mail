# 10 Minute Mail

A local, disposable email service — built for learning FastAPI, SMTP, TDD, and
clean architecture. Runs entirely on your machine via Docker.

## Status

Under construction. See `git log` for incremental progress.

The full business logic (domain, storage, mailbox lifecycle) is complete and
tested. HTTP and SMTP layers are next.

## Setup

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -e ".[dev]"
```

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
- **Pytest** with in-memory SQLite and a frozen clock: the whole suite runs in
  ~0.1s, so there is never a reason to skip it.

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
| `service.py` | Use cases: create, extend, deliver, read, purge. |
