import asyncio

import typer

from lockwire.cli import app
from lockwire.mcp_server import build_server

TOOLS_BY_COMMAND = {
    "check": {"check_contract"},
    "check-sim": {"check_sim_config"},
    "what-if": {"compare_proposal"},
    "from-parts": {"match_parts_list"},
    "describe": {"describe_device", "describe_system", "list_devices"},
    "plan": {"validate_plan"},
    "review": {"review_value"},
    "upgrade": {"upgrade_contract"},
    "catalog robots": {"list_robots"},
}
NAMED_GAPS = {
    "agent-setup": "writes files into the user's repo; no MCP door by the no-public-door rule",
    "hook-check": "reads a Claude Code hook payload on stdin; an agent's MCP door is check_model",
    "help": "lists the CLI's own commands; an agent has the MCP tool list",
}
for _names, _why in {
    "accept add gearbox declare apply scene~import": "contract write door",
    "mount-shape move place reparent rotate": "pose edit, a contract write door",
    "export init start report": "writes a file for the user, not a question",
    "hw scan": "probes attached hardware",
    "diff blame log": "needs git history a robot-facing server does not own",
    "mcp serve": "launches a server itself",
    "bench api-types api-schema import": "hidden developer or build-time command",
}.items():
    NAMED_GAPS |= {n.replace("~", " "): _why for n in _names.split()}


def _free_leaves(command, prefix=""):
    for name, sub in sorted(command.commands.items()):
        if getattr(sub, "commands", None):
            yield from _free_leaves(sub, f"{prefix}{name} ")
        elif not getattr(sub.callback, "__module__", "").startswith("lockwire_pro"):
            yield f"{prefix}{name}"


def test_every_free_command_has_an_mcp_tool_or_a_named_gap():
    leaves = set(_free_leaves(typer.main.get_command(app)))
    unknown = leaves - TOOLS_BY_COMMAND.keys() - NAMED_GAPS.keys()
    assert not unknown, f"free commands with no MCP tool and no named gap: {sorted(unknown)}"
    stale = (TOOLS_BY_COMMAND.keys() | NAMED_GAPS.keys()) - leaves
    assert not stale, f"listed but not a free command: {sorted(stale)}"

    async def names():
        from mcp.client import Client

        async with Client(build_server()) as client:
            return {t.name for t in (await client.list_tools()).tools}

    mapped = {t for tools in TOOLS_BY_COMMAND.values() for t in tools}
    assert mapped <= asyncio.run(names()), "mapped to a tool the server does not serve"
