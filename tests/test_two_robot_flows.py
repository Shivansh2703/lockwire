"""E4.6: both robots through the seven app faces, via the library `api` module.

One parametrized test per robot (X500, ANYmal) exercising the library function
behind each face (`api.FUNCTIONS`; face->command mapping in `gui.py`
`_COMMAND_MODES`): Design/scene (`api.load_scene`), Declare (`api.declare_form`,
the read-only form `declare`/`apply`/`review` share), Check (`api.check`),
Plan (`api.plan`), Test/replay and Run/preflight-dry (both `api.preflight`,
offline/dry: a saved recording, never live hardware or Docker — see below),
Reference/describe (`api.describe`).

Each assertion is only "returns a well-formed document with no exception" —
this is a smoke test across two very different contracts, not a correctness
audit of either.
"""

from pathlib import Path

import pytest

from conftest import FIXTURES
from lockwire_pro import api_pro as api

X500_CONTRACT = FIXTURES / "x500" / "contract_corrected.yaml"
X500_SCENE = FIXTURES / "x500" / "x500.glb"
ANYMAL_CONTRACT = FIXTURES / "anymal" / "contract.yaml"

# Test/replay and Run/preflight-dry both go through `api.preflight`, and both
# take the offline/dry path here: a saved recording, not a live robot or a
# Docker-run sim (E4.6 asks for the dry/offline variant wherever a face would
# otherwise need hardware or Docker). `bags/clean`'s channels belong to its own
# synthetic fixture contract, not X500 or ANYmal, so neither contract declares
# any fitted-band device the bag's channels would score against — the honest
# result for both is NOT_SCORED, not GO; `run_preflight` still returns a
# complete document rather than raising.
DRY_RECORDING = FIXTURES / "bags" / "clean"


@pytest.mark.parametrize(
    "contract_path, scene_path",
    [
        pytest.param(X500_CONTRACT, X500_SCENE, id="x500"),
        # Keep scene_path=None to exercise T2 discovery. The checked-in
        # display-only companion URDF resolves ANYmal's 40 active DAE visuals
        # from the checked-in mesh package; model identity remains separate.
        pytest.param(ANYMAL_CONTRACT, None, id="anymal"),
    ],
)
def test_seven_faces_return_well_formed_documents(contract_path: Path, scene_path: Path | None):
    # Design/scene
    scene, scene_source = api.load_scene(contract_path, scene_path)
    if scene_path is None:
        assert scene is not None
        assert scene_source == "contract.urdf (found beside the contract)"
        assert scene.asset_path == str((FIXTURES / "anymal" / "contract.urdf").resolve())
        assert len(scene.parts) == 40
    else:
        assert scene is not None
        assert scene_source

    # Declare (read-only form shared by declare/apply/review)
    declare_doc = api.declare_form(contract_path)
    assert isinstance(declare_doc, dict)
    assert "rows" in declare_doc

    # Check
    check_doc = api.check(contract_path)
    assert isinstance(check_doc, dict)
    assert check_doc["artifact_type"] == "lockwire-check-report"
    assert check_doc["verdict"]

    # Plan (an empty plan is a valid plan; scoring it is the smoke test)
    plan_doc = api.plan(contract_path, {"steps": []})
    assert isinstance(plan_doc, dict)
    assert plan_doc["artifact_type"] == "lockwire-plan-report"

    # Test/replay (offline replay of a saved recording)
    test_doc = api.preflight(contract_path, DRY_RECORDING)
    assert isinstance(test_doc, dict)
    assert test_doc["artifact_type"] == "lockwire-preflight-report"
    assert test_doc["verdict"]

    # Run/preflight dry (same library call as Test/replay; a live Run needs a
    # powered-on robot, which this test never has)
    run_doc = api.preflight(contract_path, DRY_RECORDING)
    assert isinstance(run_doc, dict)
    assert run_doc["artifact_type"] == "lockwire-preflight-report"

    # Reference/describe
    describe_doc = api.describe(contract_path)
    assert isinstance(describe_doc, dict)
    assert describe_doc["artifact_type"] == "lockwire-describe"
    assert describe_doc["files"]
