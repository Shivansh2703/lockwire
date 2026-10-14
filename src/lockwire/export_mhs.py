"""The MHS bridge, outward only.

Renders each declared device into the converging device-manifest shape five independent
third-party designs settled on (``device_id``, ``capabilities[]``, ``safety_limits[]`` with
``min``/``max``): what it measures, what it adjusts, what it refuses. ``min``/``max`` are
aliased straight from ``Band.lo``/``Band.hi`` — nothing here is invented, predicted or guessed.
Read-only, derived from the contract, never the truth.

Import of a third-party manifest into a contract is refused elsewhere (``lockwire import``): the inbound direction is refused until
an open standard is published, because import is where lock-in happens. This module
builds the outward direction only — no reader for the inbound shape lives here.
"""

from __future__ import annotations

from lockwire.checks import power_rollup
from lockwire.describe import _role
from lockwire.schema import Contract, DeviceInstance, Provenance

# ponytail: the schema's witness enum (`Provenance.witness`, schema.py:191) is
# "w0..w6 | live | human" — no literal "fixture"/"sim" value exists yet. "human"
# (author-asserted, not measured off real hardware or a bag) is the closest proxy for
# "this number is a stand-in, not a real measurement". Revisit if a real witness value
# for fixture/sim data lands.
_SIMULATED_WITNESSES = {"human"}


def _provenance_fields(provenance: Provenance | None) -> dict[str, object]:
    if provenance is None:
        return {"witness": None, "simulated": False}
    return {
        "witness": provenance.witness,
        "simulated": provenance.witness in _SIMULATED_WITNESSES,
    }


def _safety_limits(instance: DeviceInstance, model) -> list[dict[str, object]]:
    """Every band this device declares, each tagged with where it came from.

    A datasheet rating (``model.hard_limits``) never reads the same as a fitted band
    (statistically evidenced, ``envelope.fitted_bands``) or an inferred one (observed,
    no fit evidence) — collapsing the three into one field is a loss this module refuses.
    """
    limits: list[dict[str, object]] = []
    if model is not None and model.hard_limits:
        prov = _provenance_fields(model.provenance)
        for channel, band in sorted(model.hard_limits.items()):
            limits.append(
                {
                    "channel": channel,
                    "min": band.lo,
                    "max": band.hi,
                    "unit": band.unit,
                    "kind": "datasheet",
                    **prov,
                }
            )
    for envelope in instance.envelopes:
        prov = _provenance_fields(envelope.provenance)
        for name, signal in sorted(envelope.signals.items()):
            kind = "fitted" if f"signals/{name}" in envelope.fitted_bands else "inferred"
            limits.append(
                {
                    "channel": f"{envelope.context}/{name}",
                    "min": signal.band.lo,
                    "max": signal.band.hi,
                    "unit": signal.band.unit,
                    "kind": kind,
                    **prov,
                }
            )
    return limits


def _capabilities(model, instance: DeviceInstance) -> list[str]:
    role = _role(model, instance)  # reuse describe.py's walk rather than re-derive it
    if role == "measures and adjusts":
        return ["measures", "adjusts"]
    if role in ("measures", "adjusts"):
        return [role]
    return []


def render_device(contract: Contract, instance: DeviceInstance) -> dict[str, object]:
    """One device's converging-manifest entry."""
    model = contract.device_model(instance.model)
    prov = _provenance_fields(instance.provenance)
    return {
        "device_id": instance.id,
        "capabilities": _capabilities(model, instance),
        "safety_limits": _safety_limits(instance, model),
        **prov,
    }


def _loss_report(contract: Contract) -> list[str]:
    """What a per-device manifest cannot carry, named rather than generic."""
    rail_totals, *_ = power_rollup(contract, "nominal")
    rails = ", ".join(sorted(rail_totals)) if rail_totals else "none declared"
    return [
        f"shared-rail power budgets are not carried: this contract rolls up {rails}; "
        "a per-device manifest has no field for a rail's capacity or headroom.",
        "frame references are not carried: joint/link membership and the URDF frame tree "
        "(mount frames, drives_joint pairings) have no place in a per-device manifest.",
        "provenance/enforcement metadata beyond witness/simulated is not carried: bag "
        "hashes, lock dates, and the write-layer verdicts are dropped.",
    ]


def export_mhs(contract: Contract) -> dict[str, object]:
    """Render the whole contract into the converging device-manifest shape. Outward only."""
    return {
        "devices": [render_device(contract, device) for device in contract.devices],
        "loss_report": _loss_report(contract),
    }
