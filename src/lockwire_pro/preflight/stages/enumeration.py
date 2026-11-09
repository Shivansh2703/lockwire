"""Stage 0 — device enumeration (declared identity vs source observation)."""

from __future__ import annotations

from lockwire_pro.preflight.source import DeviceObservation, PreflightSource
from lockwire.report import Finding
from lockwire.schema import Contract, DeviceInstance

IMPLEMENTED = True

# Identity descriptors compared when BOTH operands are populated (D3).
_IDENTITY_FIELDS = (
    "bus_ref",
    "host",
    "vid_pid",
    "serial_number",
    "can_id",
    "address",
)


def _scores_absence(source: PreflightSource) -> bool:
    """Live bus observers score absence as NO-GO; RecordedSource cannot."""
    return bool(getattr(source, "scores_absence", False))


def check(contract: Contract, source: PreflightSource) -> list[Finding]:
    """Compare declared bus-attached devices to what the source enumerates."""
    observed_by_id = {obs.device_id: obs for obs in source.enumerate()}
    findings: list[Finding] = []
    # Host-side enumeration must not silently score as a container bus fact (§3.1).
    # Call after enumerate() — LiveSource fills disclosures during that pass,
    # keyed on the recorded filesystem access fact (not a path-string heuristic).
    host_side = getattr(source, "host_side_disclosures", None)
    if callable(host_side):
        findings.extend(host_side())
    for device in contract.devices:
        if device.identity is None:
            # Busless / non-data-plane devices are not expected on the bus.
            continue
        observed = observed_by_id.get(device.id)
        if observed is None:
            if _scores_absence(source):
                findings.append(
                    Finding(
                        severity="violation",
                        quantity=f"devices/{device.id}/enumeration",
                        observed="device absent on the bus",
                        expected="device present on the bus",
                        owner=device.owner,
                        action="connect the device or reconcile the contract declaration",
                    )
                )
            else:
                # Source cannot see the declaration at all → disclose, never NO-GO.
                findings.append(
                    Finding(
                        severity="not_scored",
                        quantity=f"devices/{device.id}/enumeration",
                        observed="not observed by preflight source",
                        expected="device present on the bus",
                        owner=device.owner,
                        action=(
                            "declared and not seen: enumeration not scored; "
                            "so this device cannot count as measured"
                        ),
                    )
                )
            continue
        findings.extend(_identity_mismatches(device, observed))
    return findings


def _identity_mismatches(device: DeviceInstance, observed: DeviceObservation) -> list[Finding]:
    """Violations only where declared and observed both carry a value (D3)."""
    assert device.identity is not None
    out: list[Finding] = []
    for field in _IDENTITY_FIELDS:
        declared = getattr(device.identity, field)
        seen = getattr(observed, field)
        if declared is None or seen is None:
            continue
        if declared == seen:
            continue
        out.append(
            Finding(
                severity="violation",
                quantity=f"devices/{device.id}/identity/{field}",
                observed=str(seen),
                expected=str(declared),
                owner=device.owner,
                action="reconcile declared identity with what the source sees",
            )
        )
    return out
