"""Rigid-body statics for stage one — support reactions, determinate sets only.

``docs/design/e5_solver_choice.md`` chose **numpy-stiffness** by
measurement. This module is the *determinate subset* of that choice, and the
narrowing is deliberate, not a substitution:

* For a **statically determinate** support set the reactions follow from
  equilibrium alone and are **independent of the stiffness EI**. The solver study measured
  exactly that — its ``equilibrium-statics`` candidate matched the textbook
  reactions to 1.9e-16 — and rejected it only because it cannot produce
  *deflections* or answer a *redundant* set.
* For a **redundant** set the reactions *do* depend on EI. The contract schema
  is frozen and declares no Young's modulus, no section and no area on
  ``Link`` or ``Physical``, so there is nothing to solve a redundant set with.
  Those read NOT SCORED naming why, never a plausible-looking guess.

Consequently a stiffness solve has no input it could consume here, and a beam
deflection is not offered at all. What that document lists as unproven —
plates, shear-dominated stubby members, out-of-plane frames, dynamics — is
likewise out of stage one and named in the payload rather than omitted.

Sign convention: reactions are positive upward, against gravity acting along
the root frame's -Z. Positions are scalars on one horizontal span axis.
"""

from __future__ import annotations

STANDARD_G = 9.81
"""m/s^2. The same standard-gravity figure preflight's cross-signal stage
scores an accelerometer's |g| against (``preflight/stages/cross_signal.py``);
kept literal here so this module imports no preflight stage."""


class Indeterminate(ValueError):
    """The support set cannot be answered from the contract alone."""


def weight_n(mass_kg: float, g: float = STANDARD_G) -> float:
    """Weight in newtons of a mass in kilograms."""
    return mass_kg * g


def support_reactions(
    load_n: float, load_pos_m: float, support_pos_m: list[float]
) -> list[tuple[float, float]]:
    """Reaction (force N, moment N*m) at each support, in the order given.

    One support is a fixed interface: it carries the whole load plus the moment
    ``W * (load_pos - support)`` — case 1 in reaction form.

    Two supports are the lever rule — case 3, the two-bolt bracket. Each
    is a simple support, so each carries force and no moment; a support behind
    the load correctly comes out negative (it pulls down).

    Three or more supports on one rigid body are statically indeterminate: the
    split depends on stiffness the contract does not declare. Raises
    ``Indeterminate`` rather than returning a number.
    """
    count = len(support_pos_m)
    if count == 0:
        raise Indeterminate("no mount point is declared, so nothing reacts the load")
    if count == 1:
        return [(load_n, load_n * (load_pos_m - support_pos_m[0]))]
    if count == 2:
        near, far = support_pos_m
        if near == far:
            raise Indeterminate(
                "both mount points sit at the same place on the span axis, "
                "so the load split between them is undetermined"
            )
        reaction_far = load_n * (load_pos_m - near) / (far - near)
        return [(load_n - reaction_far, 0.0), (reaction_far, 0.0)]
    raise Indeterminate(
        f"{count} mount points on one rigid body is statically indeterminate; the load "
        "split depends on the structure's stiffness (Young's modulus and section), which "
        "the contract schema does not declare"
    )
