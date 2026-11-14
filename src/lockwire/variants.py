"""One in-memory contract per variant, and a check run over each (never written to disk)."""

from __future__ import annotations

from typing import Any

from lockwire.schema import Contract

BASE = "base"


def variant_contracts(contract: Contract) -> list[tuple[str, Contract]]:
    """[(name, contract)]: each variant's param overrides applied and re-validated.

    No variants -> just ("base", contract). Derived masses and joint offsets follow the params.
    """
    if not contract.variants:
        return [(BASE, contract)]
    out = []
    for variant in contract.variants:
        data = contract.to_file_data(mode="json", exclude_none=True)
        data.pop("variants", None)
        data.pop("active_variant", None)
        for param in data["params"]:
            param["value"] = variant.params.get(param["name"], param["value"])
        try:
            out.append((variant.name, Contract.model_validate(data)))
        except ValueError as error:
            msg = (
                error.errors()[0]["msg"].removeprefix("Value error, ")
                if hasattr(error, "errors")
                else str(error)
            )
            raise ValueError(
                f"variant {variant.name!r} sets a value outside its range: {msg}"
            ) from error
    return out


def check_variants(contract: Contract, **kwargs: Any) -> list[tuple[str, str, list]]:
    """Run the record checks once per variant: [(variant name, verdict code, findings)]."""
    from lockwire.checks import run_record_checks  # noqa: PLC0415
    from lockwire.report import verdict  # noqa: PLC0415

    rows = []
    for name, variant in variant_contracts(contract):
        findings = run_record_checks(variant, **kwargs)
        rows.append((name, verdict(findings).code, findings))
    return rows
