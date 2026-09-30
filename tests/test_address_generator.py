"""Tests for the address generator.

The generator has two collaborators it does NOT own:
  - a `random.Random` instance (for deterministic tests)
  - an `AddressAvailabilityChecker` (so it can avoid collisions)

Both are injected. That's Dependency Inversion in action: the generator
depends on abstractions it defines, not on concrete storage or global state.
"""

from __future__ import annotations

import random
import re

import pytest

from ten_min_mail.address_generator import (
    AddressExhaustedError,
    RandomAddressGenerator,
)

# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeChecker:
    """In-memory `AddressAvailabilityChecker`.

    Using a hand-rolled fake (not a mock) keeps tests readable and lets the
    test file document exactly what behavior we depend on. If the Protocol
    ever grows, the type checker will tell us to update the fake — which is
    a feature, not a bug.
    """

    def __init__(self, taken: set[str] | None = None) -> None:
        self._taken: set[str] = set(taken or ())

    def is_taken(self, address: str) -> bool:
        return address in self._taken

    def mark_taken(self, address: str) -> None:
        self._taken.add(address)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


class TestRandomAddressGenerator:
    def test_generates_address_on_our_domain(self) -> None:
        gen = RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(0),
            checker=FakeChecker(),
        )
        address = gen.generate()
        assert address.endswith("@localhost.test")

    def test_generated_address_matches_expected_shape(self) -> None:
        # Shape: <word>-<word>-<4 digits>@<domain>
        # Documenting the shape as a test protects against accidental changes.
        gen = RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(0),
            checker=FakeChecker(),
        )
        address = gen.generate()
        local, _, _ = address.partition("@")
        assert re.fullmatch(r"[a-z]+-[a-z]+-\d{4}", local), (
            f"unexpected local-part shape: {local!r}"
        )

    def test_same_seed_produces_same_address(self) -> None:
        # Reproducibility: identical inputs => identical outputs.
        # This is why we inject `random.Random` instead of using the module-level
        # `random.choice`.
        gen1 = RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(123),
            checker=FakeChecker(),
        )
        gen2 = RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(123),
            checker=FakeChecker(),
        )
        assert gen1.generate() == gen2.generate()

    def test_two_calls_produce_two_different_addresses(self) -> None:
        # With a healthy word list and a 4-digit suffix, collisions across two
        # sequential calls should be astronomically unlikely.
        gen = RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(0),
            checker=FakeChecker(),
        )
        assert gen.generate() != gen.generate()


# --------------------------------------------------------------------------- #
# Collision handling
# --------------------------------------------------------------------------- #


class TestCollisionHandling:
    def test_retries_when_first_pick_is_taken(self) -> None:
        # We rig the checker: the very first candidate the generator produces
        # is already taken. The generator must retry and return a different one.
        gen = RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(7),
            checker=FakeChecker(),
        )
        # Ask what it *would* pick first, then poison that pick.
        first_pick = RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(7),
            checker=FakeChecker(),
        ).generate()

        checker = FakeChecker(taken={first_pick})
        gen = RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(7),
            checker=checker,
        )
        second_pick = gen.generate()

        assert second_pick != first_pick
        assert not checker.is_taken(second_pick)

    def test_raises_after_exhausting_retry_budget(self) -> None:
        # A checker that says "taken" to *everything* forces the generator
        # to give up. Bounded loops > infinite loops.
        class AlwaysTakenChecker:
            def is_taken(self, address: str) -> bool:
                return True

        gen = RandomAddressGenerator(
            domain="localhost.test",
            rng=random.Random(0),
            checker=AlwaysTakenChecker(),
            max_attempts=5,
        )
        with pytest.raises(AddressExhaustedError):
            gen.generate()


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


class TestConstructorValidation:
    @pytest.mark.parametrize(
        "bad_domain",
        ["", "no-dot", "spaces here.com", "@bad.com"],
    )
    def test_rejects_bad_domain(self, bad_domain: str) -> None:
        with pytest.raises(ValueError):
            RandomAddressGenerator(
                domain=bad_domain,
                rng=random.Random(0),
                checker=FakeChecker(),
            )

    def test_rejects_non_positive_max_attempts(self) -> None:
        with pytest.raises(ValueError):
            RandomAddressGenerator(
                domain="localhost.test",
                rng=random.Random(0),
                checker=FakeChecker(),
                max_attempts=0,
            )
