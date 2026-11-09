"""Stage 1 — driver / node liveness (declared Driver vs source observation)."""

from __future__ import annotations

from lockwire_pro.preflight.source import LivenessObservation, PreflightSource
from lockwire.report import Finding
from lockwire.schema import Contract, DeviceInstance

IMPLEMENTED = True

# Driver fields compared when BOTH operands are populated (D3).
_DRIVER_FIELDS = ("node", "topic", "nominal_rate_hz", "payload_bytes")


def _scores_absence(source: PreflightSource) -> bool:
    """Live bus observers score a missing driver as NO-GO; RecordedSource cannot."""
    return bool(getattr(source, "scores_absence", False))


def check(contract: Contract, source: PreflightSource) -> list[Finding]:
    """Compare declared drivers to what the source reports as live."""
    live_by_id = {obs.device_id: obs for obs in source.liveness()}
    findings: list[Finding] = []
    for device in contract.devices:
        if device.driver is None:
            continue
        observed = live_by_id.get(device.id)
        if observed is None:
            if _scores_absence(source):
                findings.append(
                    Finding(
                        severity="violation",
                        quantity=f"devices/{device.id}/liveness",
                        observed="driver / node not up",
                        expected="driver / node up",
                        owner=device.owner,
                        action=(
                            "check ROS_DOMAIN_ID and QoS (reliability, depth) match the driver "
                            "before concluding the driver is down; then start it or reconcile "
                            "the contract declaration"
                        ),
                    )
                )
            else:
                findings.append(
                    Finding(
                        severity="not_scored",
                        quantity=f"devices/{device.id}/liveness",
                        observed="not observed live by preflight source",
                        expected="driver / node up",
                        owner=device.owner,
                        action=(
                            "declared and not seen: liveness not scored; "
                            "so this device cannot count as measured"
                        ),
                    )
                )
            continue
        findings.extend(_missing_operand_findings(device, observed))
        findings.extend(_driver_mismatches(device, observed))
    return findings


def _missing_operand_findings(
    device: DeviceInstance, observed: LivenessObservation
) -> list[Finding]:
    """eb-22: a populated-but-unseen operand is worded by WHICH operand it is.

    A node absent from the ROS graph is a real no-go — a declared driver is
    not running. A topic absent alone is weak evidence: composable nodes and
    intra-process topics genuinely do not appear in ``ros2 topic list``, so
    treating that the same as a missing node produces a confident false
    NO-GO. The two must never render the same wording.
    """
    out: list[Finding] = []
    if observed.node_missing:
        out.append(
            Finding(
                severity="violation",
                quantity=f"devices/{device.id}/liveness",
                observed="node not on the ROS graph",
                expected="driver node up",
                owner=device.owner,
                action=(
                    "absence of the node is still a real no-go: check ROS_DOMAIN_ID and QoS "
                    "before concluding the driver is down; then start it or reconcile the "
                    "contract declaration"
                ),
            )
        )
    if observed.topic_missing:
        out.append(
            Finding(
                severity="not_scored",
                quantity=f"devices/{device.id}/liveness/topic",
                observed=(
                    "topic not on the ROS graph (composable / intra-process topics do not "
                    "appear here)"
                ),
                expected="driver topic up",
                owner=device.owner,
                action=(
                    "a topic absent alone is weak evidence, not a fault; do not NO-GO on it — "
                    "confirm via the node or another signal before treating the driver as down"
                ),
            )
        )
    return out


def _driver_mismatches(device: DeviceInstance, observed: LivenessObservation) -> list[Finding]:
    """Violations only where declared and observed both carry a value (D3)."""
    assert device.driver is not None
    out: list[Finding] = []
    for field in _DRIVER_FIELDS:
        declared = getattr(device.driver, field)
        seen = getattr(observed, field)
        if declared is None or seen is None:
            continue
        if declared == seen:
            continue
        out.append(
            Finding(
                severity="violation",
                quantity=f"devices/{device.id}/driver/{field}",
                observed=str(seen),
                expected=str(declared),
                owner=device.owner,
                action="reconcile declared driver with what the source sees as live",
            )
        )
    return out
