"""Refinery: a spec-driven, formally verified software factory."""


def main() -> None:
    """The ``refinery`` console script: factoryd's admin CLI."""
    # Imported here so that `import refinery` stays light.
    from refinery.factoryd.cli import app

    app()
