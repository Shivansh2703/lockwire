"""Lockwire — the physical-assumption contract for robots, enforced in CI and at preflight."""

__version__ = "0.2.8"


def main() -> None:
    import sys

    from lockwire.catalog import PrivatePartsError
    from lockwire.cli import app
    from lockwire.schema import ContractError

    for stream in (sys.stdout, sys.stderr):  # a cp1252 Windows console cannot print Δ, →, ✓
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        app()
    except PrivatePartsError as error:  # one line, not a traceback
        from rich.console import Console
        from rich.markup import escape

        Console(stderr=True).print(f"[red]private parts file broken:[/] {escape(str(error))}")
        sys.exit(2)
    except ContractError as error:  # the user's lockfile, not a bug: plain lines, no traceback
        from rich.console import Console
        from rich.markup import escape

        Console(stderr=True).print(escape(str(error)), soft_wrap=True, highlight=False)
        sys.exit(2)
    except OSError as error:  # a file-system problem, not a lockwire bug: one plain line
        from rich.console import Console
        from rich.markup import escape

        from lockwire.cli import plain_os_error

        Console(stderr=True).print(escape(plain_os_error(error)), soft_wrap=True, highlight=False)
        sys.exit(2)
    except Exception as error:  # a crash is a bug, never a verdict: REFUSED (2), not NO-GO (1)
        from rich.console import Console
        from rich.markup import escape

        Console(stderr=True).print(
            f"[red]lockwire stopped: {type(error).__name__}: {escape(str(error))} "
            "— this is a bug in lockwire, not a verdict[/]"
        )
        sys.exit(2)
