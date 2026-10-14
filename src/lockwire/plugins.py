"""The plugin seam: extensions (e.g. the paid package) register through the
``lockwire.plugins`` entry-point group. Free code names no plugin."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable

# A plugin may swap in a richer implementation of a library function (same name,
# same signature); free callers go through `pick`, so without a plugin they get their own.
OVERRIDES: dict[str, Callable[..., Any]] = {}


def pick(name: str, default: Callable[..., Any]) -> Callable[..., Any]:
    return OVERRIDES.get(name, default)


_ready = False


def ensure() -> None:
    """Load plugins once for library callers that never went through the CLI."""
    if not _ready:
        load()


def load(app: Any = None, mcp: Any = None) -> None:
    """Call every plugin with the typer `app` and/or MCP server to extend.

    A plugin that cannot import must not take the free tool down: one dim line, move on.
    """
    global _ready
    _ready = True
    for ep in entry_points(group="lockwire.plugins"):
        try:
            ep.load()(app=app, mcp=mcp)
        except ImportError as error:
            from rich.console import Console

            Console(stderr=True).print(f"[dim]plugin {ep.name} not loaded: {error}[/]")
