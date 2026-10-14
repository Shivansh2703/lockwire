"""Lockwire — the physical-assumption contract for robots, enforced in CI and at preflight."""

__version__ = "0.1.0"


def main() -> None:
    from lockwire.cli import app

    app()
