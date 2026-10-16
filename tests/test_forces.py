"""Force numbers with their kinds.

Two anchors sit outside the implementation, because check, describe, the
library and the payload all share one code path and would agree while all being
wrong (PARITY-ANCHOR law):

* the two-mass fixture's CG is a **hand-computed literal** with the arithmetic
  in the docstring, not the check's own output;
* the closed-form gate below reproduces textbook cases from
  the solver-choice design note — the expected numbers come from the
  formulas in that table, not from this solver.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from lockwire import api
from lockwire.forces import (
    _KIND_BY_SOURCE,
    force_document,
    kind_for_source,
    number,
    render_markdown,
)
from lockwire.schema import load_contract
from lockwire.statics import Indeterminate, support_reactions, weight_n

FIXTURES = Path(__file__).parent / "fixtures"
TWO_MASS = FIXTURES / "twomass" / "lockwire.yaml"
THREE_MOUNTS = FIXTURES / "twomass" / "three_mounts.yaml"


def _every_figure(node: Any) -> list[dict[str, Any]]:
    """Every figure in the document — a dict is a figure when it carries a value."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "value" in node and "unit" in node:
            found.append(node)
        for child in node.values():
            found.extend(_every_figure(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_every_figure(child))
    return found


# ---------------------------------------------------------------- the gate
# Stage one must clear its own closed-form gate before it scores anything
# . The three cases are the textbook ones, in reaction form, with g = 9.81.


def test_the_closed_form_gate_holds_before_anything_is_scored():
    """Textbook case 3: 10 kg at 0.5 m on two bolts 0.2 m apart.

    ``W = 10 * 9.81 = 98.1 N``; ``R_far = W*a/d = 98.1*0.5/0.2 = 245.25 N``;
    ``R_near = W - R_far = -147.15 N`` — the near bolt pulls down.
    """
    load = weight_n(10.0)
    assert load == pytest.approx(98.1)
    near, far = support_reactions(load, 0.5, [0.0, 0.2])
    assert far[0] == pytest.approx(245.25, rel=1e-12)
    assert near[0] == pytest.approx(-147.15, rel=1e-12)
    assert (near[1], far[1]) == (0.0, 0.0)

    # Textbook case 1, in reaction form: a 100 N load 1 m out on one fixed support
    # reacts the whole load and a root moment of P*L = 100 N*m.
    (only,) = support_reactions(100.0, 1.0, [0.0])
    assert only == (100.0, 100.0)


def test_three_supports_are_refused_rather_than_split():
    with pytest.raises(Indeterminate) as refusal:
        support_reactions(100.0, 0.5, [0.0, 0.5, 1.0])
    assert "stiffness" in str(refusal.value)


# ---------------------------------------------------------------- the claims


def test_every_force_number_carries_its_kind():
    """Every figure the payload carries, on every path, went through ``number()``.

    The library door is exercised here (``api.check``); the CLI writes the same
    document from the same builder. The claim is *every*, so it walks the whole
    document recursively rather than checking named keys.
    """
    document = api.check(TWO_MASS)["forces"]
    figures = _every_figure(document)
    assert len(figures) >= 9, figures
    missing = [figure for figure in figures if "kind" not in figure]
    assert not missing, f"figures with no kind: {missing}"
    unknown = [
        figure
        for figure in figures
        if figure["kind"] not in set(_KIND_BY_SOURCE.values()) | {"not_scored"}
        and not figure["kind"].startswith("derived-by-")
    ]
    assert not unknown, f"figures with an uninterpretable kind: {unknown}"

    # And the builder itself refuses a kind nobody can read.
    with pytest.raises(ValueError):
        number(1.0, "kg", "vibes")


def test_cg_matches_the_hand_computed_value_for_the_two_mass_fixture():
    """dev.fore 2 kg at x=0, dev.aft 6 kg at x=1.

    total = 2 + 6 = 8 kg; CG_x = (2*0.0 + 6*1.0) / 8 = 0.75 m exactly (both
    masses and both offsets are exact binary fractions, so no tolerance is
    needed). This literal is the anchor: it does not come from the check.
    """
    document = api.check(TWO_MASS)["forces"]
    assert document["cg"]["value"] == [0.75, 0.0, 0.0]
    assert document["cg"]["unit"] == "m"
    assert document["mass_total"]["value"] == 8.0
    # The reaction at the single mount is that same hand-computed CG at work:
    # W = 8 * 9.81 = 78.48 N, moment about `base` = 78.48 * 0.75 = 58.86 N*m.
    (mount,) = document["mounts"]
    assert mount["reaction_n"] == pytest.approx(78.48, rel=1e-12)
    assert mount["moment_nm"] == pytest.approx(58.86, rel=1e-12)


def test_a_catalog_figure_is_never_labelled_measured():
    """A catalog figure carries its own word, never a measurement's.

    Ceiling, stated because the test's name is a claim: no contract field
    sources a mass from a part catalog today (the catalog is not wired in yet),
    so this guards the single mapping every kind is drawn through rather than a
    live catalog number. A catalog mass added later cannot be spelled
    "measured" without going through here.
    """
    assert kind_for_source("catalog") == "catalog"
    assert kind_for_source("estimate") == "projected"
    assert kind_for_source("recording") == "measured"
    assert kind_for_source("contract") == "declared"
    for source, kind in _KIND_BY_SOURCE.items():
        if source != "recording":
            assert kind != "measured", f"{source!r} must not be labelled measured"


def test_an_indeterminate_mount_set_is_not_scored_and_names_why():
    """Three mounts on one rigid body: NOT SCORED, with the missing input named."""
    document = api.check(THREE_MOUNTS)["forces"]
    mounts = document["mounts"]
    assert [mount["id"] for mount in mounts] == ["mount_a", "mount_b", "mount_c"]
    for mount in mounts:
        assert mount["kind"] == "not_scored"
        assert mount["value"] is None
        assert "reaction_n" not in mount
        because = mount["not_scored_because"]
        assert "indeterminate" in because
        assert "stiffness" in because and "section" in because


def test_a_link_with_no_declared_mass_is_not_scored_rather_than_assumed_zero():
    """An undeclared mass is a coverage gap, never a 0 kg contributor."""
    contract = load_contract(THREE_MOUNTS)
    contract.devices[0].physical.mass = None  # type: ignore[union-attr]
    from lockwire.checks import compute_system_cg

    document = force_document(contract, compute_system_cg(contract))
    rows = {row["id"]: row for row in document["masses"]}
    gap = rows["devices/dev.a/physical/mass"]
    assert gap["kind"] == "not_scored"
    assert gap["value"] is None
    assert "not summed as 0 kg" in gap["not_scored_because"]
    # The total sums only the declared masses (6.0, not 8.0 padded with a
    # zero) — but it is NOT a clean 6 kg answer, and it must not print as one:
    # dev.a's mass is unknown, not zero, so the number carries the gap.
    assert document["mass_total"]["value"] == 6.0
    assert document["mass_total"]["complete"] is False
    assert "dev.a" in document["mass_total"]["limitation"]
    assert "no declared mass" in document["mass_total"]["limitation"]
    rendered = "\n".join(render_markdown(document))
    assert "INCOMPLETE" in rendered


def test_an_incomplete_cg_prints_marked_rather_than_as_a_plain_figure():
    """A massed but unpositioned device leaves `complete=False`.

    The CG value is still the best available number (positioned contributors
    only) but must not read like a clean, whole figure — same defect class as
    the mass-total bounce, on the position side rather than the mass side.
    """
    contract = load_contract(TWO_MASS)
    contract.devices[0].physical.mount_xyz = None  # type: ignore[union-attr]
    from lockwire.checks import compute_system_cg

    document = force_document(contract, compute_system_cg(contract))
    assert document["cg"]["complete"] is False
    assert document["cg"]["limitation"]
    rendered = "\n".join(render_markdown(document))
    assert "INCOMPLETE" in rendered
    # And the mount reaction built on that incomplete CG correctly refuses,
    # naming the same gap rather than scoring a number off a partial CG.
    (mount,) = document["mounts"]
    assert mount["kind"] == "not_scored"
    assert "incomplete" in mount["not_scored_because"]


def test_forces_reads_and_renders_the_declared_mount_shape(tmp_path):
    """The force lane consumes the shape stored on ``Physical``."""
    contract = tmp_path / "lockwire.yaml"
    shutil.copy(TWO_MASS, contract)
    api.mount_shape(
        contract,
        "dev.fore",
        {
            "kind": "custom-mesh",
            "dimensions_mm": [80.0, 40.0, 2.5],
            "fastener_pattern": "4x M3",
            "mesh_path": "cad/fore-mount.stl",
        },
        source="cad/fore-mount.step",
        witness="Maya Chen",
    )

    document = api.check(contract)["forces"]
    assert document["mount_shapes"] == [
        {
            "id": "dev.fore",
            "kind": "custom-mesh",
            "dimensions": {
                "value": [80.0, 40.0, 2.5],
                "unit": "mm",
                "kind": "declared",
            },
            "fastener_pattern": "4x M3",
            "mesh_path": "cad/fore-mount.stl",
        }
    ]
    rendered = "\n".join(render_markdown(document))
    assert (
        "mount shape dev.fore: custom-mesh, (80.000, 40.000, 2.500) mm "
        "(kind: declared), fasteners 4x M3, mesh cad/fore-mount.stl"
    ) in rendered
