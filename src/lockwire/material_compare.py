"""CADN-matcompare: what one link would weigh, and how the torque check would read, in each catalog material.

Works on in-memory copies of the contract; nothing is written. Only a link whose mass is
derived from its shapes has a volume to scale.
"""

from __future__ import annotations

from lockwire import catalog, checks, schema
from lockwire.schema import Contract

PRINT_NOTE = "Printed materials are shown as solid 100 percent infill; real prints weigh less."


def _is_printed(m: schema.Material) -> bool:
    return "infill" in m.source  # the catalog's own caveat on a printed plastic's density


def compare_materials(contract: Contract, link: str) -> dict:
    """Per catalog material: the link's mass, the robot's total mass and the worst torque margin."""
    if contract.model is None or link not in {k.name for k in contract.model.links}:
        raise ValueError(f"No link named {link!r} in this lockfile.")
    original = next(k for k in contract.model.links if k.name == link)
    if "mass" not in original._derived:
        return {
            "link": link,
            "rows": [],
            "note": f"Link {link!r} has a declared or weighed mass, so there is no volume to scale.",
        }
    rows = []
    for mat in catalog.materials():
        c = contract.model_copy(deep=True)
        target = next(k for k in c.model.links if k.name == link)
        object.__setattr__(target, "material", mat)
        c._derive_masses()
        loads = checks.gravity_loads(c) or {}
        margins = {
            j.name: j.limits.effort_limit - loads[j.name]
            for j in c.model.joints
            if j.name in loads and j.limits is not None and j.limits.effort_limit is not None
        }
        worst = min(margins, key=margins.get) if margins else None
        rows.append(
            {
                "material": mat.name,
                "density_kg_m3": mat.density_kg_m3,
                "link_mass_kg": target.mass.base(),
                "total_mass_kg": sum(k.mass.base() for k in c.model.links if k.mass is not None),
                "worst_margin_nm": margins[worst] if worst else None,
                "worst_joint": worst,
                "current": original.material is not None and original.material.name == mat.name,
                "printed": _is_printed(mat),
            }
        )
    return {
        "link": link,
        "rows": rows,
        "note": PRINT_NOTE if any(r["printed"] for r in rows) else None,
    }
