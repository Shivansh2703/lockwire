"""`lockwire check-sim`: a simulator's model file read beside the lock, value by value.

Lockwire never runs a simulation; this only reads the sim file the way a
reviewer would and says where it agrees with the lock and where it does not
("sim file says 1.2 kg, lock says 0.9 kg"). Agreement is never a claim that the
sim matches the real robot. The failure it exists for: a vendor's sim
file kept old masses after their URDF changed.

Formats: MuJoCo MJCF (`<mujoco>` root) and SDF, stdlib XML only. MJCF semantics
from the MuJoCo XML reference (https://mujoco.readthedocs.io/en/stable/XMLreference.html,
read 2026-11-05): `body/inertial` has `mass`, `pos` (COM in the body frame),
`diaginertia` or `fullinertia` (order M11 M22 M33 M12 M13 M23); without an
inertial, mass and inertia are inferred from geoms (`compiler/inertiafromgeom`,
"true" overrides every inertial). `joint` has `type` (default hinge), `range`
(default "0 0" = no limit; hinge range is in `compiler/angle` units, default
DEGREE), `damping`, `frictionloss`, `armature`. Actuators (`motor`, `general`, ...)
name a `joint` and carry `gear` (first of 6), `ctrlrange`, `forcerange` (clamps the actuator
force; joint torque = force x gear, so effort = forcerange x gear[0]; `motor`, `general` and
`position` all take both). `<default>` classes (read 2026-11-05, same page, "default"): a child
element named for a tag sets that tag's attributes for elements with `class="..."`, else those
under a body's `childclass`, else the top-level (`main`) class; nested classes inherit their
parent's; explicit attributes win. `limited="false"` ignores `range`.
"""

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lockwire.api import verdict_document
from lockwire.cli_line import cli_line
from lockwire.report import Finding, RecordReport, ScoredRecord
from lockwire.schema import Contract
from lockwire.sdf import parse_sdf
from lockwire.urdf import _principal, _refuse_doctype_entity

ARTIFACT_TYPE = "lockwire-check-sim-report"
_SUFFIXES = ("_link", "_body", "_joint")
_COM_MM = 1.0  # COM agrees within 1 mm per axis
_DEFAULT_PCT = 1.0  # mass/inertia/limit tolerance when the lock declares none


@dataclass
class _Body:
    mass: float | None = None
    com: tuple[float, ...] | None = None
    moments: tuple[float, ...] | None = None
    why: str = "the sim file gives this body no mass"


@dataclass
class _Joint:
    lower: float | None = None
    upper: float | None = None
    effort: float | None = None
    velocity: float | None = None
    no_effort: str = "no actuator drives this joint in the sim file"
    extras: dict[str, float] = field(default_factory=dict)  # damping/friction/armature


def _floats(text: str | None) -> list[float]:
    return [float(x) for x in text.split()] if text else []


def _classes(root: ET.Element) -> dict[str, dict[str, dict[str, str]]]:
    """class name -> element tag -> attributes, each class inheriting its parent's."""
    out: dict[str, dict[str, dict[str, str]]] = {}

    def walk(default: ET.Element, inherited: dict[str, dict[str, str]]) -> None:
        mine = {t: dict(a) for t, a in inherited.items()}
        for child in default:
            if child.tag != "default":
                mine[child.tag] = {**mine.get(child.tag, {}), **child.attrib}
        out[default.get("class", "main")] = mine
        for child in default.findall("default"):
            walk(child, mine)

    for top in root.findall("default"):
        walk(top, {})
    return out


def _resolve(classes: dict, el: ET.Element, childclass: str | None) -> dict[str, str]:
    """el's attributes over its class's: `class`, else the enclosing body's `childclass`, else main."""
    base = classes.get(el.get("class") or childclass or "main", {}).get(el.tag, {})
    return {**base, **el.attrib}


def _joints(el: ET.Element, childclass: str | None):
    for child in el:
        if child.tag == "joint":
            yield child, childclass
        elif child.tag == "body":
            yield from _joints(child, child.get("childclass", childclass))
        elif child.tag == "worldbody":
            yield from _joints(child, childclass)


def _read_mjcf(root: ET.Element) -> tuple[dict[str, _Body], dict[str, _Joint], str]:
    compiler = root.find("compiler")
    degrees = compiler is None or compiler.get("angle", "degree") == "degree"
    from_geoms = compiler is not None and compiler.get("inertiafromgeom") == "true"
    classes = _classes(root)
    bodies: dict[str, _Body] = {}
    joints: dict[str, _Joint] = {}
    for body_el in root.iter("body"):
        inertial = body_el.find("inertial")
        if inertial is None or from_geoms:
            why = "mass from density" if body_el.find("geom") is not None else ""
            bodies[body_el.get("name", "?")] = _Body(why=why or _Body.why)
            continue
        diag, full = _floats(inertial.get("diaginertia")), _floats(inertial.get("fullinertia"))
        moments = sorted(diag) if diag else None
        if len(full) == 6:  # MJCF order M11 M22 M33 M12 M13 M23 -> _principal's order
            moments = list(
                _principal(full[0], full[3], full[4], full[1], full[5], full[2]).moments_kgm2
            )
        com = tuple(_floats(inertial.get("pos")) or (0.0, 0.0, 0.0))
        bodies[body_el.get("name", "?")] = _Body(
            float(inertial.get("mass", "0")), com, moments and tuple(moments)
        )
    for raw, childclass in _joints(root, None):
        joint_el = _resolve(classes, raw, childclass)
        if joint_el.get("name") is None or joint_el.get("type") == "free":
            continue
        joint = joints.setdefault(joint_el["name"], _Joint())
        lo_hi = _floats(joint_el.get("range"))
        if lo_hi and lo_hi != [0.0, 0.0] and joint_el.get("limited") != "false":
            scale = math.pi / 180 if degrees and joint_el.get("type", "hinge") == "hinge" else 1.0
            joint.lower, joint.upper = lo_hi[0] * scale, lo_hi[1] * scale
        for attr, key in (
            ("damping", "damping"),
            ("frictionloss", "friction"),
            ("armature", "armature"),
        ):
            if float(joint_el.get(attr, "0")):
                joint.extras[key] = float(joint_el.get(attr, "0"))
    for raw in root.findall("actuator/*"):
        act = _resolve(classes, raw, None)
        joint = joints.get(act.get("joint", ""))
        if joint is None:
            continue
        force, ctrl = _floats(act.get("forcerange")), _floats(act.get("ctrlrange"))
        gear = abs((_floats(act.get("gear")) or [1.0])[0])  # first of 6 = the joint transmission
        if force and force != [0.0, 0.0] and raw.tag in ("motor", "general", "position"):
            joint.effort = max(abs(x) for x in force) * gear  # joint torque = force * gear
        elif raw.tag in ("motor", "general") and ctrl and ctrl != [0.0, 0.0]:
            joint.effort = max(abs(x) for x in ctrl) * gear
        else:
            joint.no_effort = f"<{raw.tag}> actuator declares no force limit"
    return bodies, joints, "MJCF"


def _read_sdf(path: Path, root: ET.Element) -> tuple[dict[str, _Body], dict[str, _Joint], str]:
    model = parse_sdf(path)
    bodies = {
        link.name: _Body(
            link.mass.base() if link.mass else None,
            link.com,
            link.inertia.moments_kgm2 if link.inertia else None,
        )
        for link in model.links
    }
    joints: dict[str, _Joint] = {}
    for joint in model.joints:
        lim = joint.limits
        effort = lim.effort if lim and lim.effort and lim.effort > 0 else None  # SDF -1 = none
        joints[joint.name] = _Joint(
            lim and lim.lower,
            lim and lim.upper,
            effort,
            lim and lim.velocity,
            "the SDF gives no effort",
        )
    for joint_el in root.iter("joint"):
        dyn = joint_el.find("axis/dynamics")
        for key in ("damping", "friction"):
            value = float(dyn.findtext(key) or 0) if dyn is not None else 0.0
            if value and joint_el.get("name") in joints:
                joints[joint_el.get("name", "")].extras[key] = value
    return bodies, joints, "SDF"


def read_sim(path: Path) -> tuple[dict[str, _Body], dict[str, _Joint], str]:
    raw = path.read_bytes()
    _refuse_doctype_entity(raw, path)
    root = ET.fromstring(raw)
    if root.tag == "mujoco":
        return _read_mjcf(root)
    if root.tag == "sdf":
        return _read_sdf(path, root)
    raise ValueError(f"{path}: root is <{root.tag}>; check-sim reads MJCF <mujoco> or SDF <sdf>")


def _match(sim: list[str], lock: list[str]) -> tuple[dict[str, str], list[str], list[str]]:
    """sim name -> lock name: exact, then case-insensitive, then a common suffix stripped."""

    def stripped(name: str) -> str:
        low = name.lower()
        return next((low[: -len(s)] for s in _SUFFIXES if low.endswith(s)), low)

    pairs: dict[str, str] = {}
    for key in (str, str.lower, stripped):
        free = [n for n in lock if n not in pairs.values()]
        index: dict[str, list[str]] = {}
        for name in free:
            index.setdefault(key(name), []).append(name)
        for name in sim:
            hits = index.get(key(name), [])
            if name not in pairs and len(hits) == 1:  # two candidates is a guess: leave it
                pairs[name] = hits[0]
                index[key(name)] = []
    return pairs, [n for n in sim if n not in pairs], [n for n in lock if n not in pairs.values()]


def _cannot_express(c: Contract, sim_bodies: set[str], fmt: str) -> list[dict[str, str]]:
    rows = []
    topo = c.topology
    if topo and topo.rails:
        rows.append(("topology/rails", f"{len(topo.rails)} power rail(s)"))
    if topo and topo.battery:
        rows.append(("topology/battery", f"battery {topo.battery.capacity}"))
    if topo and topo.buses:
        rows.append(("topology/buses", f"{len(topo.buses)} data bus(es) and their loads"))
    powered = [d.id for d in c.devices if d.power]
    if powered:
        rows.append(("devices/power", f"power draw of {len(powered)} device(s)"))
    if any(d.thermal for d in c.devices):
        rows.append(("devices/thermal", "thermal limits"))
    if c.gearboxes or c.actuator_cases:
        rows.append(("actuators", "actuator electrical model (gearboxes, motor cases)"))
    if fmt == "MJCF" and c.model and any(j.limits and j.limits.velocity for j in c.model.joints):
        rows.append(("model/joints/velocity", "joint velocity limits (MJCF has no field for them)"))
    for d in c.devices:
        phys = d.physical
        if phys and phys.mass and phys.mount_frame and d.id not in sim_bodies:
            rows.append(
                (
                    f"devices/{d.id}/physical/mass",
                    f"{d.id} ({phys.mass}) mounted on {phys.mount_frame}",
                )
            )
    return [{"quantity": q, "lock_has": what} for q, what in rows]


def check_sim(sim: Path, c: Contract, lock: Path) -> tuple[RecordReport, list[dict[str, str]]]:
    bodies, joints, fmt = read_sim(sim)
    again = cli_line("lockwire", "check-sim", sim, "--lock", lock)
    findings: list[Finding] = []
    passed: list[ScoredRecord] = []
    keys: set[str] = set()

    def row(severity: str, quantity: str, observed: str, expected: str, action: str) -> None:
        findings.append(
            Finding(
                severity=severity,  # type: ignore[arg-type]
                quantity=quantity,
                observed=observed,
                expected=expected,
                source=str(sim),
                action=action,
                next_command=again,
                next_tool="check_sim_config",
            )
        )

    def compare(
        quantity: str, element: str, sim_v: float, lock_v: float, unit: str, pct: float
    ) -> None:
        keys.add(quantity)
        ok = abs(sim_v - lock_v) <= abs(lock_v) * pct / 100 + 1e-12
        said = f"sim file says {sim_v:.6g} {unit}".rstrip()
        has = f"lock says {lock_v:.6g} {unit}".rstrip()
        if ok:
            passed.append(
                ScoredRecord(
                    quantity=quantity, observed=said, expected=has, margin=f"within {pct:g} %"
                )
            )
        else:
            row(
                "violation",
                quantity,
                f"{fmt} {element}: {said}",
                has,
                f"the sim file and the lock disagree by more than {pct:g} %; fix whichever is stale",
            )

    model = c.model
    links = {link.name: link for link in model.links} if model else {}
    pairs, sim_only, lock_only = _match(list(bodies), list(links))
    tol = {e.quantity: e.tolerance.rel_pct for e in c.edges if e.tolerance.rel_pct is not None}
    for sim_name, name in pairs.items():
        body, link, base = bodies[sim_name], links[name], f"model/links/{name}"
        el = f"<body name={sim_name!r}>" if fmt == "MJCF" else f"<link name={sim_name!r}>"
        pct = tol.get(f"{base}/mass", _DEFAULT_PCT)
        if link.mass is None:
            continue
        if body.mass is None:
            row(
                "not_scored",
                f"{base}/mass",
                f"not compared: {body.why}",
                f"lock says {link.mass}",
                "give the sim body an explicit <inertial mass=...> to compare it",
            )
            continue
        compare(f"{base}/mass", el, body.mass, link.mass.base(), "kg", pct)
        if link.com and body.com:
            q = f"{base}/com"
            keys.add(q)
            off = max(abs(a - b) for a, b in zip(body.com, link.com)) * 1000
            said = "sim file says COM " + " ".join(f"{v * 1000:.4g}" for v in body.com) + " mm"
            has = "lock says COM " + " ".join(f"{v * 1000:.4g}" for v in link.com) + " mm"
            if off <= _COM_MM:
                passed.append(
                    ScoredRecord(
                        quantity=q, observed=said, expected=has, margin=f"within {_COM_MM:g} mm"
                    )
                )
            else:
                row(
                    "violation",
                    q,
                    f"{fmt} {el}: {said}",
                    has,
                    f"COM differs by {off:.3g} mm; fix whichever is stale",
                )
        if link.inertia and body.moments:
            for i, (s, lk) in enumerate(zip(sorted(body.moments), link.inertia.moments_kgm2)):
                compare(f"{base}/inertia/{i}", el, s, lk, "kg m^2", pct)
    locked_joints = {j.name: j for j in model.joints} if model else {}
    jpairs, sim_joints_only, lock_joints_only = _match(list(joints), list(locked_joints))
    damping = {d.joint: d for d in (c.simulation.damping if c.simulation else []) if d.joint}
    for sim_name, name in jpairs.items():
        joint, lim, base = joints[sim_name], locked_joints[name].limits, f"model/joints/{name}"
        el = f"<joint name={sim_name!r}>"
        for what in ("lower", "upper", "effort", "velocity"):
            lock_v, sim_v = getattr(lim, what) if lim else None, getattr(joint, what)
            if lock_v is None or (what == "velocity" and fmt == "MJCF"):
                continue
            if sim_v is None:
                reason = (
                    joint.no_effort if what == "effort" else f"the sim file sets no {what} limit"
                )
                row(
                    "not_scored",
                    f"{base}/{what}",
                    f"not compared: {reason}",
                    f"lock says {lock_v:g}",
                    f"declare the {what} limit in the sim file to compare it",
                )
            else:
                compare(f"{base}/{what}", el, sim_v, lock_v, "", _DEFAULT_PCT)
        for key, sim_v in joint.extras.items():
            locked = damping.get(name) if key == "damping" else None
            quantity = locked and (locked.rotational or locked.translational)
            if quantity is not None:
                compare(f"{base}/{key}", el, sim_v, quantity.base(), "", _DEFAULT_PCT)
            else:
                row(
                    "not_scored",
                    f"{base}/{key}",
                    f"sim declares {key} {sim_v:g}, lock has no value",
                    "no locked value",
                    f"lock a {key} value to compare it",
                )
    for kind, names in (
        ("sim body", sim_only),
        ("sim joint", sim_joints_only),
        ("locked link", lock_only),
        ("locked joint", lock_joints_only),
    ):
        for name in names:
            side = "the lock" if kind.startswith("sim") else "the sim file"
            row(
                "not_scored",
                f"sim/unmatched/{kind.replace(' ', '_')}/{name}",
                f"not compared: {kind} {name!r} has no match in {side}",
                "a name match",
                "rename one side so the names match; check-sim never guesses a pairing",
            )
    report = RecordReport(
        findings, scored=len(keys), scored_keys=frozenset(keys), scored_records=passed
    )
    return report, _cannot_express(c, set(bodies), fmt)


def sim_document(sim: Path, c: Contract, lock: Path) -> tuple[RecordReport, dict[str, Any]]:
    """The one document both doors (CLI `--json`, MCP `check_sim_config`) return."""
    report, cannot = check_sim(sim, c, lock)
    document = verdict_document(c, report, ARTIFACT_TYPE)
    document["sim_file"] = str(sim)
    document["cannot_express"] = cannot
    return report, document
