"""Shared context-envelope views for preflight stages and score accounting."""

from __future__ import annotations

from dataclasses import dataclass

from lockwire.schema import (
    ContextEnvelope,
    DeviceInstance,
    FittedBandEvidence,
    SignalEnvelope,
)


@dataclass(frozen=True, slots=True)
class SignalDeclaration:
    """One signal and evidence from the exact envelope that declares it."""

    signal: SignalEnvelope
    fitted: FittedBandEvidence | None
    envelope: ContextEnvelope


@dataclass(frozen=True, slots=True)
class ContextSignalView:
    """All matching envelopes and their uniquely declared signal channels."""

    envelopes: tuple[ContextEnvelope, ...]
    signals: dict[str, SignalDeclaration]


def signal_view_for_context(device: DeviceInstance, context: str) -> ContextSignalView:
    """Merge signal declarations across every envelope for ``context``.

    Fitted evidence stays coupled to its declaring envelope as defence-in-depth
    behind schema validation of fitted-band declarations. A duplicate signal is
    a contract defect: provenance cannot be resolved by envelope order.
    """
    envelopes: list[ContextEnvelope] = []
    signals: dict[str, SignalDeclaration] = {}

    for envelope in device.envelopes:
        if envelope.context != context:
            continue
        envelopes.append(envelope)
        for channel, signal in envelope.signals.items():
            previous = signals.get(channel)
            if previous is not None:
                first = previous.envelope
                raise ValueError(
                    f"duplicate signal channel {channel!r} for device {device.id!r}: "
                    f"context {first.context!r} / witness "
                    f"{first.provenance.witness!r} and context {envelope.context!r} / "
                    f"witness {envelope.provenance.witness!r}"
                )
            signals[channel] = SignalDeclaration(
                signal=signal,
                fitted=envelope.fitted_bands.get(f"signals/{channel}"),
                envelope=envelope,
            )

    return ContextSignalView(envelopes=tuple(envelopes), signals=signals)
