"""Smoke test: proves the package is installed and importable.

If this fails, nothing else can pass. It has no business logic — it exists
to validate the project skeleton itself.
"""

import ten_min_mail


def test_package_exposes_version() -> None:
    assert ten_min_mail.__version__ == "0.1.0"
