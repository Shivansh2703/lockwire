"""What is connected to what, and which parts a finding is about.

Pure and read-only: an edge exists only if a lockfile field says so, and a finding whose
quantity path names nothing known involves nothing. Graph order is declaration order
(links, joints, devices+connectors, hosts, buses, rails, battery, wires, code); `involved`
returns node ids sorted lexicographically.
"""

from __future__ import annotations

from typing import Any

from .schema import Contract

_BATTERY = "battery"


def robot_graph(contract: Contract) -> dict[str, list[dict[str, str]]]:
    nodes: dict[str, dict[str, str]] = {}
    edges: dict[tuple[str, str, str], dict[str, str]] = {}

    def node(kind: str, key: str, label: str | None = None) -> str:
        nid = f"{kind}:{key}" if kind != "battery" else _BATTERY
        nodes.setdefault(nid, {"id": nid, "kind": kind, "label": label or key})
        return nid

    def edge(a: str, b: str, kind: str) -> None:
        if a in nodes and b in nodes:
            edges.setdefault((a, b, kind), {"from": a, "to": b, "kind": kind})

    c, topo = contract, contract.topology
    model = c.model
    for link in model.links if model else []:
        node("link", link.name)
    for joint in model.joints if model else []:
        node("joint", joint.name)
    for d in c.devices:
        node("device", d.id)
        for k in d.connectors or []:
            node("connector", f"{d.id}/{k.id}", k.id)
    for h in topo.hosts if topo else []:
        node("host", h.id)
    for b in topo.buses if topo else []:
        node("bus", b.id)
    for r in topo.rails if topo else []:
        node("rail", r.id)
    if topo and topo.battery:
        node("battery", _BATTERY, "battery")
    for w in topo.wires if topo else []:
        node("wire", w.id)

    for j in model.joints if model else []:
        edge(f"link:{j.parent}", f"joint:{j.name}", "structure")
        edge(f"joint:{j.name}", f"link:{j.child}", "structure")
        if j.mimic:
            edge(f"joint:{j.mimic.joint}", f"joint:{j.name}", "depends")
    for d in c.devices:
        did = f"device:{d.id}"
        if d.drives_joint:
            edge(did, f"joint:{d.drives_joint}", "drives")
        if d.physical and d.physical.mount_frame:
            edge(did, f"link:{d.physical.mount_frame}", "mounts")
        if d.power:
            edge(f"rail:{d.power.rail}", did, "power")
        for cid in d.power_inputs or []:
            edge(f"connector:{d.id}/{cid}", did, "power")
        if d.identity:
            edge(did, f"bus:{d.identity.bus_ref}", "signal")
    for b in topo.buses if topo else []:
        edge(f"host:{b.host}", f"bus:{b.id}", "signal")
    for r in topo.rails if topo else []:
        rid = f"rail:{r.id}"
        up = r.upstream
        if up:
            edge(_BATTERY if up == _BATTERY else f"rail:{up}", rid, "power")
        for dev in (r.source_device, r.charged_by):
            if dev:
                edge(f"device:{dev}", rid, "power")
    for w in topo.wires if topo else []:
        wid, kind = f"wire:{w.id}", "power" if w.rail else "signal"
        if w.rail:
            edge(f"rail:{w.rail}", wid, "power")
        for end, outbound in ((w.from_, False), (w.to, True)):
            if end is None:
                continue
            cid = f"connector:{end.device}/{end.connector}"
            hub = cid if end.connector and cid in nodes else f"device:{end.device}"
            if hub == cid:
                edge(cid, f"device:{end.device}", kind)
            if kind == "power" or outbound:
                edge(wid, hub, kind)
            else:
                edge(hub, wid, kind)
    for e in c.edges:
        owner = _node_of(c, e.quantity)
        for path in e.code if owner else []:
            edge(node("code", path), owner, "depends")
    for d in c.devices:
        for path in d.depends:
            edge(node("code", path), f"device:{d.id}", "depends")
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def _name_in(path: str, prefix: str, names: list[str]) -> str | None:
    """The known name `path` is about, under `prefix` (names may contain '/')."""
    for n in sorted(names, key=len, reverse=True):
        if path == prefix + n or path.startswith(prefix + n + "/"):
            return n
    return None


def _node_of(c: Contract, path: str) -> str | None:
    topo, model = c.topology, c.model
    for prefix, kind, names in (
        ("model/links/", "link", [x.name for x in model.links] if model else []),
        ("model/joints/", "joint", [x.name for x in model.joints] if model else []),
        ("devices/", "device", [x.id for x in c.devices]),
        ("topology/rails/", "rail", [x.id for x in topo.rails] if topo else []),
        ("topology/wires/", "wire", [x.id for x in topo.wires] if topo else []),
        ("topology/buses/", "bus", [x.id for x in topo.buses] if topo else []),
    ):
        if (n := _name_in(path, prefix, names)) is not None:
            return f"{kind}:{n}"
    if (
        topo
        and topo.battery
        and (path == "topology/battery" or path.startswith("topology/battery/"))
    ):
        return _BATTERY
    return None


def involved(contract: Contract, finding: Any) -> list[str]:
    c, topo, q = contract, contract.topology, finding.quantity
    graph = robot_graph(c)
    known = {n["id"] for n in graph["nodes"]}
    links = {x.name: x for x in c.model.links} if c.model else {}
    joints = {x.name: x for x in c.model.joints} if c.model else {}
    out: set[str] = set()

    def device_on(rail: str):
        return [d for d in c.devices if d.power and d.power.rail == rail]

    def torque(joint: str) -> None:
        """The joint, its actuator, and everything it carries: distal links + devices on them."""
        j = joints[joint]
        out.add(f"joint:{joint}")
        out.update(f"device:{d.id}" for d in c.devices if d.drives_joint == joint)
        carried, todo = set(), [j.child]
        while todo:
            link = todo.pop()
            if link not in carried:
                carried.add(link)
                todo += [x.child for x in joints.values() if x.parent == link]
        out.update(f"link:{n}" for n in carried)
        out.update(
            f"device:{d.id}" for d in c.devices if d.physical and d.physical.mount_frame in carried
        )

    def rail(rid: str) -> None:
        """The rail, its supply chain to the battery, its wires and connectors, its loads."""
        seen, first = set(), rid
        while rid and rid not in seen and c.rail(rid):
            seen.add(rid)
            r = c.rail(rid)
            out.add(f"rail:{rid}")
            out.update(f"device:{x}" for x in (r.source_device, r.charged_by) if x)
            if r.upstream == _BATTERY:
                out.add(_BATTERY)
            rid = r.upstream if r.upstream != _BATTERY else None
        for w in topo.wires:
            if w.rail in seen:
                wire(w.id)
        out.update(f"device:{d.id}" for d in device_on(first) if first in seen)

    def wire(wid: str) -> None:
        w = c.wire(wid)
        out.add(f"wire:{wid}")
        if w.rail:
            out.add(f"rail:{w.rail}")
        for end in (w.from_, w.to):
            if end:
                out.add(f"device:{end.device}")
                if end.connector:
                    out.add(f"connector:{end.device}/{end.connector}")

    if (n := _name_in(q, "model/joints/", list(joints))) is not None:
        if q.endswith("/limits/effort"):
            torque(n)
        else:
            j = joints[n]
            out.update({f"joint:{n}", f"link:{j.parent}", f"link:{j.child}"})
    elif q.startswith("plan/segments/") and q.split("/hold_torque/", 1)[-1] in joints:
        torque(q.split("/hold_torque/", 1)[-1])
    elif (n := _name_in(q, "devices/", [d.id for d in c.devices])) is not None:
        d = c.device(n)
        out.add(f"device:{n}")
        if q == f"devices/{n}/drives_joint" and d.drives_joint in joints:
            torque(d.drives_joint)
        elif q == f"devices/{n}/physical/mount_frame" and d.physical:
            out.add(f"link:{d.physical.mount_frame}")
        elif q == f"devices/{n}/power/rail" and d.power:
            out.add(f"rail:{d.power.rail}")
    elif (n := _name_in(q, "model/links/", list(links))) is not None:
        out.add(f"link:{n}")
    elif topo and (n := _name_in(q, "topology/rails/", [r.id for r in topo.rails])) is not None:
        rail(n)
    elif topo and (n := _name_in(q, "topology/wires/", [w.id for w in topo.wires])) is not None:
        wire(n)
    elif topo and (n := _name_in(q, "topology/buses/", [b.id for b in topo.buses])) is not None:
        b = next(x for x in topo.buses if x.id == n)
        out.update({f"bus:{n}", f"host:{b.host}"})
        out.update(f"device:{d.id}" for d in c.devices if d.identity and d.identity.bus_ref == n)
    elif topo and topo.battery and q.startswith("topology/battery"):
        out.add(_BATTERY)
        out.update(f"rail:{r.id}" for r in topo.rails if r.upstream == _BATTERY)
    elif b := next(
        (x for x in c.budgets if q == f"budgets/{x.id}" or q.startswith(f"budgets/{x.id}/")), None
    ):
        if b.kind == "mass_total":
            if b.sources in ("links", "links+devices"):
                out.update(f"link:{k.name}" for k in links.values() if k.mass is not None)
            if b.sources in ("devices", "links+devices"):
                out.update(f"device:{d.id}" for d in c.devices if d.physical and d.physical.mass)
        elif b.kind == "rail_power" and b.rail:
            out.add(f"rail:{b.rail}")
            out.update(f"device:{d.id}" for d in device_on(b.rail))
        elif b.kind == "endurance":
            out.add(_BATTERY)
            out.update(f"device:{d.id}" for d in c.devices if d.power)
    return sorted(i for i in out if i in known)
