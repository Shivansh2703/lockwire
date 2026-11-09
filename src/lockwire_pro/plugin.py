"""The paid plugin: entry point ``lockwire.plugins`` -> ``register``."""

from __future__ import annotations

from typing import Any

from lockwire import plugins


def register(app: Any = None, mcp: Any = None) -> None:
    # Imported here, not at the top: cli_pro imports lockwire.cli, whose tail loads plugins,
    # which would re-enter this module mid-import. paid_checks fills the check slots on import.
    from lockwire_pro import (  # noqa: F401
        api_payload_pro,
        api_pro,
        cli_pro,
        page_pro,
        paid_checks,
        serve_pro,
    )

    plugins.OVERRIDES.update(what_if=api_pro.what_if)
    api_payload_pro.register()
    serve_pro.register()
    if page_pro.page_extras not in plugins.PAGE_EXTRAS:
        plugins.PAGE_EXTRAS.append(page_pro.page_extras)
    if mcp is not None:
        cli_pro.register_tools(mcp)
