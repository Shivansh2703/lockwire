"""Variants (param overrides) and the per-variant check."""

import re
import shutil
from pathlib import Path

import pytest

from lockwire import edit
from lockwire.schema import Contract, Variant, load_contract, save_contract
from lockwire.variants import check_variants, variant_contracts

ROVER = (
    Path(__file__).parents[1] / "src" / "lockwire" / "examples" / "rover-param" / "contract.yaml"
)


def _rover(tmp_path):
    path = tmp_path / "rover.yaml"
    shutil.copy(ROVER, path)
    return path


def _add(path, name, **params):
    edit.add_entry(
        path, "variant", {"name": name, "params": params}, source="design choice", witness="w1"
    )


def _select(path, name):
    edit.add_entry(path, "select", {"variant": name}, source="design choice", witness="w1")


def _tip(findings):
    [f] = [f for f in findings if f.quantity == "model/tip_margin"]
    return int(re.search(r"is (\d+) mm inside", f.observed).group(1))


def test_variant_record_round_trips_and_selector_is_declared(tmp_path):
    path = _rover(tmp_path)
    _add(path, "long", half_wheelbase=0.2)
    _select(path, "long")
    c = load_contract(path)
    assert c.active_variant == "long" and c.variants[0].params["half_wheelbase"] == 0.2
    again = tmp_path / "again.yaml"
    save_contract(c, again)
    assert load_contract(again).variants == c.variants
    assert load_contract(again).active_variant == "long"


def test_undeclared_param_override_is_refused_byte_identical(tmp_path):
    path = _rover(tmp_path)
    before = path.read_bytes()
    with pytest.raises(edit.EditRefusal, match="variant 'x' sets param 'nope'"):
        _add(path, "x", nope=1.0)
    assert path.read_bytes() == before


def test_active_variant_must_name_a_declared_variant(tmp_path):
    data = load_contract(ROVER).to_file_data(mode="json", exclude_none=True)
    data["active_variant"] = "ghost"
    with pytest.raises(ValueError, match="active_variant 'ghost' is not a declared variant"):
        Contract.model_validate(data)
    path = _rover(tmp_path)
    before = path.read_bytes()
    with pytest.raises(edit.EditRefusal, match="active_variant 'ghost'"):
        _select(path, "ghost")
    assert path.read_bytes() == before


def test_variant_contracts_apply_overrides(tmp_path):
    path = _rover(tmp_path)
    _add(path, "wide", half_track=0.3)
    c = load_contract(path)
    [(_, base), (n1, wide)] = [("base", c), *variant_contracts(c)]
    assert n1 == "wide"
    offsets = lambda k: sorted(abs(j.origin.xyz[1]) for j in k.model.joints)  # noqa: E731
    assert offsets(wide) != offsets(base)
    assert variant_contracts(load_contract(ROVER))[0][0] == "base"


def test_check_runs_every_variant_and_names_it(tmp_path):
    path = _rover(tmp_path)
    _add(path, "short", half_wheelbase=0.15)
    _add(path, "long", half_wheelbase=0.2)
    _select(path, "short")
    rows = check_variants(load_contract(path), contract_path=path)
    assert [r[0] for r in rows] == ["short", "long"]
    assert _tip(rows[0][2]) != _tip(rows[1][2])
    assert all(r[1] for r in rows)


def test_out_of_range_override_is_refused_byte_identical(tmp_path):
    path = _rover(tmp_path)
    before = path.read_bytes()
    with pytest.raises(
        edit.EditRefusal,
        match=r"variant 'big' sets param 'half_wheelbase' to 5, outside its range 0.0 to 1.0",
    ):
        _add(path, "big", half_wheelbase=5.0)
    assert path.read_bytes() == before


def test_hand_edited_out_of_range_variant_is_one_plain_line(tmp_path):
    c = load_contract(ROVER)
    object.__setattr__(c, "variants", [Variant(name="big", params={"half_wheelbase": 5.0})])
    with pytest.raises(ValueError, match="variant 'big' sets a value outside its range") as info:
        variant_contracts(c)
    assert "\n" not in str(info.value) and "validation error" not in str(info.value)
