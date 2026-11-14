"""The plugin seam: extensions (e.g. the paid package) register through the
``lockwire.plugins`` entry-point group. Free code names no plugin."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable, NamedTuple

# A plugin may swap in a richer implementation of a library function (same name,
# same signature); free callers go through `pick`, so without a plugin they get their own.
OVERRIDES: dict[str, Callable[..., Any]] = {}


# Payload members a plugin adds to the generated app types: enveloped ones by
# `artifact_type`, the rest in a list, and their number-kind rows.
PAYLOAD_MEMBERS: dict[str, type] = {}
PAYLOAD_EXTRA: list[type] = []
PAYLOAD_NUMBER_KINDS: dict[str, dict[str, str]] = {}


# Hooks that add fields to the page payload: each is called as
# `hook(contract=, contract_path=, preflight=, run_findings=, redact_paths=, payload=)` and returns a dict
# merged over the payload (so a plugin may also replace `modes`, `mode_labels`, `command_modes`).
PAGE_EXTRAS: list[Callable[..., dict[str, Any]]] = []


class ServeCommand(NamedTuple):
    """One sidecar command: `handler`, its `args` model, and its request model (a pydantic
    class with a `command: Literal[...]` field). A plugin's handler is called `(body, state)`."""

    handler: Callable[..., Any]
    args: type
    command: type


# Sidecar commands a plugin adds (or replaces, by name) beside the free ones; callables that
# prepare the app's `state` at construction; and the factory behind `lockwire serve --profile`,
# `(profile, required) -> Callable[[request], request]`, raising ValueError to refuse.
SERVE_COMMANDS: dict[str, ServeCommand] = {}
# Pro commands live in the private Pro package. `help --all` lists these names under "Pro"
# whether or not it is installed, with one line pointing at the waitlist. The public snapshot
# reads this set to drop tests that invoke a Pro command.
PRO_COMMANDS = frozenset(
    {
        "preflight", "baseline", "baseline-review", "baseline-check", "sample", "health",
        "actuator-review-draft", "bag", "doctor", "tune", "battery", "print-check", "fly", "fleet-drift",
        "generate", "package", "bringup", "tip", "requirements", "fit", "timing", "power", "corners",
        "autotune", "release", "impact", "wokwi", "kicad", "loads", "cable", "bend", "srdf", "envelope", "drawing",
    }
)  # fmt: skip
WAITLIST_URL = "https://lockwirerobotics.com"  # the website, until a waitlist page exists
WAITLIST_LINE = f"Paid plans are waitlist only for now: {WAITLIST_URL}"
SERVE_SETUP: list[Callable[[Any], None]] = []
SERVE_POLICY: Callable[..., Callable[[Any], Any]] | None = None


def pick(name: str, default: Callable[..., Any]) -> Callable[..., Any]:
    return OVERRIDES.get(name, default)


_ready = False
FAILED: list[ImportError] = []  # plugins that could not import; `check` refuses if it needs one


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
            FAILED.append(error)
            from rich.console import Console
            from rich.markup import escape

            Console(stderr=True, emoji=False).print(
                f"[dim]plugin {escape(ep.name)} not loaded: {escape(str(error))}[/]"
            )
