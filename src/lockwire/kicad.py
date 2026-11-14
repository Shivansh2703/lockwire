"""KiCad bridge: a project's own ERC/DRC, 3D model and netlist, via `kicad-cli`.

Licence: kicad-cli is GPL, so it only ever runs as a separate process. Never import KiCad,
pcbnew or kiutils here (tests/test_kicad.py fails if this file does).
Flags (KiCad 8/9): https://docs.kicad.org/9.0/en/cli/cli.html
  sch erc --format json --output F.json X.kicad_sch   (JSON: sheets[].violations[])
  pcb drc --format json --output F.json X.kicad_pcb   (JSON: violations[], unconnected_items[])
  pcb export glb --output F.glb X.kicad_pcb
  sch export netlist --format kicadxml --output F.xml X.kicad_sch
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

NO_CLI = (
    "KiCad is not installed (no kicad-cli on PATH, LOCKWIRE_KICAD_CLI not set): nothing checked."
)


def find_cli() -> str | None:
    return os.environ.get("LOCKWIRE_KICAD_CLI") or shutil.which("kicad-cli")


def _rows(kind: str, violations: list[dict]) -> list[dict]:
    return [
        {
            "kind": kind,
            "severity": v.get("severity", ""),
            "type": v.get("type", ""),
            "description": v.get("description", ""),
            "where": "; ".join(i.get("description", "") for i in v.get("items", [])),
        }
        for v in violations
    ]


def parse_erc(document: dict) -> list[dict]:
    return _rows("erc", [v for s in document.get("sheets", []) for v in s.get("violations", [])])


def parse_drc(document: dict) -> list[dict]:
    keys = ("violations", "unconnected_items", "schematic_parity")
    return _rows("drc", [v for k in keys for v in document.get(k, [])])


def parse_netlist(xml_text: str) -> dict[tuple[str, str], str]:
    """(reference, pin) -> net name, from `kicadxml`. Unconnected pins have no entry."""
    out = {}
    for net in ET.fromstring(xml_text).iterfind("./nets/net"):
        for node in net.iterfind("node"):
            out[(node.get("ref", ""), node.get("pin", ""))] = net.get("name", "")
    return out


def _run(cli: str, *args: str) -> str | None:
    """None on success, else the first line kicad-cli said about the failure."""
    done = subprocess.run([cli, *args], capture_output=True, text=True, timeout=300)
    if done.returncode == 0:
        return None
    return ((done.stderr or done.stdout).strip().splitlines() or [f"exit {done.returncode}"])[0]


def run(project: Path, out_dir: Path | None = None) -> dict:
    """{lines, erc, drc, model, netlist}; a missing input or step emits nothing for it."""
    result: dict = {"lines": [], "erc": [], "drc": [], "model": None, "netlist": {}}
    project = Path(project)
    stem = project.with_suffix("") if project.suffix == ".kicad_pro" else project / project.name
    sch, pcb = stem.with_suffix(".kicad_sch"), stem.with_suffix(".kicad_pcb")
    if not (sch.is_file() or pcb.is_file()):
        return result
    cli = find_cli()
    if cli is None:
        result["lines"].append(NO_CLI)
        return result
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="lockwire-kicad-"))
    out.mkdir(parents=True, exist_ok=True)
    steps = []
    if sch.is_file():
        steps += [
            (
                "erc",
                ("sch", "erc", "--format", "json", "--output", str(out / "erc.json"), str(sch)),
            ),
            (
                "net",
                (
                    "sch",
                    "export",
                    "netlist",
                    "--format",
                    "kicadxml",
                    "--output",
                    str(out / "net.xml"),
                    str(sch),
                ),
            ),  # fmt: skip
        ]
    if pcb.is_file():
        steps += [
            (
                "drc",
                ("pcb", "drc", "--format", "json", "--output", str(out / "drc.json"), str(pcb)),
            ),
            ("glb", ("pcb", "export", "glb", "--output", str(out / "board.glb"), str(pcb))),
        ]
    for name, args in steps:
        failed = _run(cli, *args)
        if failed:
            result["lines"].append(f"kicad-cli {' '.join(args[:2])} failed: {failed}")
    if (out / "erc.json").is_file():
        result["erc"] = parse_erc(json.loads((out / "erc.json").read_text()))
    if (out / "drc.json").is_file():
        result["drc"] = parse_drc(json.loads((out / "drc.json").read_text()))
    if (out / "net.xml").is_file():
        result["netlist"] = parse_netlist((out / "net.xml").read_text())
    if (out / "board.glb").is_file():
        result["model"] = str(out / "board.glb")
    return result
