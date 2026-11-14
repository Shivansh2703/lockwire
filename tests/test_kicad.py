import ast
import stat
from pathlib import Path

from lockwire import kicad

FX = Path(__file__).parent / "fixtures" / "kicad"
FAKE = """#!/bin/sh
out=""; while [ $# -gt 0 ]; do [ "$1" = "--output" ] && out="$2"; shift; done
case "$out" in *erc.json) cp {fx}/erc.json "$out";; *drc.json) cp {fx}/drc.json "$out";;
*net.xml) cp {fx}/net.xml "$out";; *.glb) echo glb > "$out";; esac
"""


def _project(tmp_path):
    (tmp_path / "board.kicad_sch").write_text("")
    (tmp_path / "board.kicad_pcb").write_text("")
    return tmp_path / "board.kicad_pro"


def _fake_cli(tmp_path, monkeypatch):
    cli = tmp_path / "kicad-cli"
    cli.write_text(FAKE.format(fx=FX))
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("LOCKWIRE_KICAD_CLI", str(cli))


def test_erc_and_drc_json_become_plain_rows():
    erc = kicad.parse_erc(__import__("json").loads((FX / "erc.json").read_text()))
    assert [r["type"] for r in erc] == ["pin_not_connected", "power_pin_not_driven"]
    assert erc[0]["where"] == "Symbol J1 Pin 2" and erc[0]["severity"] == "error"
    drc = kicad.parse_drc(__import__("json").loads((FX / "drc.json").read_text()))
    assert [r["type"] for r in drc] == ["clearance", "unconnected_items"]
    assert drc[0]["where"] == "Track [GND] on F.Cu; Via [VCC]"


def test_netlist_maps_each_connector_pin_to_its_net():
    nets = kicad.parse_netlist((FX / "net.xml").read_text())
    assert nets[("J1", "2")] == "/UART_TX" and nets[("U1", "2")] == "GND"
    assert ("J1", "4") not in nets


def test_without_kicad_cli_one_plain_line_says_so_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCKWIRE_KICAD_CLI", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = kicad.run(_project(tmp_path))
    assert result["lines"] == [kicad.NO_CLI]
    assert not (result["erc"] or result["drc"] or result["model"] or result["netlist"])


def test_a_folder_with_no_kicad_files_emits_nothing(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch)
    result = kicad.run(tmp_path / "empty")
    assert result == {"lines": [], "erc": [], "drc": [], "model": None, "netlist": {}}


def test_run_collects_erc_drc_model_and_netlist_from_kicad_cli(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch)
    result = kicad.run(_project(tmp_path), out_dir=tmp_path / "out")
    assert result["lines"] == [] and len(result["erc"]) == 2 and len(result["drc"]) == 2
    assert Path(result["model"]).read_text() == "glb\n"
    assert result["netlist"][("J1", "3")] == "+5V"


def test_a_failing_kicad_cli_is_named_not_swallowed(tmp_path, monkeypatch):
    cli = tmp_path / "kicad-cli"
    cli.write_text("#!/bin/sh\necho 'boom: bad file' >&2\nexit 3\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("LOCKWIRE_KICAD_CLI", str(cli))
    lines = kicad.run(_project(tmp_path))["lines"]
    assert "kicad-cli sch erc failed: boom: bad file" in lines


def test_the_bridge_never_imports_kicad_code():
    tree = ast.parse(Path(kicad.__file__).read_text())
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not [n for n in names if n.split(".")[0] in {"pcbnew", "kicad", "kiutils", "eeschema"}]
