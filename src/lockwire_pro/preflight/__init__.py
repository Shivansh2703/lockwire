"""Preflight gate package.

B85 lands the machine-state seam. B86 adds the stage runner and ``preflight``
CLI wiring; stage bodies live under ``preflight.stages``.
"""

from lockwire_pro.preflight.runner import (
    STAGES,
    SamplesUnavailable,
    StageEntry,
    run_preflight,
)
from lockwire_pro.preflight.source import (
    DeviceObservation,
    GraphUnavailable,
    LiveSource,
    LivenessObservation,
    PreflightSource,
    RecordedSource,
)

__all__ = [
    "DeviceObservation",
    "GraphUnavailable",
    "LiveSource",
    "LivenessObservation",
    "PreflightSource",
    "RecordedSource",
    "SamplesUnavailable",
    "STAGES",
    "StageEntry",
    "run_preflight",
]
