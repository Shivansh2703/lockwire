"""`check --format csv|tsv` and the app's findings-export route: one row per finding."""

import csv
import io

from lockwire.copy import check_label, fix_text
from lockwire.report import Finding, verdict, verdict_word

COLUMNS = ("severity", "about", "observed", "expected", "units", "source", "action")


def _severity(f: Finding) -> str:
    return "advice: not yet sourced" if f.advice_unsourced else f.severity


def rows(findings: list[Finding]) -> list[dict[str, str]]:
    """The finding rows, in report order, each with a `plain` sentence the app can copy."""
    out = []
    for f in findings:
        unit = f.observed_quantity.unit if f.observed_quantity else ""
        row = {
            "severity": _severity(f),
            "about": check_label(f.quantity),
            "observed": f.observed,
            "expected": f.expected,
            "units": unit,
            "source": f.source or "",
            "action": fix_text(f.action) if f.action else "",
        }
        row["plain"] = (
            f"{row['about']}: observed {f.observed}, expected {f.expected}. {row['action']}".strip()
        )
        out.append(row)
    return out


def verdict_row(counting: list[Finding]) -> dict[str, str]:
    """First row of the table: severity VERDICT, the word in `about`, the counts in `observed`."""
    v = verdict(counting)
    counts = f"{v.violations} violation(s), {v.warnings} warning(s), {v.not_scored} not scored"
    return {"severity": "VERDICT", "about": verdict_word(v.code), "observed": counts}


def table(findings: list[Finding], counting: list[Finding], delimiter: str) -> str:
    """CSV (`,`) or TSV (tab) text: header, the VERDICT row, then one row per finding."""
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, COLUMNS, delimiter=delimiter, extrasaction="ignore", restval="", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows([verdict_row(counting), *rows(findings)])
    return buf.getvalue()
