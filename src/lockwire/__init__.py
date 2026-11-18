"""Lockwire - the physical-assumption contract for robots, enforced in CI and at preflight."""

__version__ = "0.5.3"


def main() -> None:
    import sys

    from lockwire.catalog import PrivatePartsError
    from lockwire.cli import app
    from lockwire.schema import ContractError

    for stream in (sys.stdout, sys.stderr):  # a cp1252 Windows console cannot print Δ, →, ✓
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    from lockwire.checks import CheckCrashed, ModelFileRefusal

    try:
        try:
            app()
        except CheckCrashed as crashed:  # the terminal gets the error's own words, as before
            raise crashed.__cause__ from None
    except PrivatePartsError as error:  # one line, not a traceback
        from rich.console import Console
        from rich.markup import escape

        Console(stderr=True, emoji=False).print(
            f"[red]private parts file broken:[/] {escape(str(error))}"
        )
        sys.exit(2)
    except (ContractError, ModelFileRefusal) as error:  # the user's files, not a bug: plain lines
        from rich.console import Console
        from rich.markup import escape

        Console(stderr=True, emoji=False).print(escape(str(error)), soft_wrap=True, highlight=False)
        sys.exit(2)
    except OSError as error:  # a file-system problem, not a lockwire bug: one plain line
        from rich.console import Console
        from rich.markup import escape

        from lockwire.cli import plain_os_error

        Console(stderr=True, emoji=False).print(
            escape(plain_os_error(error)), soft_wrap=True, highlight=False
        )
        sys.exit(2)
    except Exception as error:  # a crash is a bug, never a verdict: REFUSED (2), not NO-GO (1)
        from rich.console import Console
        from rich.markup import escape

        Console(stderr=True, emoji=False).print(
            f"[red]lockwire stopped: {type(error).__name__}: {escape(str(error))}"
            "; this is a bug in lockwire, not a verdict[/]"
        )
        sys.exit(2)
