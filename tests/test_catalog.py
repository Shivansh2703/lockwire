"""Tests for the parts catalog: schema fields, the seeded parts, the loader."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from lockwire import catalog as catalog_module
from lockwire import schema
from lockwire.catalog import Catalog, CatalogPart, SourcedNumber, belt_pitch, load_catalog

VALID_KINDS = {"datasheet", "source_file", "assumed"}


def test_load_catalog_reads_the_seeded_file():
    catalog = load_catalog()
    assert isinstance(catalog, Catalog)
    # "about 40" — not pinned exact, just in the expected ballpark.
    assert 30 <= len(catalog.parts) <= 260


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
    assert len(robots) == 16
    for number in (n for r in robots for n in r.numbers.values()):
        assert number.kind == "datasheet" and number.url.startswith("https://") and number.retrieved
    assert all(r.numbers["mass_total"].scope for r in robots)
    assert "dof" not in {r.id: r for r in robots}["unitree-b2"].numbers  # the page gives none
    assert all(recognise("x", r.fingerprint).id == r.id for r in robots if "unitree" in r.id)
    with pytest.raises(ValidationError, match="datasheet"):
        RobotNumber(value=1, unit="kg", kind="assumed", source="https://x", retrieved="2026-11-04")


def test_arm_robots_load_with_cited_mass_scope():
    from lockwire.catalog import load_robots

    by_id = {r.id: r for r in load_robots()}
    assert {"ur16e", "ur20", "kinova-gen3-7dof"} <= by_id.keys()
    for rid in ("ur16e", "ur20", "kinova-gen3-7dof"):
        assert by_id[rid].numbers["mass_total"].scope
        assert all(n.url.startswith("https://") for n in by_id[rid].numbers.values())


def test_robots_recognise_gen3_by_its_links():
    from lockwire.catalog import recognise

    gen3_links = [
        "world",
        "base_link",
        "shoulder_link",
        "half_arm_1_link",
        "half_arm_2_link",
        "forearm_link",
        "spherical_wrist_1_link",
        "spherical_wrist_2_link",
        "bracelet_link",
        "end_effector_link",
    ]
    assert recognise("unknown", gen3_links).id == "kinova-gen3-7dof"


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


MAKER_IDS = frozenset(
    {
        "x500.airframe.holybro-x500-v2",
        "x500.flight-controller.pixhawk-6c",
        "x500.power-module.pm02-v3",
        "x500.motor.holybro-2216-920kv",
        "x500.companion.jetson-orin-nano",
        "nvidia.jetson-orin-nano-devkit",
        "nvidia.jetson-nano-devkit",
        "x500.gnss.here3-plus",
        "x500.camera.oak-d-lite",
        "x500.imu.icm-42688-p",
        "x500.imu.bmi088",
        "dynamixel.xl430-w250",
        "dynamixel.xm430-w350",
        "dynamixel.xc330-t288",
        "dynamixel.xl330-m288",
        "dynamixel.xc430-w150",
        "robotis.lds-02",
        "feetech.st3215",
        "feetech.sts3215",
        "feetech.sts3250",
        "feetech.sts3032",
        "feetech.scs0009",
        "anymal.actuator.anydrive",
        "anymal.leg.foot",
        "anymal.leg.hip-housing",
        "anymal.leg.thigh-housing",
        "anymal.leg.shank-housing",
        "anymal.power.battery",
        "x500.radio.rfd900x",
        "x500.magnetometer.ist8310",
        "rpi.pi4b",
        "rpi.pi5",
    }
)


def test_listed_parts_have_a_maker_and_generic_parts_have_none():
    parts = load_catalog().parts
    assert {p.id for p in parts} >= MAKER_IDS
    assert all(p.maker for p in parts if p.id in MAKER_IDS)
    assert all(p.maker is None for p in parts if p.id.startswith("generic."))


def test_maker_is_optional_and_reads_back_when_given():
    bare = Catalog.model_validate({"parts": [{"id": "a", "family": "fam.x", "name": "A"}]})
    assert bare.parts[0].maker is None
    given = Catalog.model_validate(
        {"parts": [{"id": "a", "family": "fam.x", "name": "A", "maker": "Acme"}]}
    )
    assert given.parts[0].maker == "Acme"


def _example_families() -> set[str]:
    root = Path(catalog_module.__file__).parent / "examples"
    files = [*root.rglob("contract.yaml"), root / "servo-bench.yaml"]
    return {m for f in files for m in re.findall(r"family: (fam\.[\w.\-]+)", f.read_text())}


def test_catalog_family_labels_match_the_shipped_examples():
    example = _example_families()
    for fam in {p.family for p in load_catalog().parts}:
        kind = fam.split(".")[1]
        labels = {e for e in example if e.split(".")[1] == kind}
        assert not labels or fam in labels, fam


def _numbers(pid: str):
    return next(p for p in load_catalog().parts if p.id == pid).numbers


def test_every_smart_servo_with_a_bus_has_a_sourced_stall_current_and_its_voltage():
    servos = [p for p in load_catalog().parts if p.family == "fam.servo.smart" and p.bus]
    assert len(servos) >= 18
    for p in servos:
        for key in ("stall_current", "stall_current_voltage"):
            n = p.numbers.get(key)
            assert n is not None, f"{p.id} lacks {key}"
            assert n.kind == "datasheet" and "https://" in n.source, f"{p.id}.{key}"


def test_no_stall_torque_source_leans_on_a_maximum_torque():
    for p in load_catalog().parts:
        n = p.numbers.get("stall_torque")
        assert n is None or "maximum" not in n.source.lower() or "stall" in n.source.lower(), p.id


def test_feetech_maker_page_corrections_hold():
    st = _numbers("feetech.st3215")
    assert (st["input_voltage_min"].value, st["input_voltage_max"].value) == (4, 14)
    assert _numbers("feetech.sts3215")["stall_torque_voltage"].value == 6


_REGULATOR_FAMILIES = {"fam.power.buck", "fam.power.ldo", "fam.power.bec", "fam.power-module"}


def _family_parts(match):
    return [p for p in load_catalog().parts if match(p.family)]


def test_maker_cited_regulator_and_driver_counts_are_pinned():
    regulators = _family_parts(lambda f: f in _REGULATOR_FAMILIES)
    drivers = _family_parts(lambda f: f.startswith("fam.driver."))
    assert len(regulators) == 24
    assert len(drivers) == 14


def test_every_power_and_drive_number_is_a_datasheet_with_an_https_source():
    parts = _family_parts(
        lambda f: f.startswith(("fam.power", "fam.driver.", "fam.controller.")) or f == "fam.esc"
    )
    assert parts
    for p in parts:
        for key, n in p.numbers.items():
            if p.id.startswith(("x500.", "generic.")):
                continue  # earlier seed parts keep their own source style
            assert n.kind == "datasheet", (p.id, key)
            assert re.search(r"https?://\S+", n.source), (p.id, key)


def test_wire_table_loads_with_contiguous_awg_and_the_staged_10_awg_resistance():
    table = load_catalog().wire_table
    awgs = [row.awg for row in table.rows]
    assert table.kind == "rule_of_thumb"
    assert set(range(10, 31)) <= set(awgs)
    assert {row.awg: row.ohm_per_m for row in table.rows}[10] == 0.003276


def test_pack_and_ptc_numbers_are_datasheet_with_an_https_source():
    parts = [p for p in load_catalog().parts if p.id.startswith(("pack.", "ptc."))]
    assert len(parts) >= 18
    assert all(
        n.kind == "datasheet" and "https://" in n.source for p in parts for n in p.numbers.values()
    )


def test_a_catalog_without_a_wire_table_still_loads():
    assert Catalog.model_validate({"parts": []}).wire_table is None


def test_tvs_diodes_carry_standoff_and_clamp_from_the_maker_sheet():
    tvs = _family_parts(lambda f: f == "fam.protection.tvs")
    assert len(tvs) == 8
    for p in tvs:
        n = p.numbers
        assert n["clamping_voltage_vc_max"].value > n["standoff_voltage_vr"].value
        assert all(x.source.startswith("https://www.vishay.com/") for x in n.values())


def test_connector_contact_resistance_is_a_maker_figure_in_milliohm():
    conns = _family_parts(lambda f: f.startswith("fam.connector."))
    rs = {
        p.id: p.numbers["contact_resistance_mohm"]
        for p in conns
        if "contact_resistance_mohm" in p.numbers
    }
    assert len(rs) == 8
    assert all(r.unit == "mOhm" and r.source.startswith("https://") for r in rs.values())


def _hardware(prefix: str):
    return [p for p in load_catalog().parts if p.id.startswith(prefix)]


def test_iso4762_m3x8_mass_is_08_g_with_an_https_source():
    part = next(p for p in load_catalog().parts if p.id == "hw.iso4762.m3x8")
    assert part.numbers["mass"].value == 0.8
    assert part.numbers["mass"].source.startswith("https://")


def test_every_standard_fastener_has_a_thread_diameter_and_screws_a_length():
    parts = [p for p in load_catalog().parts if p.family.startswith("fam.hardware.")]
    assert parts
    assert all("thread_diameter" in p.numbers for p in parts)
    assert all("length" in p.numbers for p in parts if ".screw." in p.family)


def test_m3_screw_mass_does_not_fall_as_length_grows():
    m3 = sorted(
        (p for p in _hardware("hw.iso4762.m3x") if p.numbers["thread_diameter"].value == 3),
        key=lambda p: p.numbers["length"].value,
    )
    masses = [p.numbers["mass"].value for p in m3]
    assert len(masses) >= 4
    assert masses == sorted(masses)


def _pinouts():
    from lockwire.catalog import load_pinouts

    return load_pinouts()


def test_every_pinout_card_loads_and_every_pin_table_has_an_https_source():
    cards = _pinouts()
    assert len(cards) == 13
    for conn in (c for card in cards for c in card.connectors if c.pins):
        assert conn.source.startswith("https://"), conn.id


def test_esp32_gpio0_is_a_reserved_strapping_pin_with_a_reason():
    card = {c.id: c for c in _pinouts()}["esp32-devkitc-wroom-32"]
    pin = next(p for c in card.connectors for p in c.pins if "GPIO0" in (p.aliases or []))
    assert pin.reserved and pin.reason and "strapping" in pin.capabilities


def test_jetson_orin_nano_card_is_pin_facts_only():
    card = {c.id: c for c in _pinouts()}["nvidia-jetson-orin-nano-devkit"]
    assert "no tables, figures or wording are reproduced" in (card.note or "")
    pins = [p for c in card.connectors if c.id == "J12" for p in c.pins]
    assert len(pins) == 40
    assert all(len(p.reason or "") <= 80 for p in pins)


def test_every_pinout_number_is_a_datasheet_figure_with_an_https_source():
    numbers = [n for card in _pinouts() for n in card.numbers.values()]
    assert numbers
    assert all(n.kind == "datasheet" and "https://" in n.source for n in numbers)


def test_cells_carry_capacity_and_nominal_voltage_from_a_maker_sheet():
    cells = _family_parts(lambda f: f == "fam.battery.cell.li-ion")
    assert len(cells) == 14
    for c in cells:
        assert {"capacity_ah", "voltage_nominal"} <= set(c.numbers), c.id
        assert all(n.kind == "datasheet" and "https://" in n.source for n in c.numbers.values())


def _ctypes():
    return {c.id: c for c in load_catalog().connector_types}


def test_qwiic_current_is_sparkfuns_226ma_and_pins_are_in_order():
    q = _ctypes()["qwiic"]
    assert q.rated_current_a == 0.226 and "226mA" in q.sources["rated_current_a"]
    assert q.pin_labels == ["GND", "3V3", "SDA", "SCL"]


def test_every_connector_type_field_has_an_https_source():
    types = load_catalog().connector_types
    assert len(types) == 17
    for c in types:
        assert set(c.sources) == {f for f in c.model_fields_set if f not in ("id", "sources")}, c.id


def test_a_connector_type_field_without_an_https_source_is_rejected():
    raw = load_catalog().connector_types[0].model_dump(exclude_none=True)
    raw["sources"].pop("pin_count")
    with pytest.raises(ValidationError, match="pin_count needs an https source"):
        catalog_module.CatalogConnectorType(**raw)


def test_a_catalog_without_connector_types_loads():
    assert Catalog(parts=[]).connector_types == []


def test_no_grove_entry_states_a_pitch():
    grove = [c for i, c in _ctypes().items() if i.startswith("grove-")]
    assert len(grove) == 4 and all(c.pitch_mm is None for c in grove)


def test_maker_stated_pin_counts_on_grove_and_the_jst_gh_4_board_connector():
    types = {t.id: t for t in load_catalog().connector_types}
    assert [types[g].pin_count for g in ("grove-uart", "grove-digital", "grove-analog")] == [4] * 3
    gh4 = types["jst-gh-4"]
    assert (gh4.family, gh4.pitch_mm, gh4.pin_count, gh4.pin_labels) == ("JST-GH", 1.25, 4, None)


def test_amass_contact_counts_come_from_the_maker_drawing():
    types = {t.id: t for t in load_catalog().connector_types}
    got = {i: types[i].pin_count for i in ("xt60", "xt90hw", "mr30", "mr60", "xt30")}
    assert got == {"xt60": 2, "xt90hw": 2, "mr30": 3, "mr60": 3, "xt30": None}
    assert all("maker drawing" in types[i].sources["pin_count"] for i in ("xt60", "mr30"))


def _count_yaml_loads(monkeypatch):
    from ruamel.yaml import YAML

    calls = []
    real = YAML.load

    def counting(self, stream):
        calls.append(1)
        return real(self, stream)

    monkeypatch.setattr(YAML, "load", counting)
    return calls


def test_catalog_is_parsed_once_per_process_across_a_check_run(monkeypatch):
    from lockwire.api import load
    from lockwire.checks import run_record_checks

    contract = load(Path(catalog_module.__file__).parent / "examples" / "arm" / "contract.yaml")
    catalog_module._CACHE.clear()
    calls = _count_yaml_loads(monkeypatch)
    run_record_checks(contract)
    assert len(calls) <= 3  # seed, robots and pinouts once each, not seven seeds
    seed_loads = len(calls)
    run_record_checks(contract)
    assert len(calls) == seed_loads  # second run parses nothing


def test_touching_the_catalog_file_reloads_it_once(tmp_path, monkeypatch):
    import os
    import shutil

    copy = tmp_path / "cat.yaml"
    shutil.copy(catalog_module._SEED_PATH, copy)
    catalog_module._CACHE.clear()
    calls = _count_yaml_loads(monkeypatch)
    load_catalog(copy)
    load_catalog(copy)
    assert len(calls) == 1
    st = copy.stat()
    os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    load_catalog(copy)
    load_catalog(copy)
    assert len(calls) == 2


def test_mutating_a_returned_part_does_not_reach_the_next_caller():
    part = next(p for p in load_catalog().parts if p.numbers)
    name = next(iter(part.numbers))
    before = part.numbers[name].value
    part.numbers[name].value = before + 123
    part.numbers.clear()
    again = next(p for p in load_catalog().parts if p.id == part.id)
    assert again.numbers[name].value == before


_DENSITIES = {
    "Aluminium 6061-T6": 2700,
    "Mild steel S235JR": 7850,
    "Stainless steel 304 (1.4301)": 8000,
    "PLA (Polymaker PolyLite)": 1170,
    "PETG (Polymaker PolyLite)": 1250,
    "ABS (Polymaker PolyLite)": 1040,
    "Carbon fibre sheet 1 mm (Easy Composites CFS-RI)": 1330,
}


def test_the_seven_cited_materials_carry_their_printed_densities():
    got = {m.name: m.density_kg_m3 for m in catalog_module.materials()}
    assert got == _DENSITIES


def test_material_looks_up_by_name_and_unknown_is_none():
    assert catalog_module.material("pla (polymaker polylite)").density_kg_m3 == 1170
    assert catalog_module.material("unobtainium") is None


def test_every_material_is_a_lockfile_material_with_a_url_source():
    for m in catalog_module.materials():
        assert schema.Material.model_validate(m.model_dump()) == m
        assert "https://" in m.source and "read 2026-11-13" in m.source


def test_a_catalog_without_materials_loads(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("parts: []\n")
    assert load_catalog(f).materials == []


BODIES = {
    "hitec.hs-422": (0.0406, 0.0198, 0.0366),
    "hitec.hs-645mg": (0.0408, 0.0200, 0.0378),
    "feetech.st3215": (0.04523, 0.02473, 0.035),
    "feetech.sts3215": (0.04523, 0.02473, 0.035),
    "feetech.sts3250": (0.04522, 0.02472, 0.035),
    "feetech.scs0009": (0.0233, 0.0121, 0.02525),
}


def test_the_verified_bodies_are_pinned_in_metres():
    parts = {p.id: p for p in load_catalog().parts}
    with_body = {i for i, p in parts.items() if p.body and not i.startswith("gobilda.1501-")}
    assert with_body == set(BODIES)
    for pid, size in BODIES.items():
        assert parts[pid].body.size == pytest.approx(size)


def test_ax12a_horn_frame_is_the_hand_worked_x_only():
    part = {p.id: p for p in load_catalog().parts}["dynamixel.ax-12a"]
    (frame,) = part.frames
    assert frame.id == "horn" and frame.rpy is None
    assert frame.xyz == (pytest.approx(0.025 - 0.0115), None, None)
    assert part.body is None


def test_a_part_without_frames_or_body_defaults_and_loads():
    part = CatalogPart(id="x.y", family="f", name="n")
    assert part.frames == [] and part.body is None


def test_a_body_or_frame_source_without_a_link_is_refused():
    with pytest.raises(ValidationError):
        catalog_module.PartBody(size=(1, 2, 3), source="a drawing")
    with pytest.raises(ValidationError):
        catalog_module.PartFrame(id="horn", xyz=(0, None, None), source="a drawing")


def test_the_origin_convention_is_written_in_catalog_py():
    src = Path(catalog_module.__file__).read_text()
    assert "origin is the centre of its case's bounding" in src.replace("\n# ", " ")


KIT_IDS = {
    *(f"gobilda.{s}" for s in (
        "1120-0005-0144", "1120-0008-0216", "1120-0017-0432", "1120-0033-0816",
        "1121-0003-0096", "1121-0007-0192", "1121-0011-0288",
        "1116-0040-0088", "1116-0088-0232", "1123-0096-0240")),
    *(f"actobotics.{s}" for s in ("585442", "585448", "585450", "585454", "585458", "585004")),
}  # fmt: skip


def _kit():
    return {p.id: p for p in load_catalog().parts if p.id in KIT_IDS}


def test_the_16_kit_part_ids_are_pinned():
    assert len(KIT_IDS) == 16
    assert set(_kit()) == KIT_IDS


def test_kit_hole_pattern_pitches_are_metres():
    for pid, p in _kit().items():
        if pid.startswith("gobilda."):
            assert (p.hole_pattern.pitch, p.hole_pattern.kind) == (0.008, "grid")
        elif p.hole_pattern:
            assert p.hole_pattern.kind == "center_spacing"
            assert p.hole_pattern.pitch == pytest.approx(0.01905, abs=1e-12)


def test_a_part_without_hole_pattern_loads_with_none():
    assert _kit()["actobotics.585004"].hole_pattern is None
    assert load_catalog().parts[0].hole_pattern is None


def test_actobotics_mass_uses_the_ounce_figure_not_the_rounded_grams():
    mass = _kit()["actobotics.585450"].numbers["mass"]
    assert mass.value == pytest.approx(3.62 * 28.349523125, abs=1e-6)
    assert "3.62 oz (103g)" in mass.source


ROTATION_300 = {"dynamixel.ax-12a", "feetech.scs0009"}
ROTATION_360 = {
    *(f"dynamixel.{s}" for s in "xc330-m288 xc330-t288 xc430-w150 xh430-v350 xl330-m077".split()),
    *(f"dynamixel.{s}" for s in "xl330-m288 xl430-w250 xm430-w210 xm430-w350 xm540-w270".split()),
    "dynamixel.mx-28", "dynamixel.mx-64", "feetech.st3215", "feetech.sts3032",
    "feetech.sts3215", "feetech.sts3250",
}  # fmt: skip


def test_the_confirmed_servos_carry_a_maker_stated_rotation_range():
    parts = {p.id: p for p in load_catalog().parts}
    got = {i for i, p in parts.items() if "rotation_range" in p.numbers}
    assert got == ROTATION_300 | ROTATION_360
    for i in got:
        n = parts[i].numbers["rotation_range"]
        assert (n.unit, n.kind) == ("deg", "datasheet")
        assert n.value == (300 if i in ROTATION_300 else 360)


# ---- mechanical properties (CADN-matdata)
def _mech(name):
    entry = catalog_module.material_properties(name)
    assert entry is not None
    return entry


def test_every_material_has_cited_stiffness_and_strength():
    for m in catalog_module.materials():
        nums = _mech(m.name).numbers
        assert "youngs_modulus" in nums
        assert {"yield_strength", "tensile_strength", "tensile_strength_z"} & set(nums)
        assert all(n.source.startswith("https://") and n.unit == "MPa" for n in nums.values())


def test_petg_caveat_names_print_direction():
    assert "print direction" in (_mech("PETG (Polymaker PolyLite)").caveat or "")


def test_no_plastic_stores_a_tensile_figure_as_yield():
    for name in ("PLA", "PETG", "ABS"):
        assert "yield_strength" not in _mech(f"{name} (Polymaker PolyLite)").numbers


def test_abs_xy_tensile_dropped_and_poisson_absent():
    assert "tensile_strength" not in _mech("ABS (Polymaker PolyLite)").numbers
    assert all("poisson_ratio" not in m.numbers for m in load_catalog().mechanical)


def test_unknown_material_has_no_properties():
    assert catalog_module.material_properties("unobtainium") is None


def test_mechanical_entry_naming_unknown_material_is_refused():
    data = load_catalog().model_dump()
    data["mechanical"][0]["name"] = "unobtainium"
    with pytest.raises(ValidationError, match="names no material"):
        Catalog.model_validate(data)


def test_material_still_validates_as_schema_material():
    assert isinstance(catalog_module.material("PLA (Polymaker PolyLite)"), schema.Material)


DIMS = {
    "plate": ["length", "width", "thickness"],
    "round_tube": ["outer_diameter", "wall", "length"],
    "round_rod": ["diameter", "length"],
    "square_tube": ["outer_size", "wall", "length"],
    "rect_tube": ["outer_width", "outer_height", "wall", "length"],
    "angle": ["leg_a", "leg_b", "thickness", "length"],
    "tslot": ["length"],
}


def test_stock_ids_and_dimension_lists_are_pinned():
    rows = {s.id: s for s in catalog_module.stock()}
    plain = {f"stock.{k}" for k in DIMS if k != "tslot"}
    assert set(rows) == plain | {"tslot.20x20", "tslot.20x40", "tslot.30x30"}
    for s in rows.values():
        assert s.dimensions == DIMS[s.shape]


def test_tslot_rows_use_the_makers_mass_per_metre_and_outer_box():
    rows = {s.id: s for s in catalog_module.stock()}
    want = {"tslot.20x20": (0.48, (0.02, 0.02)), "tslot.20x40": (0.89, (0.02, 0.04))}
    want["tslot.30x30"] = (1.26, (0.03, 0.03))
    for sid, (mass, box) in want.items():
        assert rows[sid].mass_from == "mass_per_length"
        assert (rows[sid].mass_per_length.value, rows[sid].outer) == (mass, box)
    assert rows["stock.plate"].mass_from == "material"


def test_stock_mass_rule_is_enforced_both_ways():
    n = {"value": 1, "unit": "kg/m", "kind": "assumed", "source": "assumed"}
    base = {"id": "x", "name": "x", "shape": "plate"}
    with pytest.raises(ValidationError):
        catalog_module.StockShape(**base, mass_from="material", mass_per_length=n)
    with pytest.raises(ValidationError):
        catalog_module.StockShape(**{**base, "shape": "tslot"}, mass_from="mass_per_length")


def test_goBILDA_standoff_bodies_are_the_outer_box_in_metres():
    ids = {p.id: p for p in catalog_module.structure_parts()}
    for sid, length in {"gobilda.1501-0006-0100": 0.01, "gobilda.1501-0006-0430": 0.043}.items():
        assert ids[sid].body.size == (0.006, 0.006, length)
        assert ids[sid].numbers["length"].value == length
    assert len([i for i in ids if i.startswith("gobilda.1501-")]) == 6


def test_structure_parts_lists_kit_parts_and_no_servo():
    ids = {p.id for p in catalog_module.structure_parts()}
    assert {"gobilda.1120-0005-0144", "gobilda.1501-0006-0160"} <= ids
    assert all(p.family.startswith("fam.structure.") for p in catalog_module.structure_parts())


def test_belt_pitch_matches_profile_or_alias_in_metres():
    assert belt_pitch("GT2").pitch == 0.002
    assert belt_pitch("2gt") == belt_pitch("GT2")


def test_belt_pitch_mxl_is_2_032_mm_in_metres():
    assert belt_pitch("MXL").pitch == 0.002032


def test_belt_pitch_unlisted_profile_is_none():
    assert belt_pitch("HTD 8M") is None


def test_htd_pitches_come_from_gates_in_metres():
    assert (belt_pitch("HTD 3M").pitch, belt_pitch("5m").pitch) == (0.003, 0.005)
    assert "gates.com" in belt_pitch("HTD5M").source


def test_catalog_without_belt_profiles_loads(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("parts: []\n")
    assert load_catalog(f).belt_profiles == []


def test_two_printed_figures_store_the_lower_and_keep_the_other_in_the_source():
    parts = {p.id: p for p in load_catalog().parts}
    n = lambda pid, key: parts[pid].numbers[key]  # noqa: E731
    cases = [
        ("pack.gensace.3s-2200-60c-adventure", "c_rate_continuous", 50, "60C"),
        ("conn.powerpole-pp45", "voltage_rated", 160, "600"),
        ("odrive.pro", "current_continuous", 20, "80 A"),
        ("ti.drv8833", "current_continuous", 1.5, "PW (TSSOP)"),
    ]
    for pid, key, value, other in cases:
        assert n(pid, key).value == value
        assert other in n(pid, key).source
    assert "PWP package" in parts["ti.drv8833"].name


def test_availability_records_where_the_maker_states_it():
    d = catalog_module.availability("pololu.d24v60f5")
    assert d["status"] == "discontinued" and "D24V90F5" in d["replaced_by"]
    assert catalog_module.availability("ti.drv8833")["status"] == "active"


def test_availability_without_a_record_is_not_checked_never_active():
    part = next(p.id for p in load_catalog().parts if p.id not in ("pololu.d24v60f5", "ti.drv8833"))
    assert catalog_module.availability(part)["status"] == "not checked"
    assert catalog_module.availability("no.such.part") is None


def test_availability_naming_a_missing_part_is_refused():
    rec = {"part": "no.such", "status": "active", "source": "https://x.test", "read_on": "2026-11-13"}
    with pytest.raises(ValidationError, match="names no part"):
        Catalog.model_validate({**load_catalog().model_dump(), "availability": [rec]})


def test_hole_size_clearance_is_optimas_medium_fit():
    got = catalog_module.hole_size("M3", "clearance")
    assert got["diameter_m"] == 0.0034 and got["fit_class"] == "Medium Fit"


def test_hole_size_m8_tap_keeps_optimas_value_and_notes_tr_differs():
    got = catalog_module.hole_size("m8", "tap")
    assert got["diameter_m"] == 0.00675 and "6.8" in got["source"]


def test_hole_size_unknown_fit_or_size_is_none():
    assert catalog_module.hole_size("M3", "press") is None
    assert catalog_module.hole_size("M10", "close") is None


def test_hole_sizes_seed_has_28_rows():
    assert len(load_catalog().hole_sizes) == 28
