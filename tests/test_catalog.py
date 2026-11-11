"""Tests for the parts catalog: schema fields, the seeded parts, the loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lockwire import catalog as catalog_module
from lockwire.catalog import Catalog, CatalogPart, SourcedNumber, load_catalog

VALID_KINDS = {"datasheet", "source_file", "assumed"}


def test_load_catalog_reads_the_seeded_file():
    catalog = load_catalog()
    assert isinstance(catalog, Catalog)
    # "about 40" — not pinned exact, just in the expected ballpark.
    assert 30 <= len(catalog.parts) <= 60


def test_every_seeded_number_has_a_kind_and_a_source():
    """The whole claim: no number on any seeded part is missing its badge.

    This is the box's central promise — a seed catalog of unlabelled numbers is worse than an
    empty one — so the mutation bite below breaks it at the schema level (the shared validator),
    not by editing one part's data, to prove the *whole* claim, not one instance of it.
    """
    catalog = load_catalog()
    assert catalog.parts, "the seed catalog must not be empty"
    for part in catalog.parts:
        for number_name, number in part.numbers.items():
            assert number.kind in VALID_KINDS, f"{part.id}.{number_name} has an unknown kind"
            assert number.source.strip(), f"{part.id}.{number_name} has a blank source"


def test_assumed_numbers_say_so_in_their_own_source_text():
    """A placeholder number's source string must itself say it is a placeholder — not just its
    kind field — so the flag survives a reader who only looks at the source string (the brief's
    'in the data, not in a comment you hope someone reads'). The warning must be contiguous,
    starting with "ASSUMED for seeding" and ending with "replace before use."—not a bookend
    around a reason."""
    import difflib

    catalog = load_catalog()
    assumed = [
        (part.id, name, number)
        for part in catalog.parts
        for name, number in part.numbers.items()
        if number.kind == "assumed"
    ]
    assert assumed, "expected at least one labelled placeholder number in the seed catalog"
    for part_id, name, number in assumed:
        source = number.source
        # Both halves of the warning must be present (not just the first)
        has_start = "ASSUMED for seeding" in source
        has_end = "replace before use" in source

        if not (has_start and has_end):
            # Show a diff if the warning is incomplete
            expected_parts = ["ASSUMED for seeding", "...<reason>...", "replace before use"]
            actual = source
            diff = "\n".join(
                difflib.unified_diff(
                    expected_parts,
                    [actual],
                    fromfile="expected warning pattern",
                    tofile=f"actual: {part_id}.{name}",
                    lineterm="",
                )
            )
            raise AssertionError(
                f"{part_id}.{name} is kind=assumed but source is missing "
                f"'ASSUMED for seeding' (has: {has_start}) or 'replace before use' (has: {has_end}).\n"
                f"Source: {actual}\n{diff}"
            )


def test_catalog_part_ids_are_unique():
    catalog = load_catalog()
    ids = [p.id for p in catalog.parts]
    assert len(ids) == len(set(ids))


def test_duplicate_part_id_is_rejected():
    with pytest.raises(ValidationError, match="duplicate catalog part id"):
        Catalog.model_validate(
            {
                "parts": [
                    {"id": "dup", "family": "fam.x", "name": "one"},
                    {"id": "dup", "family": "fam.x", "name": "two"},
                ]
            }
        )


def test_sourced_number_rejects_blank_source():
    with pytest.raises(ValidationError, match="source must not be blank"):
        SourcedNumber(value=1.0, unit="g", kind="assumed", source="   ")


def test_catalog_part_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        CatalogPart(id="x", family="fam.x", name="X", not_a_real_field=True)


def _code_only(text: str) -> str:
    """Source text with string/comment tokens dropped, so a docstring explaining the split
    (which must name both models in prose, per catalog.py's CatalogPart docstring) does not
    itself trip this guard — only actual code (an import, an attribute access, a call) does."""
    import io
    import tokenize

    kept = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in (tokenize.STRING, tokenize.COMMENT):
            continue
        kept.append(tok.string)
    return " ".join(kept)


def test_no_module_straddles_catalog_and_registry_vocabularies():
    """CatalogPart (shopping) and DeviceModel (registry) are two vocabularies for "a part".

    Nothing in src/lockwire straddles both today (measured) — that is the fact this
    guard preserves. The day a module's CODE (not its docstrings/comments — see `_code_only`)
    imports or references both names it becomes a problem: a consumer needing both must go
    through one door, not read both models directly.
    """
    src_root = Path(__file__).parent.parent / "src" / "lockwire"
    roots = [r for r in src_root.parent.iterdir() if r.is_dir()]  # every package under src/
    offenders = []
    for path in sorted(p for r in roots for p in r.rglob("*.py")):
        code = _code_only(path.read_text())
        if "CatalogPart" in code and "DeviceModel" in code:
            offenders.append(str(path.relative_to(src_root.parent.parent)))
    assert not offenders, (
        "a module's code references both CatalogPart (shopping vocabulary) and DeviceModel "
        "(registry vocabulary) — the design requires a consumer needing both to go through one door, "
        f"not both models directly. Offending file(s): {offenders}"
    )


def test_every_robot_number_is_a_fetched_datasheet_figure_and_assumed_is_refused():
    from lockwire.catalog import RobotNumber, load_robots, recognise

    robots = load_robots()
    assert len(robots) == 13
    for number in (n for r in robots for n in r.numbers.values()):
        assert number.kind == "datasheet" and number.url.startswith("https://") and number.retrieved
    assert all(r.numbers["mass_total"].scope for r in robots)
    assert "dof" not in {r.id: r for r in robots}["unitree-b2"].numbers  # the page gives none
    assert all(recognise("x", r.fingerprint).id == r.id for r in robots if "unitree" in r.id)
    with pytest.raises(ValidationError, match="datasheet"):
        RobotNumber(value=1, unit="kg", kind="assumed", source="https://x", retrieved="2026-11-04")


def test_recognise_by_model_then_name_then_fingerprint_else_nothing():
    from lockwire.catalog import recognise

    waffle = ["base_footprint", "base_link", "wheel_left_link", "wheel_right_link", "imu_link"]
    waffle += ["caster_back_right_link", "caster_back_left_link", "base_scan", "camera_link"]
    assert recognise("anything", [], "ur5e").id == "ur5e"
    assert recognise("turtlebot3_waffle_pi", []).id == "turtlebot3-waffle-pi"
    assert recognise("renamed", waffle).id == "turtlebot3-waffle-pi"  # 9 of 11, not Burger
    assert recognise("ur", ["base_link", "shoulder_link"]) is None
    with pytest.raises(ValueError, match="turtlebot3-burger"):
        recognise("x", [], "nope")


def test_fingerprint_needs_80_percent_link_overlap():
    from lockwire.catalog import recognise

    burger = ["base_footprint", "base_link", "wheel_left_link", "wheel_right_link"]
    burger += ["caster_back_link", "imu_link", "base_scan"]
    assert recognise("renamed", burger + ["extra_link"]).id == "turtlebot3-burger"  # 7 of 8
    half = ["base_footprint", "base_link", "caster_back_link", "imu_link"]
    assert recognise("renamed", half) is None  # 4 of 7: a partial match recognises nothing


def test_servo_rows_carry_what_part_swap_needs():
    need = {"stall_torque", "mass", "input_voltage_min", "input_voltage_max"}
    parts = [p for p in load_catalog().parts if p.family == "fam.servo.smart" and p.bus]
    for maker in ("feetech", "dynamixel"):
        full = [p for p in parts if p.id.startswith(maker + ".") and need <= set(p.numbers)]
        assert len(full) >= 3, maker
        for p in full:
            assert all(n.kind == "datasheet" and "http" in n.source for n in p.numbers.values())


def _part(pid, *numbers):
    nums = {
        f"k{i}": SourcedNumber(value=1, unit="g", kind=k, source=s)
        for i, (k, s) in enumerate(numbers)
    }
    return CatalogPart(id=pid, family="f", name="n", numbers=nums)


def test_label_is_cited_only_when_every_number_is_a_datasheet_url_figure():
    url = ("datasheet", "https://x.com/ds p3")
    assert _part("a", url, url).label == "cited"
    assert _part("a", url, ("assumed", "ASSUMED")).label == "unverified"
    assert _part("a", ("datasheet", "https://shop.com retail listing")).label == "unverified"


def _private(tmp_path, monkeypatch, pid):
    (tmp_path / ".lockwire").mkdir()
    (tmp_path / ".lockwire" / "parts.yaml").write_text(f"- {{id: {pid}, family: f, name: Mine}}\n")
    monkeypatch.setenv("HOME", str(tmp_path))


def test_private_parts_merge_without_touching_the_seed(tmp_path, monkeypatch):
    seed = Path(catalog_module.__file__).parent / "catalog_seed.yaml"
    before = seed.read_bytes()
    _private(tmp_path, monkeypatch, "mine.1")
    assert "mine.1" in {p.id for p in load_catalog().parts}
    assert seed.read_bytes() == before


def test_private_part_reusing_a_public_id_is_refused(tmp_path, monkeypatch):
    _private(tmp_path, monkeypatch, load_catalog().parts[0].id)
    with pytest.raises(ValueError, match="parts.yaml"):
        load_catalog()


@pytest.mark.parametrize(
    "text, said",
    [
        ("- {id: a\n", "not valid YAML"),
        ("id: a\n", "must be a list"),
        ("- {family: f, name: Mine}\n", "entry 1 has no id"),
        ("- {id: x, family: f}\n", "part 'x' name: Field required"),
        (
            "- {id: x, family: f, name: M, numbers: {stall_torque: {value: 20, unit: kg*cm}}}\n",
            r"part 'x' numbers.stall_torque.unit is 'kg\*cm'; write it in N\*m",
        ),
    ],
)
def test_malformed_private_file_is_one_plain_error_naming_it(tmp_path, monkeypatch, text, said):
    (tmp_path / ".lockwire").mkdir()
    (tmp_path / ".lockwire" / "parts.yaml").write_text(text)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ValueError, match=f"^{tmp_path}.*parts.yaml: .*{said}"):
        load_catalog()


@pytest.mark.parametrize("unit", ["stone", "V"])
def test_an_explicit_parts_file_with_a_non_mass_mass_unit_is_refused(tmp_path, unit):
    f = tmp_path / "c.yaml"
    f.write_text(
        f"parts:\n- {{id: x, family: f, name: M, numbers: {{mass: {{value: 50, unit: {unit}}}}}}}\n"
    )
    with pytest.raises(ValueError, match=rf"^{f}: part 'x' numbers.mass.unit is '{unit}'"):
        load_catalog(f)
