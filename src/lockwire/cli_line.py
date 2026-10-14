"""One function that turns a command's parts into a shell-safe line.

Every door that prints or returns a paste-able ``lockwire ...`` command
composes its parts here — the CLI's own ``ran:`` lines, the ``gui``/``start``
page footer's line, and ``scan.py``'s docker ``bash -lc`` line — so there is
exactly one place quoting can go wrong, and it uses ``shlex.quote`` (stdlib),
not hand-rolled or Python (``repr``/``!r``) quoting.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import typer


def cli_line(*parts: object) -> str:
    """Shell-quote every part and join with spaces.

    Callers that want a literal ``lockwire ...`` line to paste back in pass
    ``"lockwire"`` as the first part themselves; this function only quotes
    and joins.
    """
    return " ".join(shlex.quote(str(part)) for part in parts)


def argv_from_context(ctx: "typer.Context", exclude: "set[str] | None" = None) -> list[str]:
    """Rebuild the argv actually typed for this invocation.

    Walks the command's own parameters (``ctx.command.params``) and each
    one's source (``ctx.get_parameter_source``), including only what the
    user actually passed on the command line — instead of a hand-written
    flag list, which is exactly how ``add`` dropped its value flags,
    how ``scene import`` dropped ``--package-root``, and how ``declare``
    dropped ``--note``/``--locked-at``.

    Bools carry their own hazard and get their own handling: a true flag
    emits its option, a false one emits its ``--no-...`` form if it has one
    and otherwise **nothing**. Both directions matter — a replayed
    ``apply --dry-run`` line that lost the flag performs a real write, and a
    line that grew a ``--dry-run`` nobody typed describes a run that wrote
    nothing when the real one wrote.

    ``exclude`` names parameters the caller renders itself; the only one in
    the tree is ``gui``'s ``--out``, deliberately printed as ``<this file>``
    so the page's bytes do not depend on its own filename.
    """
    argv: list[str] = []
    skip = exclude or set()
    for param in ctx.command.params:
        if param.name in skip:
            continue
        source = ctx.get_parameter_source(param.name)
        if source is None or source.name != "COMMANDLINE":
            continue
        value = ctx.params[param.name]
        if param.param_type_name == "argument":
            argv.append(str(value))
        elif isinstance(value, bool):
            if value:
                argv.append(param.opts[0])
            elif param.secondary_opts:
                argv.append(param.secondary_opts[0])
        elif isinstance(value, (list, tuple)):
            for item in value:
                argv.append(param.opts[0])
                argv.append(str(item))
        else:
            argv.append(param.opts[0])
            argv.append(str(value))
    return argv
