"""Fired lane-A1 findings never leak "None" or an internal snake_case name into user-facing text."""

import re

import pytest

from lockwire.report import Finding

T = "tests/test_checks.py::"
FIRING_TESTS = [
    "test_hold_torque_warns_only_at_the_pose_that_exceeds_the_limit",
    "test_unprotected_branch_and_unsourced_driver_advice",
    "test_inertia_sanity_flags_a_far_off_tensor_as_unsourced_advice",
    "test_no_source_rail_warns_only_where_source_modelling_started",
    "test_brownout_all_stall_drop_to_4_3_v_is_no_go_and_exits_1",
    "test_rail_operating_point_is_below_its_declared_range_after_the_wire_drop",
    "test_reverse_polarity_4s_pack_without_guard_warns_once_and_tvs_is_no_guard",
    "test_standing_over_limit_finding_states_its_assumptions",
    "test_bulk_cap_below_rule_warns_as_unsourced_advice",
    "test_tvs_missing_or_standoff_below_rail_warns_and_adequate_is_silent",
    "test_estop_single_channel_on_dual_sto_warns",
    "test_servo_id_spelled_differently_is_still_a_duplicate",
    "test_datasheet_limit_300_deg_on_270_deg_servo_cites_the_datasheet",
    "test_collision_visual_mismatch_is_advice_only",
    "test_connector_heat_is_i_squared_r_from_the_catalog_contact_resistance",
    "test_qwiic_chain_of_300_ma_gets_the_sparkfun_warning",
    "test_back_power_two_inputs_is_unsourced_advice_until_guarded",
]
# a word with an underscore, or the bare word None
LEAK = re.compile(r"\b\w+_\w+\b|\bNone\b")


def _fired(monkeypatch):
    seen: list[Finding] = []
    init = Finding.__init__

    def record(self, *a, **k):
        init(self, *a, **k)
        seen.append(self)

    monkeypatch.setattr(Finding, "__init__", record)
    code = pytest.main(
        ["-q", "-p", "no:cacheprovider", "-p", "no:randomly", *[T + t for t in FIRING_TESTS]]
    )
    assert code == 0
    return seen


def test_fired_findings_have_no_none_or_internal_names(monkeypatch):
    seen = _fired(monkeypatch)
    assert len(seen) >= len(FIRING_TESTS)
    for f in seen:
        for text in (f.observed, f.expected, f.action):
            assert not LEAK.search(re.sub(r"\S*/\S*", "", text)), (
                f.quantity,
                text,
            )  # file paths are not names
