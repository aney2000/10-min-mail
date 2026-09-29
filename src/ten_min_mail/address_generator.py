"""Generation of unique, human-friendly email addresses.

Design overview
---------------
This module defines *two* things:

  1. `AddressAvailabilityChecker` -- a Protocol (structural interface) that
     any storage backend can satisfy just by having an `is_taken(address)`
     method. This is Dependency Inversion: the generator depends on the
     abstraction, not on SQLite / a dict / whatever.

  2. `RandomAddressGenerator` -- an implementation that composes two words
     and a 4-digit suffix on a domain we control, retrying if the checker
     says a candidate is already in use.

Injected collaborators (constructor arguments):
  - `domain`  : the mail domain we own (e.g. 'localhost.test').
  - `rng`     : a `random.Random` instance. Tests pass a seeded one; production
                passes `random.Random()` (unpredictable).
  - `checker` : anything satisfying `AddressAvailabilityChecker`.
  - `max_attempts`: bounded retry budget to guarantee termination.

Why 'localhost.test'?
  RFC 6761 reserves the `.test` TLD for testing / examples. It will never
  conflict with a real domain, so any address we invent on it is guaranteed
  not to collide with the wider internet -- our repository check therefore
  suffices for global uniqueness.
"""

from __future__ import annotations

import random
import re
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Public exceptions
# --------------------------------------------------------------------------- #


class AddressExhaustedError(RuntimeError):
    """Raised when the generator cannot find a free address within its budget.

    In practice this only happens when the checker is broken (always says
    taken) or the namespace really is exhausted -- extremely unlikely with
    a healthy word list and a 4-digit suffix.
    """


# --------------------------------------------------------------------------- #
# Interface (Protocol) -- Dependency Inversion boundary
# --------------------------------------------------------------------------- #


@runtime_checkable
class AddressAvailabilityChecker(Protocol):
    """Anything that can answer 'is this address currently in use?'.

    Protocols are structural: a class satisfies this interface simply by
    having a matching `is_taken` method. No explicit `class X(Checker):`
    inheritance is required. That keeps our storage layer decoupled from
    the generator.
    """

    def is_taken(self, address: str) -> bool: ...  # pragma: no cover


# --------------------------------------------------------------------------- #
# Word lists
# --------------------------------------------------------------------------- #
# Kept small and inline for now. If they grow, we move them to a data file.
# ~30 adjectives x ~30 nouns x 10_000 numbers = ~9 million combinations,
# more than enough for a learning project.

_ADJECTIVES: tuple[str, ...] = (
    "swift",
    "silent",
    "brave",
    "clever",
    "gentle",
    "happy",
    "lucky",
    "mighty",
    "noble",
    "quiet",
    "rapid",
    "shiny",
    "sleepy",
    "witty",
    "amber",
    "azure",
    "crimson",
    "golden",
    "silver",
    "violet",
    "cosmic",
    "curious",
    "dapper",
    "eager",
    "fuzzy",
    "jolly",
    "merry",
    "plucky",
    "spry",
    "zesty",
)

_NOUNS: tuple[str, ...] = (
    "otter",
    "falcon",
    "panda",
    "tiger",
    "koala",
    "cobra",
    "raven",
    "wolf",
    "lynx",
    "hawk",
    "moose",
    "bison",
    "eagle",
    "shark",
    "comet",
    "meteor",
    "pebble",
    "willow",
    "cedar",
    "maple",
    "harbor",
    "canyon",
    "meadow",
    "prairie",
    "summit",
    "tundra",
    "orbit",
    "quartz",
    "beacon",
    "vortex",
)


# --------------------------------------------------------------------------- #
# Domain validation (for the constructor)
# --------------------------------------------------------------------------- #

_DOMAIN_RE = re.compile(r"^[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _is_valid_domain(domain: str) -> bool:
    return bool(_DOMAIN_RE.fullmatch(domain))


# --------------------------------------------------------------------------- #
# The generator
# --------------------------------------------------------------------------- #


class RandomAddressGenerator:
    """Generates readable random addresses, retrying on collisions.

    A single instance is safe to reuse. It has no mutable state of its own;
    all randomness lives in the injected `rng`.
    """

    def __init__(
        self,
        *,
        domain: str,
        rng: random.Random,
        checker: AddressAvailabilityChecker,
        max_attempts: int = 100,
    ) -> None:
        if not _is_valid_domain(domain):
            raise ValueError(f"invalid domain: {domain!r}")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

        self._domain = domain
        self._rng = rng
        self._checker = checker
        self._max_attempts = max_attempts

    def generate(self) -> str:
        """Return a free address. Raises `AddressExhaustedError` if it can't.

        The loop is bounded by `max_attempts` -- code that can loop forever
        is code that will loop forever, eventually, in production.
        """
        for _ in range(self._max_attempts):
            candidate = self._compose_candidate()
            if not self._checker.is_taken(candidate):
                return candidate
        raise AddressExhaustedError(
            f"could not find a free address after {self._max_attempts} attempts"
        )

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _compose_candidate(self) -> str:
        adjective = self._rng.choice(_ADJECTIVES)
        noun = self._rng.choice(_NOUNS)
        suffix = self._rng.randint(0, 9999)
        return f"{adjective}-{noun}-{suffix:04d}@{self._domain}"
