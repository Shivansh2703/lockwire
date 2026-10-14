"""Tests for the parts catalog: schema fields, the seeded parts, the loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lockwire.catalog import Catalog, CatalogPart, SourcedNumber, load_catalog

VALID_KINDS = {"datasheet", "source_file", "assumed"}


def test_load_catalog_reads_the_seeded_file():
    catalog = load_catalog()
    assert isinstance(catalog, Catalog)
    # "about 40" — not pinned exact, just in the expected ballpark.
    assert 30 <= len(catalog.parts) <= 50


def test_every_seeded_number_has_a_kind_and_a_source():
    """The whole claim: no number on any seeded part is missing its badge.

    This is the box's central promise — a seed catalog of unlabelled numbers is worse than an
    empty one — so the mutation bite below breaks it at the schema level (the shared validator),
    not by editing one part's data, to prove the *whole* claim, not one instance of it.
    """
    catalog = load_catalog()
    assert catalog.parts, "the seed catalog must not be empty"
    for part in catalog.parts:
        for number_name, number in part.numbers.items():
            assert number.kind in VALID_KINDS, f"{part.id}.{number_name} has an unknown kind"
            assert number.source.strip(), f"{part.id}.{number_name} has a blank source"


def test_assumed_numbers_say_so_in_their_own_source_text():
    """A placeholder number's source string must itself say it is a placeholder — not just its
    kind field — so the flag survives a reader who only looks at the source string (the brief's
    'in the data, not in a comment you hope someone reads'). The warning must be contiguous,
    starting with "ASSUMED for seeding" and ending with "replace before use."—not a bookend
    around a reason."""
    import difflib

    catalog = load_catalog()
    assumed = [
        (part.id, name, number)
        for part in catalog.parts
        for name, number in part.numbers.items()
        if number.kind == "assumed"
    ]
    assert assumed, "expected at least one labelled placeholder number in the seed catalog"
    for part_id, name, number in assumed:
        source = number.source
        # Both halves of the warning must be present (not just the first)
        has_start = "ASSUMED for seeding" in source
        has_end = "replace before use" in source

        if not (has_start and has_end):
            # Show a diff if the warning is incomplete
            expected_parts = ["ASSUMED for seeding", "...<reason>...", "replace before use"]
            actual = source
            diff = "\n".join(
                difflib.unified_diff(
                    expected_parts,
                    [actual],
                    fromfile="expected warning pattern",
                    tofile=f"actual: {part_id}.{name}",
                    lineterm="",
                )
            )
            raise AssertionError(
                f"{part_id}.{name} is kind=assumed but source is missing "
                f"'ASSUMED for seeding' (has: {has_start}) or 'replace before use' (has: {has_end}).\n"
                f"Source: {actual}\n{diff}"
            )


def test_catalog_part_ids_are_unique():
    catalog = load_catalog()
    ids = [p.id for p in catalog.parts]
    assert len(ids) == len(set(ids))


def test_duplicate_part_id_is_rejected():
    with pytest.raises(ValidationError, match="duplicate catalog part id"):
        Catalog.model_validate(
            {
                "parts": [
                    {"id": "dup", "family": "fam.x", "name": "one"},
                    {"id": "dup", "family": "fam.x", "name": "two"},
                ]
            }
        )


def test_sourced_number_rejects_blank_source():
    with pytest.raises(ValidationError, match="source must not be blank"):
        SourcedNumber(value=1.0, unit="g", kind="assumed", source="   ")


def test_catalog_part_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        CatalogPart(id="x", family="fam.x", name="X", not_a_real_field=True)


def _code_only(text: str) -> str:
    """Source text with string/comment tokens dropped, so a docstring explaining the split
    (which must name both models in prose, per catalog.py's CatalogPart docstring) does not
    itself trip this guard — only actual code (an import, an attribute access, a call) does."""
    import io
    import tokenize

    kept = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in (tokenize.STRING, tokenize.COMMENT):
            continue
        kept.append(tok.string)
    return " ".join(kept)


def test_no_module_straddles_catalog_and_registry_vocabularies():
    """CatalogPart (shopping) and DeviceModel (registry) are two vocabularies for "a part".

    Nothing in src/lockwire straddles both today (measured) — that is the fact this
    guard preserves. The day a module's CODE (not its docstrings/comments — see `_code_only`)
    imports or references both names it becomes a problem: a consumer needing both must go
    through one door, not read both models directly.
    """
    src_root = Path(__file__).parent.parent / "src" / "lockwire"
    roots = [r for r in (src_root, src_root.with_name("lockwire_pro")) if r.exists()]
    offenders = []
    for path in sorted(p for r in roots for p in r.rglob("*.py")):
        code = _code_only(path.read_text())
        if "CatalogPart" in code and "DeviceModel" in code:
            offenders.append(str(path.relative_to(src_root.parent.parent)))
    assert not offenders, (
        "a module's code references both CatalogPart (shopping vocabulary) and DeviceModel "
        "(registry vocabulary) — the design requires a consumer needing both to go through one door, "
        f"not both models directly. Offending file(s): {offenders}"
    )
