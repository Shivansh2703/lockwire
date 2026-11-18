"""What a robot file lacks: a short list of absent inputs, read off the finished check's own lines.

No second pass over the physics: every item is made from findings the check already produced, by
their quantity and a phrase of their own text, so it can only name something the check could not
use. Only ABSENT inputs are listed. A value the check asks the person to confirm (a speed limit of
1000 rad/s, placeholder inertia, contacts that do not span an area) is not missing and is not here.

Every instruction an item carries ("how") must work when followed: a command is complete (real lock
path, real field path, placeholders in angle brackets for what only the person knows), a file edit
names the person's file and the elements, and an input no command can supply says so.

Two warning kinds are read as well, because their own text says the check did not run (torque
limit unset, no foot contacts): a model file gives those as warnings, never not-scored.
"""

import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlparse

from lockwire.cli_line import cli_line
from lockwire.report import Finding

SOURCE = "<where this number comes from>"
WITNESS = "<your name>"
NO_LOCK = "lockwire.yaml"

# key -> (plain name, what Lockwire can then check, ((severity, quantity pattern, phrase of the line's text), ...))
# Group 1 of a quantity pattern names the unit (a joint, a link) so two lines about one unit count once.
# The dict order is the order printed: most useful for a person starting out first.
_RULES: dict[str, tuple[str, str, tuple[tuple[str, str, str], ...]]] = {
    "link_masses": (
        "link masses",
        "total weight and centre of gravity; holding torque too, once the joints have effort limits",
        (("warning", r"model/links/([^/]+)/(?:mass|material)", "zero or missing mass|no mass and no material"),),
    ),
    "effort_limits": (
        "joint effort limits",
        "holding torque against each limit, once the links have mass",
        (
            ("warning", r"model/joints/([^/]+)/limits/effort", "unset"),
            ("not_scored", r"datasheet/([^/]+)/joint_torque", "no effort limit"),
        ),
    ),
    "meshes": (
        "mesh files",
        "the mesh shapes, recorded and compared",
        (("not_scored", r"model/meshes", "not inside the robot's package folder"),),
    ),
    "foot_contacts": (
        "foot contacts",
        "the load on the leg joints while standing, once the links have mass and the joints have effort limits",
        (("warning", r"model/standing_load", "no support contacts declared"),),
    ),
    "model_file": (
        "the model file",
        "whether the model has drifted from the lock",
        (("not_scored", r"model/structure_hash", r"model file .*(?: absent;|: relative path)"),),
    ),
    "battery_chemistry": (
        "battery chemistry",
        "the battery limits that depend on it; they may then ask for cell count, C rating or temperature",
        (("not_scored", r"topology/battery/chemistry", "chemistry not declared"),),
    ),
    "battery_c_rate": (
        "battery C rating",
        "peak draw against the pack's C rating, once every device on the pack declares a draw",
        (("not_scored", r"topology/battery/c_rate_continuous", "C-rate not declared"),),
    ),
    "rail_voltages": (
        "rail voltages",
        "rail current and wire loads on that rail",
        (("not_scored", r"topology/rails/([^/]+)/voltage", "no declared voltage"),),
    ),
    "device_masses": (
        "device masses",
        "that device's share of the mass budget",
        (("not_scored", r"devices/([^/]+)/physical/mass", "mass not declared"),),
    ),
    "bus_traffic": (
        "bus traffic",
        "the bus bandwidth",
        (("not_scored", r"topology/buses/([^/]+)/bandwidth", "no declared traffic"),),
    ),
    "ambient": (
        "ambient temperature",
        "the pack temperature window",
        (("not_scored", r"topology/battery/discharge_temp_c", "no declared ambient"),),
    ),
}


def _list(names: list[str], cap: int = 3) -> str:
    """'a', 'a and b', 'a, b and c', then 'a, b, c and N more' past `cap` names."""
    if len(names) > cap:
        return f"{', '.join(names[:cap])} and {len(names) - cap} more"
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]


def _count(names: list[str], one: str, many: str) -> str:
    return f"{len(names)} {one if len(names) == 1 else many}"


class _Where:
    """Where an instruction points: the file the person edits and the lock the commands name."""

    def __init__(self, contract, given) -> None:
        shown, is_model = given or (None, False)
        self.bare = bool(is_model)
        # a robot given by name: a downloaded copy in the tool's cache, not a file the person can edit
        self.downloaded = bool(is_model) and not Path(shown).is_file()
        # a bare model file is locked beside itself, the path `lockwire check` prints for it
        self.lock = str(Path(shown).parent / NO_LOCK) if is_model else str(shown or NO_LOCK)
        self.model: str | None = str(shown) if is_model else None
        self.recorded: str | None = None
        if not is_model and contract is not None and contract.model is not None:
            from lockwire.checks import _model_on_disk, _urdf_path_candidate

            self.recorded = _urdf_path_candidate(contract.model)
            found = self.recorded and shown and _model_on_disk(Path(shown), self.recorded)
            if found and Path(found).is_file():
                here = os.path.relpath(found)
                self.model = here if not here.startswith("..") else str(Path(found).resolve())

    def start(self) -> str:
        """Lock the bare model file beside itself."""
        out = ["--out", self.lock] if self.lock != NO_LOCK else []
        return cli_line("lockwire", "start", self.model, *out)

    def edit(self, what: str) -> str:
        """A change in the model file; for a lock, the file edit and the re-lock it needs."""
        if self.bare:
            return what
        relock = cli_line("lockwire", "start", self.model, "--out", self.lock, "--force")
        return f"{what} Then re-create the lock: {relock} (this replaces the lock, so entries you added to it are lost)."

    def declare(self, path: str, value: str) -> str:
        return cli_line("lockwire", "declare", self.lock, path, value, "--source", SOURCE, "--witness", WITNESS)


def _mesh_path(uri: str, beside: Path) -> Path:
    """Where a mesh uri points on disk: file:// as written, a plain name beside the model file."""
    if uri.startswith("file://"):
        return Path(unquote(urlparse(uri).path))
    return Path("/nonexistent") / uri if "://" in uri else beside / uri


def _frames(model: str) -> set[str]:
    """Links the model file leaves bare on purpose: no inertial, no visual, no collision."""
    try:
        links = ET.parse(model).getroot().iter("link")
    except (ET.ParseError, OSError):
        return set()
    return {k.get("name") for k in links if not any(k.find(t) is not None for t in ("inertial", "visual", "collision"))}


def _how_masses(units, where, contract) -> str | None:
    names = list(units)
    if where.downloaded:
        return None  # no file of the person's to edit
    if where.model:
        if where.model.lower().endswith(".sdf"):
            what = f"In {where.model}: give {_list(names)} an <inertial> with a <mass>, a centre of mass and an inertia."
        else:
            what = (
                f"In {where.model}: give {_list(names)} an <inertial> with a <mass value=...>, "
                "an <origin> for the centre of mass and an <inertia>."
            )
        return where.edit(what)
    more = f", then the same for {_list(names[1:])}" if len(names) > 1 else ""
    return where.declare(f"model/links/{names[0]}/mass", "<mass, e.g. 1.5 kg>") + more


def _how_efforts(units, where, contract) -> str | None:
    names = list(units)
    if where.downloaded:
        return None
    if where.model:
        spot = "an <effort> inside its <axis><limit>" if where.model.lower().endswith(".sdf") else 'an effort="..." on its <limit>'
        return where.edit(f"In {where.model}: give {_list(names)} {spot} (N*m, from the motor datasheet).")
    more = f", then the same for {_list(names[1:])}" if len(names) > 1 else ""
    return where.declare(f"model/joints/{names[0]}/limits/effort", "<effort limit in N*m>") + more


def _how_meshes(units, where, contract) -> str | None:
    if not where.model or where.downloaded:
        return None  # a lock with no model file on disk, or a download, has nowhere to put them
    # the folder hint sits in the action (paths relative to the lock), never in the stored observed
    seen = re.search(r"expects (\S+) and (\S+?), next to", " ".join(f"{f.observed} {f.action}" for f in units.values()))
    if seen:
        what = (
            f"Put the mesh files where {where.model} looks for them ({seen[2]}), "
            f"in a folder that has a {Path(seen[1]).name}."
        )
    else:
        uris = sorted({m.uri for m in (contract.model.mesh_refs or [])}) if contract and contract.model else []
        absent = [u for u in uris if not _mesh_path(u, Path(where.model).parent).exists()]
        if uris and not absent:
            return None  # every mesh the model names is already on disk
        uris = absent
        named = f" ({_list(uris)})" if uris else ""
        what = f"Put the mesh files {where.model} names{named} at those paths; a plain name is read next to the model file."
    return where.edit(what)


def _how_contacts(units, where, contract) -> str | None:
    from lockwire.checks import _LEG_NAME

    parents = {j.parent for j in contract.model.joints}
    feet = [k.name for k in contract.model.links if _LEG_NAME.search(k.name) and k.name not in parents]
    row = f"contacts: [{{link: {feet[0] if feet else '<foot link>'}, xyz: [<x>, <y>, <z>]}}]"
    start = f"run {where.start()}, then " if where.bare else ""
    return (
        f"No command adds them yet: {start}add to {where.lock} a top-level contacts list with one row "
        f"per foot ({_list(feet, 6)}), for example {row} (metres, in that link's frame)."
    )


def _how_model_file(units, where, contract) -> str | None:
    if where.recorded is None:
        return None
    again = cli_line("lockwire", "start", "<your model file>", "--out", where.lock, "--force")
    return (
        f"Put the model file back at {where.recorded}, or re-create the lock from your copy: {again} "
        "(this replaces the lock, so entries you added to it are lost)."
    )


def _how_bus(units, where, contract) -> str | None:
    """Both numbers a bus needs, on the first device that sits on each bus."""
    steps = []
    for bus in units:
        if dev := next((d.id for d in contract.devices if d.identity and d.identity.bus_ref == bus), None):
            steps += [
                where.declare(f"devices/{dev}/driver/nominal_rate_hz", "<messages per second>"),
                where.declare(f"devices/{dev}/driver/payload_bytes", "<bytes per message>"),
            ]
    return " && ".join(steps) or None  # no device sits on any of these buses: nothing to give a driver to


def _how_ambient(units, where, contract) -> str | None:
    if not contract.devices:
        return None  # no device to hang a thermal ambient on
    return where.declare(f"devices/{contract.devices[0].id}/thermal/ambient", "<ambient temperature, e.g. 25 C>")


def _each(path: str, value: str):
    """The command for the first unit; the same for the others."""

    def how(units, where, contract):
        names = list(units)
        more = f", then the same for {_list(names[1:])}" if len(names) > 1 else ""
        return where.declare(path.format(names[0]), value) + more

    return how


def _how_rail(units, where, contract) -> str | None:
    """The rail's own measured voltage, never a number the rail name suggests; says what feeds it."""
    names = list(units)
    how = where.declare(f"topology/rails/{names[0]}/voltage", "<measured rail voltage in V>")
    rail = contract.rail(names[0])
    feed = contract.topology.battery if rail.upstream == "battery" else rail.upstream and contract.rail(rail.upstream)
    volts = feed.voltage.base() if feed and feed.voltage else None
    fed = f"; it is fed from {volts:g} V" if volts else ""
    more = f", then the same for {_list(names[1:])}" if len(names) > 1 else ""
    return f"{how}  # use the rail's real measured voltage{fed}{more}"  # a shell comment: the line pastes whole


_HOW = {
    "link_masses": _how_masses,
    "effort_limits": _how_efforts,
    "meshes": _how_meshes,
    "foot_contacts": _how_contacts,
    "model_file": _how_model_file,
    "battery_chemistry": _each("topology/battery/chemistry", "<li-ion, lipo, lifepo4, nimh or lead-acid>"),
    "battery_c_rate": _each("topology/battery/c_rate_continuous", "<continuous C rating, e.g. 20>"),
    "rail_voltages": _how_rail,
    "device_masses": _each("devices/{}/physical/mass", "<mass, e.g. 25 g>"),
    "bus_traffic": _how_bus,
    "ambient": _how_ambient,
}


def _covers(key: str, units: dict, contract) -> str:
    names = list(units)
    nouns = {"link_masses": ("link", "links"), "effort_limits": ("joint", "joints"),
             "device_masses": ("device", "devices"), "rail_voltages": ("rail", "rails")}
    if key in nouns:
        return _count(names, *nouns[key])
    if key == "bus_traffic":
        return "bus " + _list(names)
    if key == "meshes":
        n = len({m.uri for m in contract.model.mesh_refs or []}) if contract and contract.model else 0
        return f"{n} mesh file{'s' * (n != 1)}" if n else "the mesh files"
    covers = {"foot_contacts": "the feet", "model_file": "this lock's model", "ambient": "the robot, declared on any one device"}
    return covers.get(key, "the battery")


def _not_declared(contract, where, shown: set[str]) -> dict | None:
    """What the robot declares none of: a cheap look at the contract, never a new finding."""
    if contract is None:
        return None
    links = contract.model.links if contract.model else []
    battery = contract.topology.battery if contract.topology else None
    rails = contract.topology.rails if contract.topology else []
    if battery or rails or any(k.mass or k.payload for k in links):
        return None
    return {
        "names": ([] if "link_masses" in shown else ["any link mass"]) + ["a battery", "power rails", "a payload"],
        "start": [
            cli_line("lockwire", "add", "battery", where.lock, "--capacity", "<capacity, e.g. 50 Wh>", "--source", SOURCE, "--witness", WITNESS),
            cli_line("lockwire", "add", "rail", where.lock, "--id", "<rail name>", "--capacity", "<power, e.g. 60 W>", "--source", SOURCE, "--witness", WITNESS),
        ],
        "first": where.start() if where.bare else None,
    }


def missing_inputs(findings: Iterable[Finding], contract=None, given=None) -> dict | None:
    """The inputs the check could not use, in a fixed order of usefulness; None when nothing is missing.

    `given`: (the path the person gave, True when it is a model file with no lock), so every
    instruction names their own file.
    """
    where = _Where(contract, given)
    findings = list(findings)
    groups: dict[str, dict[str, Finding]] = {}
    for f in findings:
        for key, (_, _, rules) in _RULES.items():
            hit = next(
                (
                    m
                    for severity, quantity, phrase in rules
                    if severity == f.severity
                    and (m := re.fullmatch(quantity, f.quantity))
                    and re.search(phrase, f.observed)
                ),
                None,
            )
            if hit:
                groups.setdefault(key, {}).setdefault(hit.group(1) if hit.groups() else f.quantity, f)
                break
    if any(f.quantity.startswith("model/standing/") for f in findings):
        # the standing check ran from the feet it found by name: contacts are not missing
        groups.pop("foot_contacts", None)
    bare: list[str] = []  # links check warns have no mass that are bare frames: said, not asked for
    if where.model and "link_masses" in groups:
        frames = _frames(where.model)
        bare = [n for n in groups["link_masses"] if n in frames]
        groups["link_masses"] = {n: f for n, f in groups["link_masses"].items() if n not in frames}
    items = []
    for key, (name, unlocks, _) in _RULES.items():
        if not (units := groups.get(key)):
            continue
        how = _HOW[key](units, where, contract)
        if how is None:  # an instruction nobody can follow is not shown
            continue
        items.append(
            {
                "key": key,
                "name": name,
                "covers": _covers(key, units, contract)
                + (f", plus {_count(bare, 'bare frame', 'bare frames')} left out: {_list(bare)}" if key == "link_masses" and bare else ""),
                "unlocks": unlocks,
                "how": how,
                "lines": list(units)[:12],
            }
        )
    undeclared = _not_declared(contract, where, {i["key"] for i in items})
    if not items and not undeclared:
        return None
    return {"items": items, **({"not_declared": undeclared} if undeclared else {})}


def missing_lines(missing: dict | None, *, top: int | None = 3) -> list[str]:
    """The block `lockwire check` prints: the first `top` items (all when None), then "and N more"."""
    if not missing:
        return []
    items = missing["items"]
    shown = items if top is None else items[:top]
    lines = ["Missing inputs (what to add to get more from this check):"] if shown else []
    for i in shown:
        lines.append(f"  {i['name']} ({i['covers']}): then Lockwire can check {i['unlocks']}.")
        lines.append(f"    How: {i['how']}")
    if len(items) > len(shown):
        lines.append(f"  and {len(items) - len(shown)} more: run with --all")
    if undeclared := missing.get("not_declared"):
        first = f"First: {undeclared['first']}. " if undeclared.get("first") else ""
        battery, rail = undeclared["start"]
        lines.append(f"This robot declares none of: {', '.join(undeclared['names'])}.")
        lines.append(f"  {first}Start a battery: {battery}. Start a rail: {rail}.")
    return lines
