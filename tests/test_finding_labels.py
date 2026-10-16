"""R2: the engine names findings and targets, so the app never parses paths."""

from pathlib import Path

from lockwire import api
from lockwire import serve
from lockwire.api_payload import PlanPayload
from lockwire.page import finding_label
from lockwire.report import Finding

CLEAN = Path(__file__).parent / "fixtures" / "contract_clean.yaml"


def _f(quantity: str) -> Finding:
    return Finding(
        severity="violation", quantity=quantity, observed="1", expected="2", action="fix"
    )


def test_labels_are_short_and_drop_structure():
    assert finding_label(_f("devices/dev.imu/signals/accel_x/variance")) == "Signal not frozen"
    assert finding_label(_f("plan/steps/0/rail_load/rail")) == "Rail load"
    assert finding_label(_f("preflight/coverage/comm_links_under_representative_load")) == (
        "Comm links under load"
    )


def test_served_plan_names_each_finding_in_order():
    document = api.plan(
        CLEAN, {"steps": [{"kind": "joint_target", "joint": "imu_mount", "effort": 3.0}]}
    )
    serve._label_findings(document, str(CLEAN))
    payload = PlanPayload.model_validate(document)
    assert len(payload.finding_labels) == len(payload.findings) > 0
    assert payload.finding_labels[0].label == "Joint effort"


def test_rail_overload_has_one_name_on_every_page():
    from lockwire.page import finding_labels

    contract = api.load(Path(__file__).parent / "fixtures" / "x500" / "contract.yaml")
    f = Finding(
        severity="violation",
        quantity="budgets/bud.power.pm02-5v",
        observed="1",
        expected="2",
        action="x",
    )
    assert (
        finding_labels(contract, [f])[0]["label"] == "PM02 5 V rail overloaded"
    )  # as Check's card title
