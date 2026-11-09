"""Lockwire — the physical-assumption contract for robots, enforced in CI and at preflight."""

__version__ = "0.2.6"


def main() -> None:
    import sys

    from lockwire.cli import app

    try:
        app()
    except Exception as error:  # a crash is a bug, never a verdict: REFUSED (2), not NO-GO (1)
        from rich.console import Console
        from rich.markup import escape

        Console(stderr=True).print(
            f"[red]lockwire stopped: {type(error).__name__}: {escape(str(error))} "
            "— this is a bug in lockwire, not a verdict[/]"
        )
        sys.exit(2)
