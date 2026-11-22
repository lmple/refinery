"""Smoke test: proves the package imports and the test gate is wired up."""

import refinery


def test_package_exposes_main() -> None:
    assert callable(refinery.main)
