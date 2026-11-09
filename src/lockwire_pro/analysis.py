"""Fail-closed structural admission and bounded direct linear-static execution.

The Job controller owns this analysis child; a dedicated child owns CalculiX.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from lockwire import units
from lockwire.edit import _contract_lock
from lockwire.schema import (
    MAX_CONTRACT_BYTES,
    InputArtifact,
    ScalarAgreementLimit,
    SeriesAmplitude,
    StructuralProcedure,
    load_contract_bytes,
    read_artifact,
)

MAX_PACKAGE_MANIFEST_BYTES = 256 * 1024
MAX_ANALYSIS_ARTIFACT_BYTES = 64 * 1024 * 1024
_NODE_PRINT_ROW = re.compile(
    r"^\s*(?P<node>\d+)\s+(?P<x>[+-]?[\d.]+[Ee][+-]?\d+)\s+"
    r"(?P<y>[+-]?[\d.]+[Ee][+-]?\d+)\s+(?P<z>[+-]?[\d.]+[Ee][+-]?\d+)\s*$"
)


class AnalysisRefused(ValueError):
    """The declaration was rejected before solver process start."""


class _ClosedManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalculixProfile(_ClosedManifestModel):
    backend: Literal["calculix_ccx"]
    solver_version: Literal["2.23"]
    offline: Literal[True]
    admitted_procedures: list[
        Literal[
            "linear_static",
            "modal",
            "harmonic",
            "transient",
            "nonlinear_static",
            "impact",
        ]
    ]
    undamped_procedures: list[Literal["modal", "harmonic", "transient", "impact"]] = []
    procedure_templates: dict[str, str]
    allowed_artifact_formats: list[
        Literal[
            "urdf",
            "sdf",
            "calculix_inp",
            "mesh",
            "json",
            "yaml",
            "csv",
            "world",
            "controller_config",
        ]
    ]
    allowed_identities: list[str] = []
    allowed_includes: list[str] = []
    contact_damping_procedures: list[Literal["transient", "impact"]] = []

    @field_validator("procedure_templates")
    @classmethod
    def _safe_template_keys(cls, value: dict[str, str]) -> dict[str, str]:
        safe = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
        if not value or any(not safe.fullmatch(item) for item in value.values()):
            raise ValueError("procedure templates must use nonempty safe manifest keys")
        return value


class AnalysisPackageManifest(_ClosedManifestModel):
    manifest_version: Literal["1"]
    profiles: dict[str, CalculixProfile]


@dataclass(frozen=True)
class _DirectDeckAdmission:
    requests: set[tuple[str, str, int]]
    span_m: float | None = None
    nodes: dict[int, tuple[float, float, float]] | None = None


@dataclass(frozen=True)
class _SlidingContactAdmission:
    final_time_s: float = 1.0
    contact_rows: int = 28
    history_blocks: int = 1
    tangential_displacement_m: float = 0.02
    friction_coefficient: float = 0.2
    normal_load_n: float = 40.0


_SLIDING_CONTACT_SHA256 = "7b657e3d8b355f1ef70dd15164f7dcd0e99850f2c3496a5a47f57d48369f7905"
_SLIDING_CONTACT_EXTENDED_SHA256 = (
    "4fbcb4e7020bdbf6a54e350f355c33c3da1dec4cff0bfb088e62b8eb36a7f9ee"
)
_FRICTIONLESS_IMPACT_SHA256 = "b08b1741220948f63e506ab4bb4d5094fa49a8cb89b7722e1dc3407ccce18a21"
_SLIDING_CONTACT_PAIR = "UPPER_BOTTOM, LOWER_TOP"
_CCX_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?"


def parse_linear_static_node_print(
    text: str,
) -> dict[tuple[str, str, int], tuple[float, float, float]]:
    """Parse only CalculiX's benchmark-proven ``*NODE PRINT`` U/RF rows."""

    values: dict[tuple[str, str, int], tuple[float, float, float]] = {}
    seen_headings = set()
    kind = node_set = None
    preamble = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if preamble == 1:
            if line.split() != ["INCREMENT", "1"]:
                raise AnalysisRefused("malformed or unsupported node-print preamble")
            preamble = 2
            continue
        if not seen_headings and preamble == 0 and line.split() == ["S", "T", "E", "P", "1"]:
            preamble = 1
            continue
        heading = re.fullmatch(
            r"\s*(displacements|forces) \((vx,vy,vz|fx,fy,fz)\) for set ([A-Z][A-Z0-9_]*)"
            r"(?: and time\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?))?\s*",
            line,
        )
        if heading:
            kind = "U" if heading.group(1) == "displacements" else "RF"
            node_set = heading.group(3)
            if heading.group(2) != ("vx,vy,vz" if kind == "U" else "fx,fy,fz"):
                raise AnalysisRefused("unsupported node-print component aliases")
            if (kind, node_set) in seen_headings:
                raise AnalysisRefused("duplicate node-print heading")
            seen_headings.add((kind, node_set))
            if heading.group(4) is not None and not math.isfinite(float(heading.group(4))):
                raise AnalysisRefused("nonfinite node-print time")
            continue
        row = _NODE_PRINT_ROW.match(line)
        if row and kind and node_set:
            key = (kind, node_set, int(row["node"]))
            if key in values:
                raise AnalysisRefused("duplicate U/RF result")
            result = tuple(float(row[name]) for name in ("x", "y", "z"))
            if not all(math.isfinite(value) for value in result):
                raise AnalysisRefused("nonfinite U/RF result")
            values[key] = result
        elif line.strip():
            raise AnalysisRefused("malformed or unsupported node-print output")
    if preamble == 1 or not values:
        raise AnalysisRefused("missing or incomplete node-print results")
    return values


def parse_modal_frequencies(text: str) -> list[tuple[int, float]]:
    """Parse the measured 2.23 modal table, refusing every other ``.dat`` layout."""
    rows: list[tuple[int, float]] = []
    in_table = False
    saw_heading = False
    completed = False
    pattern = re.compile(
        r"^\s*(?P<mode>[1-9][0-9]*)\s+"
        r"(?P<eigen>[+-]?[\d.]+E[+-]?\d+)\s+"
        r"(?P<angular>[+-]?[\d.]+E[+-]?\d+)\s+"
        r"(?P<frequency>[+-]?[\d.]+E[+-]?\d+)\s+"
        r"(?P<imaginary>[+-]?[\d.]+E[+-]?\d+)\s*$"
    )
    for line in text.splitlines():
        if "MODE NO    EIGENVALUE" in line:
            if saw_heading:
                raise AnalysisRefused("duplicate modal eigenvalue table")
            saw_heading = in_table = True
            continue
        if in_table and "".join(line.split()) == "PARTICIPATIONFACTORS":
            in_table = False
            completed = True
            continue
        if not in_table or not line.strip() or "REAL PART" in line or "RAD/TIME" in line:
            continue
        match = pattern.fullmatch(line)
        if match is None:
            raise AnalysisRefused("malformed modal eigenvalue row")
        mode = int(match["mode"])
        values = [float(match[name]) for name in ("eigen", "angular", "frequency", "imaginary")]
        if (
            mode != len(rows) + 1
            or not all(math.isfinite(value) for value in values)
            or values[0] <= 0
            or values[1] <= 0
            or values[2] <= 0
            or values[3] != 0
        ):
            raise AnalysisRefused("invalid or unsupported modal eigenvalue row")
        rows.append((mode, values[2]))
    if not saw_heading or not rows:
        raise AnalysisRefused("modal eigenvalue output is missing")
    if not completed:
        raise AnalysisRefused("incomplete modal eigenvalue table")
    return rows


def _admit_direct_truss_deck(
    data: bytes,
    *,
    declared_material: tuple[float, float] | None = None,
    declared_area: float | None = None,
    declared_element_set: str | None = None,
    declared_material_name: str | None = None,
    declared_boundaries: set[tuple[str, int]] | None = None,
    declared_loads: dict[tuple[str, int], float] | None = None,
    b31_square: bool = False,
    declared_side: float | None = None,
    declared_orientation: tuple[float, float, float] | None = None,
) -> _DirectDeckAdmission:
    """Closed records/options, not CalculiX's permissive keyword abbreviations.

    Only the retained T3D2 truss and square B31 cantilever linear-static
    languages are owned. Unsupported extensions are refused before launch.
    """
    if len(data) > 1024 * 1024:
        raise AnalysisRefused("direct truss deck exceeds 1 MiB")
    options = {
        "*HEADING": set(),
        "*NODE": set(),
        "*ELEMENT": {"TYPE", "ELSET"},
        "*NSET": {"NSET"},
        "*ELSET": {"ELSET"},
        "*MATERIAL": {"NAME"},
        "*ELASTIC": set(),
        "*SOLID SECTION": {"ELSET", "MATERIAL"},
        "*BEAM SECTION": {"ELSET", "MATERIAL", "SECTION"},
        "*BOUNDARY": set(),
        "*EQUATION": set(),
        "*STEP": set(),
        "*STATIC": set(),
        "*CLOAD": set(),
        "*NODE PRINT": {"NSET", "GLOBAL"},
        "*END STEP": set(),
    }
    blocks: list[tuple[str, dict[str, str], list[list[str]]]] = []
    try:
        for raw in data.decode("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("**"):
                continue
            if line.startswith("*"):
                fields = [value.strip().upper() for value in line.split(",")]
                card = fields[0]
                if card not in options:
                    raise ValueError(f"card {card!r} is not admitted")
                selected = {}
                for field in fields[1:]:
                    key, separator, value = field.partition("=")
                    if not separator or key not in options[card] or key in selected or not value:
                        raise ValueError("unknown, repeated or malformed card option")
                    selected[key] = value
                required = options[card] - {"GLOBAL"}
                if not required <= set(selected):
                    raise ValueError("missing card option")
                if card == "*ELEMENT" and selected["TYPE"] != ("B31" if b31_square else "T3D2"):
                    raise ValueError("element type differs from the admitted template")
                if card == "*BEAM SECTION" and selected["SECTION"] != "RECT":
                    raise ValueError("only rectangular B31 sections are admitted")
                if card == "*NODE PRINT" and selected.get("GLOBAL", "YES") != "YES":
                    raise ValueError("only global U/RF results are admitted")
                if any(
                    not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value) for value in selected.values()
                ):
                    raise ValueError("unsafe or unsupported identity")
                blocks.append((card, selected, []))
            else:
                if not blocks:
                    raise ValueError("record without a card")
                blocks[-1][2].append([value.strip().upper() for value in line.split(",")])

        def integer(value: str) -> int:
            if not re.fullmatch(r"[1-9][0-9]{0,8}", value):
                raise ValueError("positive integer record required")
            return int(value)

        def number(value: str) -> float:
            if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?", value):
                raise ValueError("finite decimal record required")
            result = float(value)
            if not math.isfinite(result):
                raise ValueError("nonfinite record")
            return result

        def arity(row: list[str], size: int) -> None:
            if len(row) != size:
                raise ValueError("unsupported record arity")

        nodes: dict[int, tuple[float, ...]] = {}
        elements: dict[int, tuple[int, int]] = {}
        nsets: dict[str, set[int]] = {}
        elsets: dict[str, set[int]] = {}
        materials: set[str] = set()
        section_sets: set[str] = set()
        requests: set[tuple[str, str, int]] = set()
        boundary_keys = set()
        boundary_records: set[tuple[str, int]] = set()
        dependent_keys = set()
        load_keys = set()
        load_records: dict[tuple[str, int], float] = {}
        once = set()
        phase = "model"
        material = None

        def node_members(value: str) -> set[int]:
            members = {integer(value)} if value.isdigit() else nsets.get(value)
            if not members or not members <= nodes.keys():
                raise ValueError("unknown node or node set")
            return members

        for index, (card, selected, rows) in enumerate(blocks):
            if card in {
                "*HEADING",
                "*NODE",
                "*ELEMENT",
                "*BOUNDARY",
                "*STEP",
                "*STATIC",
                "*CLOAD",
                "*END STEP",
            }:
                if card in once:
                    raise ValueError("repeated singleton card")
                once.add(card)
            if card == "*HEADING":
                if index != 0 or rows:
                    raise ValueError("heading must be first and have no records")
            elif card == "*STEP":
                if phase != "model" or rows:
                    raise ValueError("invalid step")
                phase = "step"
            elif card == "*STATIC":
                if phase != "step" or rows:
                    raise ValueError("only default linear static is admitted")
                phase = "static"
            elif card == "*END STEP":
                if phase != "static" or rows or index != len(blocks) - 1:
                    raise ValueError("invalid end step")
                phase = "ended"
            elif card in {"*CLOAD", "*NODE PRINT"}:
                if phase != "static" or not rows:
                    raise ValueError("static load/output required inside static step")
                if card == "*CLOAD":
                    for row in rows:
                        arity(row, 3)
                        dof = integer(row[1])
                        if dof > 3:
                            raise ValueError("only translational force DOFs 1..3 are admitted")
                        value = number(row[2])
                        record = (row[0], dof)
                        if record in load_records:
                            raise ValueError("duplicate load record")
                        load_records[record] = value
                        for node in node_members(row[0]):
                            key = (node, dof)
                            if key in load_keys:
                                raise ValueError("duplicate load")
                            load_keys.add(key)
                else:
                    if len(rows) != 1 or rows[0] not in [["U"], ["RF"]]:
                        raise ValueError("only one U or RF node output record is admitted")
                    for node in node_members(selected["NSET"]):
                        key = (rows[0][0], selected["NSET"], node)
                        if key in requests:
                            raise ValueError("duplicate node output")
                        requests.add(key)
            else:
                if phase != "model" or not rows and card != "*MATERIAL":
                    raise ValueError("model record required outside step")
                if card == "*NODE":
                    for row in rows:
                        arity(row, 4)
                        node = integer(row[0])
                        if node in nodes:
                            raise ValueError("duplicate node")
                        nodes[node] = tuple(number(value) for value in row[1:])
                elif card == "*ELEMENT":
                    if selected["ELSET"] in elsets:
                        raise ValueError("duplicate element set")
                    members = set()
                    for row in rows:
                        arity(row, 3)
                        element, left, right = map(integer, row)
                        if (
                            element in elements
                            or left == right
                            or left not in nodes
                            or right not in nodes
                            or nodes[left] == nodes[right]
                        ):
                            raise ValueError("duplicate or invalid truss connectivity")
                        elements[element] = (left, right)
                        members.add(element)
                    elsets[selected["ELSET"]] = members
                elif card in {"*NSET", "*ELSET"}:
                    sets, known = (nsets, nodes) if card == "*NSET" else (elsets, elements)
                    name = selected[card[1:]]
                    members = [integer(value) for row in rows for value in row]
                    if (
                        name in sets
                        or len(members) != len(set(members))
                        or not set(members) <= known.keys()
                    ):
                        raise ValueError("duplicate or unknown set members")
                    sets[name] = set(members)
                elif card == "*MATERIAL":
                    material = selected["NAME"]
                    if rows or material in materials:
                        raise ValueError("duplicate or malformed material")
                    materials.add(material)
                    if declared_material_name is not None and material != declared_material_name:
                        raise ValueError("deck material identity differs from the declaration")
                    if index + 1 == len(blocks) or blocks[index + 1][0] != "*ELASTIC":
                        raise ValueError("material requires linear elastic record")
                elif card == "*ELASTIC":
                    if not material or blocks[index - 1][0] != "*MATERIAL" or len(rows) != 1:
                        raise ValueError("elastic record must follow material")
                    arity(rows[0], 2)
                    if number(rows[0][0]) <= 0 or not -1 < number(rows[0][1]) < 0.5:
                        raise ValueError("invalid linear isotropic material")
                    if declared_material is not None and any(
                        not math.isclose(number(value), expected, rel_tol=1e-12, abs_tol=0)
                        for value, expected in zip(rows[0], declared_material, strict=True)
                    ):
                        raise ValueError("deck elastic material differs from the declaration")
                elif card == "*SOLID SECTION":
                    if b31_square:
                        raise ValueError("B31 requires a beam section")
                    name = selected["ELSET"]
                    if (
                        name not in elsets
                        or name in section_sets
                        or selected["MATERIAL"] not in materials
                        or len(rows) != 1
                    ):
                        raise ValueError("duplicate or unknown section binding")
                    arity(rows[0], 1)
                    if number(rows[0][0]) <= 0:
                        raise ValueError("truss area must be positive")
                    if declared_area is not None and not math.isclose(
                        number(rows[0][0]), declared_area, rel_tol=1e-12, abs_tol=0
                    ):
                        raise ValueError("deck area differs from the declaration")
                    section_sets.add(name)
                    if declared_element_set is not None and name != declared_element_set:
                        raise ValueError("deck element set differs from the declaration")
                elif card == "*BEAM SECTION":
                    if not b31_square:
                        raise ValueError("truss requires a solid section")
                    name = selected["ELSET"]
                    if (
                        name not in elsets
                        or name in section_sets
                        or selected["MATERIAL"] not in materials
                        or len(rows) != 2
                    ):
                        raise ValueError("duplicate or unknown beam section binding")
                    arity(rows[0], 2)
                    arity(rows[1], 3)
                    width, height = map(number, rows[0])
                    direction = tuple(map(number, rows[1]))
                    if width <= 0 or height <= 0 or not math.isclose(width, height, rel_tol=1e-12):
                        raise ValueError("only positive square B31 sections are admitted")
                    if declared_side is not None and not math.isclose(
                        width, declared_side, rel_tol=1e-12
                    ):
                        raise ValueError("deck beam size differs from the declaration")
                    if declared_orientation is not None and any(
                        not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)
                        for actual, expected in zip(direction, declared_orientation, strict=True)
                    ):
                        raise ValueError("deck beam orientation differs from the declaration")
                    section_sets.add(name)
                    if declared_element_set is not None and name != declared_element_set:
                        raise ValueError("deck element set differs from the declaration")
                elif card == "*BOUNDARY":
                    for row in rows:
                        if len(row) not in {3, 4}:
                            raise ValueError("invalid boundary arity")
                        start, end = integer(row[1]), integer(row[2])
                        if (
                            not 1 <= start <= end <= (6 if b31_square else 3)
                            or len(row) == 4
                            and number(row[3]) != 0
                        ):
                            raise ValueError("only zero supported boundary DOFs are admitted")
                        for dof in range(start, end + 1):
                            boundary_records.add((row[0], dof))
                        for node in node_members(row[0]):
                            for dof in range(start, end + 1):
                                key = (node, dof)
                                if key in boundary_keys:
                                    raise ValueError("duplicate boundary")
                                boundary_keys.add(key)
                elif card == "*EQUATION":
                    if b31_square or declared_boundaries is not None:
                        raise ValueError("declared direct truss has no equation mapping")
                    if len(rows) != 2 or len(rows[0]) != 1:
                        raise ValueError("unsupported equation records")
                    terms = integer(rows[0][0])
                    if terms != 2:
                        raise ValueError(
                            "only the fixture's two-term linear constraint is admitted"
                        )
                    arity(rows[1], 3 * terms)
                    dependent = tuple(map(integer, rows[1][:2]))
                    if dependent in dependent_keys or dependent in boundary_keys:
                        raise ValueError("duplicate or constrained dependent equation DOF")
                    dependent_keys.add(dependent)
                    seen = set()
                    for offset in range(0, len(rows[1]), 3):
                        node, dof = map(integer, rows[1][offset : offset + 2])
                        if (
                            node not in nodes
                            or dof > 3
                            or (node, dof) in seen
                            or number(rows[1][offset + 2]) == 0
                        ):
                            raise ValueError("invalid or duplicate equation term")
                        seen.add((node, dof))
        covered = {element for name in section_sets for element in elsets[name]}
        if len(materials) != 1 or len(section_sets) != 1:
            raise ValueError("only one declared material and section are admitted")
        if (
            phase != "ended"
            or not nodes
            or not elements
            or covered != set(elements)
            or not requests
            or not boundary_keys
            or not load_keys
        ):
            raise ValueError("incomplete single linear-static deck")
        span_m = None
        if b31_square:
            if declared_boundaries is None or declared_loads is None:
                raise ValueError("B31 requires bound root and tip declarations")
            root_names = {name for name, _ in declared_boundaries}
            tip_names = {name for name, _ in declared_loads}
            if len(root_names) != 1 or len(tip_names) != 1 or root_names == tip_names:
                raise ValueError("B31 requires distinct root and tip node sets")
            root_set, tip_set = next(iter(root_names)), next(iter(tip_names))
            if len(nsets.get(root_set, set())) != 1 or len(nsets.get(tip_set, set())) != 1:
                raise ValueError("B31 root and tip sets must be single nodes")
            root, tip = next(iter(nsets[root_set])), next(iter(nsets[tip_set]))
            links: dict[int, set[int]] = {node: set() for node in nodes}
            for left, right in elements.values():
                if right in links[left]:
                    raise ValueError("B31 repeats a beam link")
                links[left].add(right)
                links[right].add(left)
            if (
                root == tip
                or len(elements) != len(nodes) - 1
                or {node for node, neighbors in links.items() if len(neighbors) == 1} != {root, tip}
                or any(len(neighbors) not in {1, 2} for neighbors in links.values())
            ):
                raise ValueError("B31 requires one unbranched root-to-tip chain")
            ordered = [root]
            previous = None
            while ordered[-1] != tip:
                following = links[ordered[-1]] - ({previous} if previous is not None else set())
                if len(following) != 1 or next(iter(following)) in ordered:
                    raise ValueError("B31 chain is disconnected or cyclic")
                previous = ordered[-1]
                ordered.append(next(iter(following)))
            if len(ordered) != len(nodes):
                raise ValueError("B31 chain has unused or disconnected nodes")
            first_y, first_z = nodes[root][1:]
            if any(
                not math.isclose(nodes[node][1], first_y, rel_tol=0, abs_tol=1e-12)
                or not math.isclose(nodes[node][2], first_z, rel_tol=0, abs_tol=1e-12)
                for node in ordered
            ) or any(nodes[right][0] <= nodes[left][0] for left, right in pairwise(ordered)):
                raise ValueError("B31 requires a straight increasing x-axis chain")
            span_m = nodes[tip][0] - nodes[root][0]
        if declared_boundaries is not None and boundary_records != declared_boundaries:
            raise ValueError("deck boundaries differ from the declaration")
        if declared_loads is not None and (
            load_records.keys() != declared_loads.keys()
            or any(
                not math.isclose(load_records[key], value, rel_tol=1e-12, abs_tol=0)
                for key, value in declared_loads.items()
            )
        ):
            raise ValueError("deck loads differ from the declaration")
        return _DirectDeckAdmission(requests, span_m)
    except (UnicodeError, ValueError, KeyError) as error:
        raise AnalysisRefused(f"direct {'B31' if b31_square else 'truss'} deck: {error}") from error


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AnalysisRefused(f"JSON repeats key {key!r}")
        result[key] = value
    return result


def _load_package_manifest(path: Path) -> tuple[AnalysisPackageManifest, str]:
    try:
        if not path.is_file():
            raise AnalysisRefused("package manifest is not a regular file")
        if path.stat().st_size > MAX_PACKAGE_MANIFEST_BYTES:
            raise AnalysisRefused("package manifest exceeds the size limit")
        raw = path.read_bytes()
        if len(raw) > MAX_PACKAGE_MANIFEST_BYTES:
            raise AnalysisRefused("package manifest exceeds the size limit")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
        return AnalysisPackageManifest.model_validate(document), hashlib.sha256(raw).hexdigest()
    except AnalysisRefused:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise AnalysisRefused(f"invalid package manifest: {error}") from error


def _unsafe_reference(value: str) -> bool:
    stripped = value.strip()
    return (
        "://" in stripped
        or stripped.startswith(("/", "\\"))
        or "$(" in stripped
        or "${" in stripped
        or "`" in stripped
        or "\x00" in stripped
    )


def _inspect_xml(data: bytes, profile: CalculixProfile, artifact: InputArtifact) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AnalysisRefused(f"{artifact.role}: XML artifact is not UTF-8") from error
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise AnalysisRefused(f"{artifact.role}: XML declarations/entities are not admitted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise AnalysisRefused(f"{artifact.role}: invalid XML: {error}") from error

    allowed_identities = set(profile.allowed_identities)
    allowed_includes = set(profile.allowed_includes)
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        values = [value.strip() for value in element.attrib.values()]
        if element.text and element.text.strip():
            values.append(element.text.strip())
        for value in values:
            if _unsafe_reference(value):
                raise AnalysisRefused(f"{artifact.role}: unsafe nested reference {value!r}")
        if tag in {"plugin", "controller", "transmission"}:
            identity = element.attrib.get("filename") or element.attrib.get("name")
            if not identity or identity not in allowed_identities:
                raise AnalysisRefused(
                    f"{artifact.role}: nested {tag} identity {identity!r} is not admitted"
                )
        if tag in {"uri", "include", "mesh"}:
            reference = (element.text or "").strip() or element.attrib.get("filename", "")
            if reference and reference not in allowed_includes:
                raise AnalysisRefused(
                    f"{artifact.role}: nested include {reference!r} is not admitted"
                )


def _inspect_calculix_input(data: bytes, profile: CalculixProfile, artifact: InputArtifact) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AnalysisRefused(f"{artifact.role}: CalculiX input is not UTF-8") from error
    allowed_includes = set(profile.allowed_includes)
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("**"):
            continue
        if _unsafe_reference(line):
            raise AnalysisRefused(
                f"{artifact.role}:{line_number}: unsafe substitution/path/reference"
            )
        if not line.startswith("*"):
            continue
        keyword = line.split(",", 1)[0].upper()
        if "USER" in keyword or keyword in {"*INCLUDE FILE", "*SYSTEM CALL"}:
            raise AnalysisRefused(
                f"{artifact.role}:{line_number}: user/executable card {keyword!r} is forbidden"
            )
        if keyword == "*INCLUDE":
            match = re.search(r"\bINPUT\s*=\s*([^,]+)", line, flags=re.IGNORECASE)
            include = match.group(1).strip() if match else ""
            if not include or _unsafe_reference(include) or include not in allowed_includes:
                raise AnalysisRefused(
                    f"{artifact.role}:{line_number}: include {include!r} is not admitted"
                )


def _inspect_data_strings(value: Any, profile: CalculixProfile, role: str) -> None:
    if isinstance(value, str):
        if _unsafe_reference(value):
            raise AnalysisRefused(f"{role}: unsafe nested string {value!r}")
    elif isinstance(value, dict):
        for key, nested in value.items():
            if key.lower() in {"plugin", "controller", "include", "uri", "filename"} and (
                not isinstance(nested, str)
                or nested not in (set(profile.allowed_identities) | set(profile.allowed_includes))
            ):
                raise AnalysisRefused(f"{role}: nested identity {nested!r} is not admitted")
            _inspect_data_strings(nested, profile, role)
    elif isinstance(value, list):
        for nested in value:
            _inspect_data_strings(nested, profile, role)


def _inspect_artifact(data: bytes, profile: CalculixProfile, artifact: InputArtifact) -> None:
    if artifact.format in {"urdf", "sdf", "world"}:
        _inspect_xml(data, profile, artifact)
    elif artifact.format == "calculix_inp":
        _inspect_calculix_input(data, profile, artifact)
    elif artifact.format == "json":
        try:
            document = json.loads(
                data.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
            )
        except (UnicodeError, json.JSONDecodeError) as error:
            raise AnalysisRefused(f"{artifact.role}: invalid JSON artifact") from error
        _inspect_data_strings(document, profile, artifact.role)


def _validate_series(data: bytes, amplitude: SeriesAmplitude) -> None:
    try:
        text = data.decode("utf-8")
        if amplitude.artifact.format == "csv":
            rows: Any = list(csv.DictReader(io.StringIO(text)))
        else:
            rows = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        if (
            not isinstance(rows, list)
            or len(rows) < 2
            or not all(isinstance(row, dict) for row in rows)
        ):
            raise AnalysisRefused("input-series artifact requires at least two object rows")
        times = [float(row[amplitude.time_column]) for row in rows]
        values = [float(row[amplitude.value_column]) for row in rows]
    except AnalysisRefused:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise AnalysisRefused("input-series artifact has invalid declared columns") from error
    if not all(math.isfinite(value) for value in times + values):
        raise AnalysisRefused("input-series time and amplitude values must be finite")
    if any(right <= left for left, right in pairwise(times)):
        raise AnalysisRefused("input-series time must increase strictly")


def _input_artifacts(procedure: StructuralProcedure, structural: Any) -> list[InputArtifact]:
    artifacts = [
        artifact
        for artifact in (structural.model.model_artifact, structural.model.mesh_artifact)
        if artifact is not None
    ]
    selected_loads = {row.id: row for row in structural.loads}
    for load_id in procedure.load_ids:
        amplitude = selected_loads[load_id].amplitude
        if isinstance(amplitude, SeriesAmplitude):
            artifacts.append(amplitude.artifact)
    return artifacts


def _declared_direct_truss(structural: Any, procedure: StructuralProcedure) -> dict[str, Any]:
    """Map the one admitted static truss envelope to exact deck operands."""
    deck = structural.model.model_artifact
    if (
        procedure.kind != "linear_static"
        or deck is None
        or deck.format != "calculix_inp"
        or structural.model.mesh_artifact is not None
        or structural.connections
        or len(procedure.assignment_ids) != 1
        or not procedure.support_ids
        or not procedure.load_ids
        or procedure.mass_ids
        or procedure.contact_ids
        or procedure.damping_ids
        or procedure.geometric_nonlinearity is not None
        or procedure.plasticity is not None
    ):
        raise AnalysisRefused(
            "direct-truss-v1 requires one bound static truss with declared supports and loads"
        )
    assignment = next(
        row for row in structural.assignments if row.id == procedure.assignment_ids[0]
    )
    section = next(row for row in structural.sections if row.id == assignment.section_id)
    material = next(row for row in structural.materials if row.id == section.material_id)
    if (
        assignment.region.kind != "element_set"
        or section.kind != "truss"
        or material.kind != "linear_isotropic"
        or section.area is None
    ):
        raise AnalysisRefused("direct-truss-v1 requires an element-set linear truss section")
    frame = structural.model.source_frame
    boundaries: set[tuple[str, int]] = set()
    for support_id in procedure.support_ids:
        support = next(row for row in structural.supports if row.id == support_id)
        if (
            support.region.kind != "node_set"
            or support.dofs.frame != frame
            or support.dofs.rotation
        ):
            raise AnalysisRefused(
                "direct-truss-v1 supports require node sets and source-frame translations"
            )
        if any(value.base() != 0 for value in support.dofs.prescribed_translation.values()):
            raise AnalysisRefused("direct-truss-v1 supports require zero displacement")
        for axis in support.dofs.translation:
            key = (support.region.name.upper(), "xyz".index(axis) + 1)
            if key in boundaries:
                raise AnalysisRefused("direct-truss-v1 repeats a support DOF")
            boundaries.add(key)
    loads: dict[tuple[str, int], float] = {}
    for load_id in procedure.load_ids:
        load = next(row for row in structural.loads if row.id == load_id)
        if (
            load.kind != "force"
            or load.region.kind != "node_set"
            or load.application_frame != frame
            or load.amplitude is not None
            or load.vector is None
        ):
            raise AnalysisRefused(
                "direct-truss-v1 loads require constant source-frame node-set force"
            )
        for dof, component in enumerate(load.vector.values, 1):
            value = units.convert(component, load.vector.unit, "N")
            if value == 0:
                continue
            key = (load.region.name.upper(), dof)
            if key in loads:
                raise AnalysisRefused("direct-truss-v1 repeats a force DOF")
            loads[key] = value
    if not loads:
        raise AnalysisRefused("direct-truss-v1 requires a nonzero force")
    return {
        "declared_material": (
            units.convert(material.youngs_modulus.value, material.youngs_modulus.unit, "Pa"),
            material.poisson_ratio.base(),
        ),
        "declared_area": section.area.base(),
        "declared_element_set": assignment.region.name.upper(),
        "declared_material_name": material.id.upper(),
        "declared_boundaries": boundaries,
        "declared_loads": loads,
    }


def _declared_direct_modal_truss(structural: Any, procedure: StructuralProcedure) -> dict[str, Any]:
    """Bind the one retained, undamped axial-truss modal envelope.

    This is deliberately only an output-recording path.  A modal frequency is
    not a simulation-validity verdict until a separately supported acceptance
    metric is declared and independently reviewed.
    """
    deck = structural.model.model_artifact
    if (
        procedure.kind != "modal"
        or procedure.damping_mode != "undamped"
        or procedure.mode_count != 1
        or deck is None
        or deck.format != "calculix_inp"
        or structural.model.mesh_artifact is not None
        or structural.connections
        or len(procedure.assignment_ids) != 1
        or len(procedure.support_ids) != 2
        or procedure.load_ids
        or procedure.mass_ids
        or procedure.contact_ids
        or procedure.damping_ids
    ):
        raise AnalysisRefused(
            "direct-modal-truss-v1 requires one undamped, one-mode, unloaded truss"
        )
    assignment = next(
        row for row in structural.assignments if row.id == procedure.assignment_ids[0]
    )
    section = next(row for row in structural.sections if row.id == assignment.section_id)
    material = next(row for row in structural.materials if row.id == section.material_id)
    if (
        assignment.region.kind != "element_set"
        or section.kind != "truss"
        or section.area is None
        or material.kind != "linear_isotropic"
        or material.density is None
    ):
        raise AnalysisRefused("direct-modal-truss-v1 requires a dense linear truss section")
    frame = structural.model.source_frame
    boundaries: set[tuple[str, int]] = set()
    for support_id in procedure.support_ids:
        support = next(row for row in structural.supports if row.id == support_id)
        if (
            support.region.kind != "node_set"
            or support.dofs.frame != frame
            or support.dofs.rotation
        ):
            raise AnalysisRefused(
                "direct-modal-truss-v1 supports require source-frame node translations"
            )
        if any(value.base() != 0 for value in support.dofs.prescribed_translation.values()):
            raise AnalysisRefused("direct-modal-truss-v1 supports require zero displacement")
        for axis in support.dofs.translation:
            boundaries.add((support.region.name.upper(), "xyz".index(axis) + 1))
    if len(boundaries) != sum(
        len(next(row for row in structural.supports if row.id == support_id).dofs.translation)
        for support_id in procedure.support_ids
    ):
        raise AnalysisRefused("direct-modal-truss-v1 repeats a support DOF")
    return {
        "declared_material": (
            units.convert(material.youngs_modulus.value, material.youngs_modulus.unit, "Pa"),
            material.poisson_ratio.base(),
        ),
        "declared_density": material.density.base(),
        "declared_area": section.area.base(),
        "declared_element_set": assignment.region.name.upper(),
        "declared_material_name": material.id.upper(),
        "declared_boundaries": boundaries,
    }


def _admit_direct_modal_truss_deck(data: bytes, **declared: Any) -> None:
    """Accept only the retained one-mode T3D2 modal deck language.

    The full CalculiX language remains outside this narrow public path.  In
    particular, no include, load, damping, mass-element, or extra step reaches
    the solver.
    """
    try:
        rows = [
            line.strip().upper()
            for line in data.decode("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("**")
        ]
        cards: list[tuple[str, list[str]]] = []
        for row in rows:
            if row.startswith("*"):
                cards.append((row, []))
            elif cards:
                cards[-1][1].append(row)
            else:
                raise ValueError("record without a card")
        expected = [
            "*HEADING",
            "*NODE",
            f"*ELEMENT, TYPE=T3D2, ELSET={declared['declared_element_set']}",
            "*NSET, NSET=ROOT",
            "*NSET, NSET=AXIAL_NODES",
            f"*MATERIAL, NAME={declared['declared_material_name']}",
            "*ELASTIC",
            "*DENSITY",
            f"*SOLID SECTION, ELSET={declared['declared_element_set']}, MATERIAL={declared['declared_material_name']}",
            "*BOUNDARY",
            "*STEP",
            "*FREQUENCY, STORAGE=YES",
            "*NODE PRINT, NSET=AXIAL_NODES, GLOBAL=YES",
            "*END STEP",
        ]
        if [card for card, _ in cards] != expected:
            raise ValueError("unsupported cards or card order")
        if cards[0][1] or len(cards[1][1]) < 2 or not cards[2][1]:
            raise ValueError("missing nodes or elements")
        number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?"
        if any(
            not re.fullmatch(rf"\d+,\s*{number},\s*{number},\s*{number}", row)
            for row in cards[1][1]
        ):
            raise ValueError("invalid node record")
        if any(not re.fullmatch(r"\d+,\s*\d+,\s*\d+", row) for row in cards[2][1]):
            raise ValueError("invalid T3D2 record")
        if len(cards[3][1]) != 1 or len(cards[4][1]) != 1:
            raise ValueError("exact root and axial node sets are required")
        boundary_records: set[tuple[str, int]] = set()
        declared_boundary_records = 0
        for row in cards[9][1]:
            fields = [field.strip() for field in row.split(",")]
            if (
                len(fields) != 3
                or not fields[1].isdigit()
                or not fields[2].isdigit()
                or not 1 <= int(fields[1]) <= int(fields[2]) <= 3
            ):
                raise ValueError("invalid boundary record")
            dofs = range(int(fields[1]), int(fields[2]) + 1)
            declared_boundary_records += len(dofs)
            boundary_records.update((fields[0], dof) for dof in dofs)
        if (
            len(boundary_records) != declared_boundary_records
            or boundary_records != declared["declared_boundaries"]
        ):
            raise ValueError("deck boundaries differ from declaration")
        elastic = [float(item.strip()) for item in cards[6][1][0].split(",")]
        if (
            len(cards[6][1]) != 1
            or len(elastic) != 2
            or any(
                not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=0)
                for actual, expected in zip(elastic, declared["declared_material"], strict=True)
            )
        ):
            raise ValueError("deck elastic material differs from declaration")
        density = [float(item.strip()) for item in cards[7][1]]
        area = [float(item.strip()) for item in cards[8][1]]
        if (
            len(density) != 1
            or len(area) != 1
            or not math.isclose(density[0], declared["declared_density"], rel_tol=1e-12, abs_tol=0)
            or not math.isclose(area[0], declared["declared_area"], rel_tol=1e-12, abs_tol=0)
        ):
            raise ValueError("deck density or area differs from declaration")
        if cards[10][1] or cards[11][1] != ["1"] or cards[12][1] != ["U"] or cards[13][1]:
            raise ValueError("invalid modal step or output request")
    except (IndexError, UnicodeError, ValueError) as error:
        raise AnalysisRefused(f"direct modal truss deck: {error}") from error


_TWO_DOF_MODAL_DECK = (
    "*HEADING",
    "*NODE",
    "1, 0., 0., 0.",
    "*ELEMENT, TYPE=SPRING1, ELSET=SPRING_X",
    "1, 1",
    "*ELEMENT, TYPE=SPRING1, ELSET=SPRING_Y",
    "2, 1",
    "*ELEMENT, TYPE=MASS, ELSET=POINT_MASS",
    "3, 1",
    "*ELSET, ELSET=ALL_ELEMENTS",
    "1, 2, 3",
    "*NSET, NSET=OSCILLATOR",
    "1",
    "*SPRING, ELSET=SPRING_X",
    "1",
    "100.",
    "*SPRING, ELSET=SPRING_Y",
    "2",
    "10000.",
    "*MASS, ELSET=POINT_MASS",
    "1.",
    "*BOUNDARY",
    "OSCILLATOR, 3, 3",
    "*STEP",
    "*FREQUENCY, STORAGE=YES",
    "1",
    "*NODE PRINT, NSET=OSCILLATOR, GLOBAL=YES",
    "U",
    "*END STEP",
)


_TWO_DOF_HARMONIC_SHA256 = "880ee2bdf67bc62aa905913f966896820d13ebd42068e2fe8c1efd9a7c5dd6c2"
_TWO_DOF_TRANSIENT_SHA256 = "fe74aed4a18dbfe4ded523dd93d2e862ca3425adbf08984775a632cd4c6d77c3"
_DYNAMIC_PRINT_HEADING = re.compile(
    r"^\s*(displacements|velocities) \(vx,vy,vz\) for set OSCILLATOR and time\s+"
    r"(?P<time>[+-]?[\d.]+E[+-]?\d+)\s*$",
    re.IGNORECASE,
)


def _declared_two_dof_response(
    structural: Any, procedure: StructuralProcedure, template: str
) -> None:
    """Bind declared dynamic operands to the retained two-DOF source deck."""
    expected_kind = {
        "direct-two-dof-harmonic-v1": "harmonic",
        "direct-two-dof-transient-v1": "transient",
    }.get(template)
    if expected_kind is None or procedure.kind != expected_kind:
        raise AnalysisRefused("two-DOF response procedure identity differs from retained source")
    if (
        len(procedure.mass_ids) != 1
        or len(procedure.load_ids) != 1
        or procedure.damping_mode != "declared"
        or len(procedure.damping_ids) != 1
        or procedure.support_ids
        or procedure.contact_ids
    ):
        raise AnalysisRefused("two-DOF response requires one declared mass, load, and damping")
    masses = {row.id: row for row in structural.masses}
    loads = {row.id: row for row in structural.loads}
    dampers = {row.id: row for row in structural.damping}
    try:
        mass = masses[procedure.mass_ids[0]]
        load = loads[procedure.load_ids[0]]
        damping = dampers[procedure.damping_ids[0]]
    except KeyError as error:
        raise AnalysisRefused("two-DOF response declaration is incomplete") from error
    if (
        mass.mass.base() != 1
        or load.kind != "force"
        or load.vector is None
        or tuple(units.convert(value, load.vector.unit, "N") for value in load.vector.values)
        != (1.0, 0.0, 0.0)
        or damping.kind != "rayleigh"
        or damping.rayleigh_alpha is None
        or damping.rayleigh_beta is None
        or damping.rayleigh_alpha.base() != 1
        or damping.rayleigh_beta.base() != 0
    ):
        raise AnalysisRefused(
            "two-DOF response mass, load, or damping differs from retained source"
        )
    if procedure.kind == "harmonic":
        if procedure.frequency is None or not math.isclose(
            procedure.frequency.base(), 0.7957747154594768, rel_tol=0, abs_tol=1e-12
        ):
            raise AnalysisRefused("two-DOF harmonic frequency differs from retained source")
    elif (
        procedure.duration is None
        or procedure.time_step is None
        or procedure.duration.base() != 0.01
        or procedure.time_step.base() != 0.001
    ):
        raise AnalysisRefused("two-DOF transient timing differs from retained source")


def _declared_two_dof_modal(structural: Any, procedure: StructuralProcedure) -> None:
    """Bind the declared oscillator mass to the retained modal deck."""
    if (
        procedure.kind != "modal"
        or len(procedure.mass_ids) != 1
        or procedure.load_ids
        or procedure.support_ids
        or procedure.contact_ids
        or procedure.damping_ids
        or procedure.damping_mode != "undamped"
        or procedure.mode_count != 1
    ):
        raise AnalysisRefused("two-DOF modal requires one declared 1 kg mass and one undamped mode")
    masses = {row.id: row for row in structural.masses}
    try:
        mass = masses[procedure.mass_ids[0]]
    except KeyError as error:
        raise AnalysisRefused("two-DOF modal mass declaration is incomplete") from error
    if mass.mass.base() != 1:
        raise AnalysisRefused("two-DOF modal mass differs from retained source")
    _two_dof_limit(structural, "two_dof_modal_frequency")


def _admit_direct_two_dof_response_deck(data: bytes, template: str) -> None:
    expected = {
        "direct-two-dof-harmonic-v1": _TWO_DOF_HARMONIC_SHA256,
        "direct-two-dof-transient-v1": _TWO_DOF_TRANSIENT_SHA256,
    }.get(template)
    if expected is None or hashlib.sha256(data).hexdigest() != expected:
        raise AnalysisRefused("two-DOF response deck differs from the retained source")


def _two_dof_print_blocks(text: str) -> list[tuple[str, float, tuple[float, float, float]]]:
    """Read only the retained CCX 2.23 oscillator U/V rows in dynamic step two."""
    blocks = []
    step = 0
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if " ".join(lines[index].split()) == "S T E P 2":
            step = 2
        match = _DYNAMIC_PRINT_HEADING.fullmatch(lines[index]) if step == 2 else None
        if match is None:
            index += 1
            continue
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines) or (row := _NODE_PRINT_ROW.fullmatch(lines[index])) is None:
            raise AnalysisRefused("two-DOF dynamic output has malformed oscillator row")
        values = tuple(float(row[name]) for name in ("x", "y", "z"))
        if int(row["node"]) != 1 or not all(math.isfinite(value) for value in values):
            raise AnalysisRefused("two-DOF dynamic output has invalid oscillator row")
        blocks.append(
            (
                "U" if match.group(1).lower() == "displacements" else "V",
                float(match["time"]),
                values,
            )
        )
        index += 1
    if not blocks:
        raise AnalysisRefused("two-DOF dynamic output has no step-two oscillator rows")
    return blocks


def _parse_two_dof_harmonic(text: str, expected_frequency_hz: float) -> tuple[float, float]:
    rows = _two_dof_print_blocks(text)
    displacements = [row for row in rows if row[0] == "U"]
    velocities = [row for row in rows if row[0] == "V"]
    if len(displacements) != 2 or len(velocities) != 2 or len(rows) != 4:
        raise AnalysisRefused("two-DOF harmonic output differs from retained U/V rows")
    frequency = displacements[0][1]
    if any(
        not math.isclose(row[1], frequency, rel_tol=0, abs_tol=1e-9)
        for row in (*displacements, *velocities)
    ) or not math.isclose(frequency, expected_frequency_hz, rel_tol=0, abs_tol=1e-7):
        raise AnalysisRefused(
            "two-DOF harmonic output has inconsistent or undeclared response frequency"
        )
    amplitude = math.hypot(displacements[0][2][0], displacements[1][2][0])
    if not math.isfinite(amplitude) or amplitude <= 0:
        raise AnalysisRefused("two-DOF harmonic output has zero response amplitude")
    return frequency, amplitude


def _parse_two_dof_transient(text: str) -> dict[float, tuple[float, float]]:
    rows = _two_dof_print_blocks(text)
    samples: dict[float, dict[str, float]] = {}
    for kind, time_value, values in rows:
        sample = samples.setdefault(time_value, {})
        if kind in sample:
            raise AnalysisRefused("two-DOF transient output repeats an oscillator component")
        sample[kind] = values[0]
    expected_times = tuple(index * 0.001 for index in range(1, 11))
    parsed: dict[float, tuple[float, float]] = {}
    for expected_time in expected_times:
        matching = [
            (time_value, sample)
            for time_value, sample in samples.items()
            if math.isclose(time_value, expected_time, rel_tol=0, abs_tol=1e-9)
        ]
        if len(matching) != 1 or set(matching[0][1]) != {"U", "V"}:
            raise AnalysisRefused(
                "two-DOF transient output differs from retained response time grid"
            )
        parsed[expected_time] = (matching[0][1]["U"], matching[0][1]["V"])
    if len(samples) != len(expected_times) or not any(
        abs(displacement) > 0 or abs(velocity) > 0 for displacement, velocity in parsed.values()
    ):
        raise AnalysisRefused("two-DOF transient output has missing, off-grid, or zero response")
    return parsed


def _two_dof_harmonic_reference(frequency_hz: float) -> float:
    """Independent SDOF reference: m=1 kg, k=100 N/m, c=1 N*s/m, F=1 N."""
    omega = 2.0 * math.pi * frequency_hz
    return 1.0 / math.hypot(100.0 - omega * omega, omega)


def _two_dof_transient_reference(time_s: float) -> tuple[float, float]:
    """Independent underdamped unit-step response for the bound x oscillator."""
    decay = 0.5
    damped_frequency = math.sqrt(100.0 - decay * decay)
    attenuation = math.exp(-decay * time_s)
    displacement = 0.01 * (
        1.0
        - attenuation
        * (
            math.cos(damped_frequency * time_s)
            + decay / damped_frequency * math.sin(damped_frequency * time_s)
        )
    )
    velocity = attenuation / damped_frequency * math.sin(damped_frequency * time_s)
    return displacement, velocity


# M13: a relative acceptance threshold above 5 percent is not an agreement check; the
# loosest sibling direct-deck limit (sliding contact, impact) is the same bound.
_MAX_RELATIVE_THRESHOLD = 0.05
# M14: these verdicts score a pinned reference deck, not the user's model; say so beside the GO.
_FIXTURE_LABEL = " (reference fixture, not your model)"


def _two_dof_limit(structural: Any, metric: str) -> ScalarAgreementLimit:
    limits = [
        row
        for row in structural.acceptance_limits
        if isinstance(row, ScalarAgreementLimit) and row.metric == metric
    ]
    if (
        len(limits) != 1
        or limits[0].comparator != "relative"
        or not 0 < limits[0].threshold.base() <= _MAX_RELATIVE_THRESHOLD
    ):
        raise AnalysisRefused(
            f"two-DOF response requires one positive relative {metric!r} limit up to 5 percent"
        )
    return limits[0]


def _score_two_dof_response(
    structural: Any, template: str, output_text: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if template == "direct-two-dof-harmonic-v1":
        frequency, amplitude = _parse_two_dof_harmonic(output_text, 0.7957747154594768)
        limit = _two_dof_limit(structural, "two_dof_harmonic_amplitude")
        expected = _two_dof_harmonic_reference(0.7957747154594768)
        error = abs(amplitude - expected) / expected
        comparisons = [
            {
                "metric": limit.metric,
                "observed": {"value": amplitude, "unit": "m", "frequency_hz": frequency},
                "expected": {"value": expected, "unit": "m", "frequency_hz": 0.7957747154594768},
                "relative_error": error,
                "passed": math.isfinite(error) and error <= limit.threshold.base(),
            }
        ]
        results = [
            {
                "kind": "harmonic_displacement_amplitude",
                "value": amplitude,
                "unit": "m",
                "frequency_hz": frequency,
            }
        ]
    else:
        samples = _parse_two_dof_transient(output_text)
        limit = _two_dof_limit(structural, "two_dof_transient_response")
        expected_samples = {
            time_value: _two_dof_transient_reference(time_value) for time_value in (0.001, 0.002)
        }
        comparisons = []
        results = []
        for time_value, expected in expected_samples.items():
            matching = [
                value
                for moment, value in samples.items()
                if math.isclose(moment, time_value, rel_tol=0, abs_tol=1e-9)
            ]
            if len(matching) != 1:
                raise AnalysisRefused("two-DOF transient output lacks required sample time")
            observed = matching[0]
            for label, actual, reference, unit in (
                ("displacement", observed[0], expected[0], "m"),
                ("velocity", observed[1], expected[1], "m/s"),
            ):
                error = abs(actual - reference) / max(abs(reference), 1e-30)
                comparisons.append(
                    {
                        "metric": f"{limit.metric}_{label}_{time_value:g}s",
                        "observed": {"value": actual, "unit": unit, "time_s": time_value},
                        "expected": {"value": reference, "unit": unit, "time_s": time_value},
                        "relative_error": error,
                        "passed": math.isfinite(error) and error <= limit.threshold.base(),
                    }
                )
            results.append(
                {
                    "kind": "transient_response",
                    "time_s": time_value,
                    "displacement_m": observed[0],
                    "velocity_m_per_s": observed[1],
                }
            )
    passed = all(row["passed"] for row in comparisons)
    return results, {
        "simulation_validity": "GO" if passed else "NO_GO",
        "acceptance_limit_id": limit.id,
        "acceptance_provenance": limit.provenance.model_dump(mode="json"),
        "threshold": limit.threshold.model_dump(mode="json"),
        "analytic_operands": (
            {
                "mass": {"value": 1.0, "unit": "kg"},
                "spring_x": {"value": 100.0, "unit": "N/m"},
                "rayleigh_alpha": {"value": 1.0, "unit": "1/s"},
                "excitation_frequency": {"value": 0.7957747154594768, "unit": "Hz"},
            }
            if template == "direct-two-dof-harmonic-v1"
            else {
                "mass": {"value": 1.0, "unit": "kg"},
                "spring_x": {"value": 100.0, "unit": "N/m"},
                "rayleigh_alpha": {"value": 1.0, "unit": "1/s"},
                "sample_times": [{"value": 0.001, "unit": "s"}, {"value": 0.002, "unit": "s"}],
            }
        ),
        "comparisons": comparisons,
        "reason": "bound two-DOF dynamic response "
        + ("agrees" if passed else "disagrees")
        + "; physical conformance remains unscored"
        + _FIXTURE_LABEL,
    }


def _admit_direct_two_dof_modal_deck(data: bytes) -> None:
    """Admit the one retained two-positive-DOF modal source deck.

    Its fixed source binds the 100 N/m x spring; the paired declaration binds
    the 1 kg mass used by the modal acceptance comparison.
    """
    try:
        rows = tuple(
            line.strip().upper()
            for line in data.decode("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("**")
        )
    except UnicodeError as error:
        raise AnalysisRefused("two-DOF modal deck is not UTF-8") from error
    if rows != _TWO_DOF_MODAL_DECK:
        raise AnalysisRefused("two-DOF modal deck differs from the retained source")


def _score_two_dof_modal(structural: Any, frequencies: list[tuple[int, float]]) -> dict[str, Any]:
    if len(frequencies) != 1 or frequencies[0][0] != 1:
        raise AnalysisRefused("two-DOF modal output must contain exactly the requested first mode")
    observed = frequencies[0][1]
    limit = _two_dof_limit(structural, "two_dof_modal_frequency")
    expected = math.sqrt(100.0 / 1.0) / (2.0 * math.pi)
    error = abs(observed - expected) / expected
    passed = math.isfinite(error) and error <= limit.threshold.base()
    return {
        "simulation_validity": "GO" if passed else "NO_GO",
        "acceptance_limit_id": limit.id,
        "acceptance_provenance": limit.provenance.model_dump(mode="json"),
        "threshold": limit.threshold.model_dump(mode="json"),
        "analytic_operands": {
            "mass": {"value": 1.0, "unit": "kg"},
            "spring_x": {"value": 100.0, "unit": "N/m"},
            "formula": "sqrt(k/m)/(2*pi)",
        },
        "comparisons": [
            {
                "metric": limit.metric,
                "observed": {"value": observed, "unit": "Hz"},
                "expected": {"value": expected, "unit": "Hz"},
                "relative_error": error,
                "passed": passed,
            }
        ],
        "reason": "bound two-DOF modal frequency "
        + ("agrees" if passed else "disagrees")
        + "; physical conformance remains unscored"
        + _FIXTURE_LABEL,
    }


def _declared_direct_b31_square(structural: Any, procedure: StructuralProcedure) -> dict[str, Any]:
    """Bind the canonical straight square B31 cantilever to its declared operands."""
    deck = structural.model.model_artifact
    if (
        procedure.kind != "linear_static"
        or deck is None
        or deck.format != "calculix_inp"
        or structural.model.mesh_artifact is not None
        or structural.connections
        or len(procedure.assignment_ids) != 1
        or len(procedure.support_ids) != 1
        or len(procedure.load_ids) != 1
        or procedure.mass_ids
        or procedure.contact_ids
        or procedure.damping_ids
        or procedure.geometric_nonlinearity is not None
        or procedure.plasticity is not None
    ):
        raise AnalysisRefused("direct-b31-square-v1 requires one static cantilever")
    assignment = next(
        row for row in structural.assignments if row.id == procedure.assignment_ids[0]
    )
    section = next(row for row in structural.sections if row.id == assignment.section_id)
    material = next(row for row in structural.materials if row.id == section.material_id)
    orientation = next(
        (row for row in structural.orientations if row.id == section.orientation_id), None
    )
    if (
        assignment.region.kind != "element_set"
        or section.kind not in {"beam", "frame"}
        or material.kind != "linear_isotropic"
        or section.area is None
        or section.principal_second_moment_y is None
        or section.principal_second_moment_z is None
        or section.torsion_constant_j is None
        or orientation is None
        or orientation.frame != structural.model.source_frame
        or orientation.rotation_about_local_axis is not None
        or section.shear_area_y is not None
        or section.shear_area_z is not None
    ):
        raise AnalysisRefused("direct-b31-square-v1 requires a source-frame square beam section")
    side = math.sqrt(section.area.base())
    inertia = side**4 / 12
    torsion = 0.1406 * side**4  # Saint-Venant square-section approximation.
    if (
        any(
            not math.isclose(actual, expected, rel_tol=1e-4)
            for actual, expected in (
                (section.principal_second_moment_y.base(), inertia),
                (section.principal_second_moment_z.base(), inertia),
                (section.torsion_constant_j.base(), torsion),
            )
        )
        or orientation.longitudinal != (1.0, 0.0, 0.0)
        or orientation.in_plane_reference != (0.0, 1.0, 0.0)
    ):
        raise AnalysisRefused("direct-b31-square-v1 section moments or axes differ from square B31")
    frame = structural.model.source_frame
    support = next(row for row in structural.supports if row.id == procedure.support_ids[0])
    if (
        support.region.kind != "node_set"
        or support.dofs.frame != frame
        or set(support.dofs.translation) != set("xyz")
        or set(support.dofs.rotation) != set("xyz")
        or any(value.base() != 0 for value in support.dofs.prescribed_translation.values())
        or any(value.base() != 0 for value in support.dofs.prescribed_rotation.values())
    ):
        raise AnalysisRefused("direct-b31-square-v1 requires one zero 6-DOF root support")
    load = next(row for row in structural.loads if row.id == procedure.load_ids[0])
    if (
        load.kind != "force"
        or load.region.kind != "node_set"
        or load.application_frame != frame
        or load.amplitude is not None
        or load.vector is None
    ):
        raise AnalysisRefused("direct-b31-square-v1 requires one constant source-frame tip force")
    force = tuple(units.convert(value, load.vector.unit, "N") for value in load.vector.values)
    if force[0] != 0 or force[1] >= 0 or force[2] != 0:
        raise AnalysisRefused("direct-b31-square-v1 requires a downward y tip force")
    return {
        "declared_material": (
            units.convert(material.youngs_modulus.value, material.youngs_modulus.unit, "Pa"),
            material.poisson_ratio.base(),
        ),
        "declared_element_set": assignment.region.name.upper(),
        "declared_material_name": material.id.upper(),
        "declared_boundaries": {(support.region.name.upper(), dof) for dof in range(1, 7)},
        "declared_loads": {(load.region.name.upper(), 2): force[1]},
        "b31_square": True,
        "declared_side": side,
        "declared_orientation": orientation.in_plane_reference,
    }


def _declared_direct_plate_s4(structural: Any, procedure: StructuralProcedure) -> dict[str, Any]:
    """Bind one declared simply-supported S4 square plate to the exact deck inputs."""
    deck = structural.model.model_artifact
    if (
        procedure.kind != "linear_static"
        or deck is None
        or deck.format != "calculix_inp"
        or structural.model.mesh_artifact is not None
        or structural.connections
        or len(procedure.assignment_ids) != 1
        or len(procedure.support_ids) != 3
        or len(procedure.load_ids) != 1
        or len(structural.supports) != 3
        or len(structural.loads) != 1
        or procedure.mass_ids
        or procedure.contact_ids
        or procedure.damping_ids
        or procedure.geometric_nonlinearity is not None
        or procedure.plasticity is not None
    ):
        raise AnalysisRefused("direct-plate-s4-v1 requires one declared linear-static plate")
    assignment = next(
        row for row in structural.assignments if row.id == procedure.assignment_ids[0]
    )
    section = next(row for row in structural.sections if row.id == assignment.section_id)
    material = next(row for row in structural.materials if row.id == section.material_id)
    if (
        assignment.region.kind != "element_set"
        or section.kind not in {"plate", "shell"}
        or section.thickness is None
        or section.orientation_id is not None
        or structural.orientations
        or material.kind != "linear_isotropic"
        or material.density is not None
        or len(structural.assignments) != 1
        or len(structural.sections) != 1
        or len(structural.materials) != 1
    ):
        raise AnalysisRefused("direct-plate-s4-v1 requires one linear isotropic shell section")
    frame = structural.model.source_frame
    boundaries: set[tuple[str, int]] = set()
    support_names: set[str] = set()
    for support_id in procedure.support_ids:
        support = next(row for row in structural.supports if row.id == support_id)
        if (
            support.region.kind != "node_set"
            or support.dofs.frame != frame
            or support.dofs.rotation
            or any(value.base() != 0 for value in support.dofs.prescribed_translation.values())
        ):
            raise AnalysisRefused(
                "direct-plate-s4-v1 supports require zero source-frame translations"
            )
        name = support.region.name.upper()
        support_names.add(name)
        for axis in support.dofs.translation:
            key = (name, "xyz".index(axis) + 1)
            if key in boundaries:
                raise AnalysisRefused("direct-plate-s4-v1 repeats a support DOF")
            boundaries.add(key)
    if len(support_names) != 3:
        raise AnalysisRefused("direct-plate-s4-v1 requires three distinct declared support sets")
    loads: dict[tuple[str, str], float] = {}
    for load_id in procedure.load_ids:
        load = next(row for row in structural.loads if row.id == load_id)
        if (
            load.kind != "pressure"
            or load.region.kind != "element_set"
            or load.application_frame != frame
            or load.amplitude is not None
            or load.scalar is None
        ):
            raise AnalysisRefused(
                "direct-plate-s4-v1 requires one constant source-frame element pressure"
            )
        value = units.convert(load.scalar.value, load.scalar.unit, "Pa")
        key = (load.region.name.upper(), "P")
        if value >= 0 or key in loads:
            raise AnalysisRefused("direct-plate-s4-v1 requires one downward pressure")
        loads[key] = value
    if len(structural.acceptance_limits) != 1 or not isinstance(
        structural.acceptance_limits[0], ScalarAgreementLimit
    ):
        raise AnalysisRefused("direct-plate-s4-v1 requires one analytic scalar limit")
    limit = structural.acceptance_limits[0]
    if (
        limit.metric != "plate_center_u3"
        or limit.reference != "analytic"
        or limit.comparator != "relative"
        or limit.threshold.dimension() != "dimensionless"
        or not math.isfinite(limit.threshold.base())
        or not 0 < limit.threshold.base() <= 0.02
    ):
        raise AnalysisRefused(
            "direct-plate-s4-v1 requires a relative plate-center limit up to 2 percent"
        )
    return {
        "declared_material": (
            units.convert(material.youngs_modulus.value, material.youngs_modulus.unit, "Pa"),
            material.poisson_ratio.base(),
        ),
        "declared_thickness": section.thickness.base(),
        "declared_element_set": assignment.region.name.upper(),
        "declared_material_name": material.id.upper(),
        "declared_boundaries": boundaries,
        "declared_loads": loads,
        "support_names": support_names,
        "acceptance_limit": limit,
    }


def _admit_direct_plate_s4_deck(data: bytes, **declared: Any) -> _DirectDeckAdmission:
    """Admit only the 8x8 and 16x16 unit-square S4 pressure-plate meshes."""
    if len(data) > 1024 * 1024:
        raise AnalysisRefused("direct plate deck exceeds 1 MiB")
    options = {
        "*HEADING": set(),
        "*NODE": set(),
        "*ELEMENT": {"TYPE", "ELSET"},
        "*NSET": {"NSET"},
        "*MATERIAL": {"NAME"},
        "*ELASTIC": set(),
        "*SHELL SECTION": {"ELSET", "MATERIAL"},
        "*BOUNDARY": set(),
        "*STEP": set(),
        "*STATIC": set(),
        "*DLOAD": set(),
        "*NODE PRINT": {"NSET", "GLOBAL"},
        "*END STEP": set(),
    }
    blocks: list[tuple[str, dict[str, str], list[list[str]]]] = []
    try:
        for raw in data.decode("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("**"):
                continue
            if line.startswith("*"):
                fields = [value.strip().upper() for value in line.split(",")]
                card = fields[0]
                if card not in options:
                    raise ValueError(f"card {card!r} is not admitted")
                selected: dict[str, str] = {}
                for field in fields[1:]:
                    key, separator, value = field.partition("=")
                    if not separator or key not in options[card] or key in selected or not value:
                        raise ValueError("unknown, repeated or malformed card option")
                    selected[key] = value
                if not options[card] - {"GLOBAL"} <= set(selected):
                    raise ValueError("missing card option")
                if card == "*ELEMENT" and selected["TYPE"] != "S4":
                    raise ValueError("only S4 plate elements are admitted")
                if card == "*NODE PRINT" and selected.get("GLOBAL", "YES") != "YES":
                    raise ValueError("only global displacement output is admitted")
                if any(not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", v) for v in selected.values()):
                    raise ValueError("unsafe or unsupported identity")
                blocks.append((card, selected, []))
            else:
                if not blocks:
                    raise ValueError("record without a card")
                blocks[-1][2].append([v.strip().upper() for v in line.split(",")])

        def integer(value: str) -> int:
            if not re.fullmatch(r"[1-9][0-9]{0,8}", value):
                raise ValueError("positive integer record required")
            return int(value)

        def number(value: str) -> float:
            if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?", value):
                raise ValueError("finite decimal record required")
            result = float(value)
            if not math.isfinite(result):
                raise ValueError("nonfinite record")
            return result

        nodes: dict[int, tuple[float, float, float]] = {}
        elements: dict[int, tuple[int, int, int, int]] = {}
        nsets: dict[str, set[int]] = {}
        elsets: dict[str, set[int]] = {}
        boundary_records: set[tuple[str, int]] = set()
        request: set[tuple[str, str, int]] = set()
        material_names: list[str] = []
        shell_sections: list[tuple[str, str, float]] = []
        dloads: list[tuple[str, str, float]] = []
        phase = "model"
        seen: set[str] = set()
        heading_text = None
        for index, (card, selected, rows) in enumerate(blocks):
            if card in {"*HEADING", "*NODE", "*ELEMENT", "*STEP", "*STATIC", "*END STEP"}:
                if card in seen:
                    raise ValueError("repeated singleton card")
                seen.add(card)
            if card == "*HEADING":
                if index != 0 or len(rows) != 1 or len(rows[0]) != 1:
                    raise ValueError("heading must be first with one title")
                heading_text = rows[0][0]
            elif card == "*STEP":
                if phase != "model" or rows:
                    raise ValueError("invalid step")
                phase = "step"
            elif card == "*STATIC":
                if phase != "step" or rows:
                    raise ValueError("only default linear static is admitted")
                phase = "static"
            elif card == "*END STEP":
                if phase != "static" or rows or index != len(blocks) - 1:
                    raise ValueError("invalid end step")
                phase = "ended"
            elif card in {"*DLOAD", "*NODE PRINT"}:
                if phase != "static" or not rows:
                    raise ValueError("load/output required inside static step")
                if card == "*DLOAD":
                    for row in rows:
                        if len(row) != 3:
                            raise ValueError("unsupported distributed-load row")
                        dloads.append((row[0], row[1], number(row[2])))
                else:
                    if len(rows) != 1 or rows[0] != ["U"]:
                        raise ValueError("only center displacement output is admitted")
                    request.update(
                        ("U", selected["NSET"], node) for node in nsets.get(selected["NSET"], set())
                    )
            elif card == "*NODE":
                for row in rows:
                    if len(row) != 4:
                        raise ValueError("node row must have four values")
                    node = integer(row[0])
                    if node in nodes:
                        raise ValueError("duplicate node")
                    nodes[node] = tuple(number(v) for v in row[1:])
            elif card == "*ELEMENT":
                name = selected["ELSET"]
                if name in elsets:
                    raise ValueError("duplicate element set")
                members = set()
                for row in rows:
                    if len(row) != 5:
                        raise ValueError("S4 element needs four nodes")
                    eid, *connectivity = map(integer, row)
                    if (
                        eid in elements
                        or len(set(connectivity)) != 4
                        or not set(connectivity) <= nodes.keys()
                    ):
                        raise ValueError("duplicate or invalid S4 connectivity")
                    elements[eid] = tuple(connectivity)
                    members.add(eid)
                elsets[name] = members
            elif card == "*NSET":
                name = selected["NSET"]
                members = [integer(v) for row in rows for v in row]
                if (
                    name in nsets
                    or not members
                    or len(set(members)) != len(members)
                    or not set(members) <= nodes.keys()
                ):
                    raise ValueError("duplicate or invalid node set")
                nsets[name] = set(members)
            elif card == "*MATERIAL":
                if rows:
                    raise ValueError("material card must be empty")
                material_names.append(selected["NAME"])
                if index + 1 >= len(blocks) or blocks[index + 1][0] != "*ELASTIC":
                    raise ValueError("material requires linear elasticity")
            elif card == "*ELASTIC":
                if (
                    index == 0
                    or blocks[index - 1][0] != "*MATERIAL"
                    or len(rows) != 1
                    or len(rows[0]) != 2
                ):
                    raise ValueError("elastic record must immediately follow material")
                actual = tuple(number(v) for v in rows[0])
                if (
                    actual[0] <= 0
                    or not -1 < actual[1] < 0.5
                    or any(
                        not math.isclose(a, b, rel_tol=1e-12, abs_tol=0)
                        for a, b in zip(actual, declared["declared_material"], strict=True)
                    )
                ):
                    raise ValueError("deck elastic material differs from declaration")
            elif card == "*SHELL SECTION":
                if len(rows) != 1 or len(rows[0]) != 1:
                    raise ValueError("shell section requires one thickness record")
                shell_sections.append((selected["ELSET"], selected["MATERIAL"], number(rows[0][0])))
            elif card == "*BOUNDARY":
                for row in rows:
                    if len(row) not in {3, 4}:
                        raise ValueError("invalid boundary row")
                    name = row[0]
                    start, end = integer(row[1]), integer(row[2])
                    if start > end or end > 6 or len(row) == 4 and number(row[3]) != 0:
                        raise ValueError("only zero shell supports are admitted")
                    for dof in range(start, end + 1):
                        key = (name, dof)
                        if key in boundary_records:
                            raise ValueError("duplicate support DOF")
                        boundary_records.add(key)
            elif card not in {"*ELASTIC"}:
                raise ValueError("unsupported plate card")

        if phase != "ended" or not nodes or not elements or len(material_names) != 1:
            raise ValueError("incomplete single static plate model")
        if len(shell_sections) != 1:
            raise ValueError("one shell section is required")
        section_set, section_material, thickness = shell_sections[0]
        if (
            section_set != declared["declared_element_set"]
            or section_material != declared["declared_material_name"]
            or not math.isclose(thickness, declared["declared_thickness"], rel_tol=1e-12, abs_tol=0)
        ):
            raise ValueError("deck shell thickness or binding differs from declaration")
        if material_names[0] != declared["declared_material_name"]:
            raise ValueError("deck material name differs from declaration")
        if set(next(iter(elsets.values()))) != set(elements) or len(elsets) != 1:
            raise ValueError("one element set must cover the entire plate")
        if set(nsets) != {"SUPPORT", "PIN_X", "PIN_Y", "CENTER"}:
            raise ValueError("only the declared support, pin and center node sets are admitted")
        if boundary_records != declared["declared_boundaries"]:
            raise ValueError("deck edge supports differ from declaration")
        if len(dloads) != 1 or dloads[0][:2] != next(iter(declared["declared_loads"])):
            raise ValueError("deck pressure region or kind differs from declaration")
        if not math.isclose(
            dloads[0][2], next(iter(declared["declared_loads"].values())), rel_tol=1e-12, abs_tol=0
        ):
            raise ValueError("deck pressure differs from declaration")

        xs, ys = (
            sorted({xyz[0] for xyz in nodes.values()}),
            sorted({xyz[1] for xyz in nodes.values()}),
        )
        if (
            len(xs) != len(ys)
            or len(xs) not in {9, 17}
            or xs[0] != 0
            or ys[0] != 0
            or xs[-1] != 1
            or ys[-1] != 1
        ):
            raise ValueError("only refined unit-square plate meshes are admitted")
        n = len(xs) - 1
        if heading_text != f"SIMPLY SUPPORTED S4 UNIT SQUARE PLATE {n}X{n}":
            raise ValueError("plate heading differs from the admitted mesh")
        if (
            any(not math.isclose(x, i / n, rel_tol=0, abs_tol=1e-12) for i, x in enumerate(xs))
            or any(not math.isclose(y, i / n, rel_tol=0, abs_tol=1e-12) for i, y in enumerate(ys))
            or any(z != 0 for _, _, z in nodes.values())
        ):
            raise ValueError("S4 mesh must be a regular planar unit square")
        coordinate_nodes = {xyz: node for node, xyz in nodes.items()}
        if len(coordinate_nodes) != (n + 1) ** 2:
            raise ValueError("plate mesh has missing or duplicate grid coordinates")
        expected_elements = set()
        for j in range(n):
            for i in range(n):
                corners = (
                    coordinate_nodes[(xs[i], ys[j], 0.0)],
                    coordinate_nodes[(xs[i + 1], ys[j], 0.0)],
                    coordinate_nodes[(xs[i + 1], ys[j + 1], 0.0)],
                    coordinate_nodes[(xs[i], ys[j + 1], 0.0)],
                )
                expected_elements.add(corners)
        if set(elements.values()) != expected_elements:
            raise ValueError("S4 elements do not form the declared positive-normal grid")
        center = coordinate_nodes[(0.5, 0.5, 0.0)]
        if nsets.get("CENTER") != {center} or request != {("U", "CENTER", center)}:
            raise ValueError("one center-node displacement output is required")
        perimeter = {node for node, (x, y, _) in nodes.items() if x in {0, 1} or y in {0, 1}}
        pin_x = coordinate_nodes[(0.0, 0.0, 0.0)]
        pin_y = coordinate_nodes[(1 / n, 0.0, 0.0)]
        if (
            nsets.get("SUPPORT") != perimeter
            or nsets.get("PIN_X") != {pin_x}
            or nsets.get("PIN_Y") != {pin_x, pin_y}
            or declared["support_names"] != {"SUPPORT", "PIN_X", "PIN_Y"}
            or not {("SUPPORT", 3), ("PIN_X", 1), ("PIN_Y", 2)} <= boundary_records
            or boundary_records != {("SUPPORT", 3), ("PIN_X", 1), ("PIN_Y", 2)}
        ):
            raise ValueError(
                "plate must have simply-supported edges and three in-plane pin constraints"
            )
        return _DirectDeckAdmission(request, 1.0)
    except (UnicodeError, ValueError, KeyError) as error:
        raise AnalysisRefused(f"direct S4 plate deck: {error}") from error


_DIRECT_THICK_SHEAR_DECKS = {
    # Closed E5.6 fixtures. Their nodal CLOAD vectors are part of these hashes;
    # this deliberately does not claim general C3D20 or traction support.
    "df597563b36bdc4da7fd91e8033ecf59dd3a3bf34d8db429614853f96281ea30": (27, 8, 8, 8),
    "da12bac1364a6299731ee6c72a045b2169ffb2bda76d444068f9a0fd28516370": (125, 21, 21, 21),
}


def _declared_direct_thick_shear(structural: Any, procedure: StructuralProcedure) -> dict[str, Any]:
    """Bind only the retained homogeneous C3D20 shear reference cases."""
    deck = structural.model.model_artifact
    if (
        procedure.kind != "linear_static"
        or deck is None
        or deck.format != "calculix_inp"
        or structural.model.mesh_artifact is not None
        or structural.connections
        or len(procedure.assignment_ids) != 1
        or len(procedure.support_ids) != 1
        or procedure.load_ids
        or procedure.mass_ids
        or procedure.contact_ids
        or procedure.damping_ids
        or procedure.geometric_nonlinearity is not None
        or procedure.plasticity is not None
        or len(structural.assignments) != 1
        or len(structural.sections) != 1
        or len(structural.materials) != 1
        or len(structural.supports) != 1
        or structural.loads
        or structural.orientations
    ):
        raise AnalysisRefused("direct-shear-c3d20-v1 requires one closed homogeneous shear model")
    assignment = next(
        row for row in structural.assignments if row.id == procedure.assignment_ids[0]
    )
    section = next(row for row in structural.sections if row.id == assignment.section_id)
    material = next(row for row in structural.materials if row.id == section.material_id)
    support = next(row for row in structural.supports if row.id == procedure.support_ids[0])
    if (
        assignment.region.kind != "mesh_region"
        or assignment.region.name.upper() != "BLOCK"
        or section.kind != "solid"
        or section.orientation_id is not None
        or material.kind != "linear_isotropic"
        or material.density is not None
        or support.region.kind != "node_set"
        or support.region.name.upper() != "SUPPORT"
        or support.dofs.frame != structural.model.source_frame
        or set(support.dofs.translation) != {"x", "y", "z"}
        or support.dofs.rotation
        or any(value.base() != 0 for value in support.dofs.prescribed_translation.values())
    ):
        raise AnalysisRefused("direct-shear-c3d20-v1 requires the declared solid and fixed support")
    if len(structural.acceptance_limits) != 1 or not isinstance(
        structural.acceptance_limits[0], ScalarAgreementLimit
    ):
        raise AnalysisRefused("direct-shear-c3d20-v1 requires one analytic displacement limit")
    limit = structural.acceptance_limits[0]
    if (
        limit.metric != "thick_shear_top_u1"
        or limit.reference != "analytic"
        or limit.comparator != "relative"
        or limit.threshold.dimension() != "dimensionless"
        or not 0 < limit.threshold.base() <= 0.02
    ):
        raise AnalysisRefused(
            "direct-shear-c3d20-v1 requires a relative top-shear limit up to 2 percent"
        )
    return {
        "declared_material": (
            units.convert(material.youngs_modulus.value, material.youngs_modulus.unit, "Pa"),
            material.poisson_ratio.base(),
        ),
        "declared_material_name": material.id.upper(),
        "declared_element_set": assignment.region.name.upper(),
        "acceptance_limit": limit,
    }


def _admit_direct_thick_shear_deck(data: bytes, **declared: Any) -> _DirectDeckAdmission:
    """Admit the byte-pinned 1- and 2-element homogeneous shear fixtures only."""
    digest = hashlib.sha256(data).hexdigest()
    fixture = _DIRECT_THICK_SHEAR_DECKS.get(digest)
    if fixture is None:
        raise AnalysisRefused("direct shear deck is not one of the byte-pinned C3D20 references")
    _node_count, support_count, top_count, _mesh = fixture
    if (
        declared["declared_material"] != (210e9, 0.3)
        or declared["declared_material_name"] != "STEEL"
    ):
        raise AnalysisRefused("direct shear material differs from the retained reference")
    # The complete hash binds mesh topology, SUPPORT/TOP membership, and every
    # CLOAD node/DOF/vector. Only the retained TOP U and SUPPORT RF rows are scored.
    support_nodes = (
        {1, 2, 3, 10, 12, 19, 20, 21}
        if support_count == 8
        else {1, 2, 3, 4, 5, 26, 28, 30, 51, 52, 53, 54, 55, 76, 78, 80, 101, 102, 103, 104, 105}
    )
    top_nodes = (
        {7, 8, 9, 16, 18, 25, 26, 27}
        if top_count == 8
        else {
            21,
            22,
            23,
            24,
            25,
            46,
            48,
            50,
            71,
            72,
            73,
            74,
            75,
            96,
            98,
            100,
            121,
            122,
            123,
            124,
            125,
        }
    )
    requests = {("U", "TOP", node) for node in top_nodes}
    requests.update(("RF", "SUPPORT", node) for node in support_nodes)
    if len(support_nodes) != support_count or len(top_nodes) != top_count:
        raise AnalysisRefused("pinned shear support fixture metadata is inconsistent")
    return _DirectDeckAdmission(requests, 1.0)


_TORSION_C3D20_SHA256 = {
    "327a0786c857f7eb94b75ad1b911c0f75f1622217366adba1b8ec141bdaeaa21",
    "88ad310791527d2cc13f8b9052474a4e278866edd0f9ed2ec548a2f66fba74e7",
}


def _declared_direct_torsion_c3d20(
    structural: Any, procedure: StructuralProcedure
) -> dict[str, Any]:
    """Map the one retained solid-annulus torsion declaration to its deck operands."""
    deck = structural.model.model_artifact
    if (
        procedure.kind != "linear_static"
        or deck is None
        or deck.format != "calculix_inp"
        or structural.model.mesh_artifact is not None
        or structural.connections
        or len(procedure.assignment_ids) != 1
        or len(procedure.support_ids) != 2
        or procedure.load_ids
        or procedure.mass_ids
        or procedure.contact_ids
        or procedure.damping_ids
        or procedure.geometric_nonlinearity is not None
        or procedure.plasticity is not None
        or len(structural.assignments) != 1
        or len(structural.sections) != 1
        or len(structural.materials) != 1
        or len(structural.supports) != 2
    ):
        raise AnalysisRefused("direct-torsion-c3d20-v1 requires one declared static solid annulus")
    assignment = next(
        row for row in structural.assignments if row.id == procedure.assignment_ids[0]
    )
    section = next(row for row in structural.sections if row.id == assignment.section_id)
    material = next(row for row in structural.materials if row.id == section.material_id)
    if (
        assignment.region.kind != "mesh_region"
        or assignment.region.name.upper() != "SHAFT"
        or section.kind != "solid"
        or material.kind != "linear_isotropic"
        or material.density is not None
    ):
        raise AnalysisRefused("direct-torsion-c3d20-v1 requires one linear isotropic SHAFT solid")
    frame = structural.model.source_frame
    supports = {
        row.region.name.upper(): row
        for row in structural.supports
        if row.id in procedure.support_ids
    }
    if set(supports) != {"SUPPORT", "TOP"}:
        raise AnalysisRefused("direct-torsion-c3d20-v1 requires SUPPORT and TOP node sets")
    fixed, top = supports["SUPPORT"], supports["TOP"]
    if (
        fixed.region.kind != "node_set"
        or fixed.dofs.frame != frame
        or set(fixed.dofs.translation) != {"x", "y", "z"}
        or fixed.dofs.rotation
        or fixed.dofs.prescribed_translation
        or fixed.dofs.prescribed_rotation
    ):
        raise AnalysisRefused("direct-torsion-c3d20-v1 requires a zero-translation SUPPORT face")
    if (
        top.region.kind != "node_set"
        or top.dofs.frame != frame
        or top.dofs.translation
        or top.dofs.rotation != ["x"]
        or top.dofs.prescribed_translation
        or set(top.dofs.prescribed_rotation) != {"x"}
    ):
        raise AnalysisRefused("direct-torsion-c3d20-v1 requires one prescribed TOP x rotation")
    twist = top.dofs.prescribed_rotation["x"].base()
    if not math.isfinite(twist) or twist <= 0:
        raise AnalysisRefused("direct-torsion-c3d20-v1 requires a positive finite TOP twist")
    if len(structural.acceptance_limits) != 1 or not isinstance(
        structural.acceptance_limits[0], ScalarAgreementLimit
    ):
        raise AnalysisRefused("direct-torsion-c3d20-v1 requires one analytic scalar limit")
    limit = structural.acceptance_limits[0]
    if (
        limit.metric != "torsion_midspan_u3"
        or limit.reference != "analytic"
        or limit.comparator != "relative"
        or limit.threshold.dimension() != "dimensionless"
        or not math.isfinite(limit.threshold.base())
        or not 0 < limit.threshold.base() <= 0.02
    ):
        raise AnalysisRefused(
            "direct-torsion-c3d20-v1 requires a relative midspan-U3 limit up to 2 percent"
        )
    return {
        "declared_material": (
            units.convert(material.youngs_modulus.value, material.youngs_modulus.unit, "Pa"),
            material.poisson_ratio.base(),
        ),
        "declared_twist": twist,
        "acceptance_limit": limit,
    }


def _admit_direct_torsion_c3d20_deck(data: bytes, **declared: Any) -> _DirectDeckAdmission:
    """Admit only the two retained C3D20 annuli and bind their TOP field to the declaration."""
    if hashlib.sha256(data).hexdigest() not in _TORSION_C3D20_SHA256:
        raise AnalysisRefused("direct torsion deck is not one retained byte-pinned C3D20 annulus")
    try:
        text = data.decode("utf-8")
        nodes: dict[int, tuple[float, float, float]] = {}
        nsets: dict[str, set[int]] = {}
        mode: str | None = None
        set_name: str | None = None
        elastic: tuple[float, float] | None = None
        boundary_rows: list[list[str]] = []
        node_prints: list[tuple[str, list[str]]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("**"):
                continue
            upper = line.upper()
            if upper.startswith("*"):
                mode = None
                set_name = None
                if upper == "*NODE":
                    mode = "node"
                elif upper.startswith("*NSET,"):
                    selected = dict(
                        item.strip().upper().split("=", 1) for item in upper.split(",")[1:]
                    )
                    set_name = selected.get("NSET")
                    if set_name is None or set_name in nsets:
                        raise ValueError("invalid node set")
                    nsets[set_name] = set()
                    mode = "nset"
                elif upper == "*ELASTIC":
                    mode = "elastic"
                elif upper == "*BOUNDARY":
                    mode = "boundary"
                elif upper.startswith("*NODE PRINT,"):
                    selected = dict(
                        item.strip().upper().split("=", 1) for item in upper.split(",")[1:]
                    )
                    if selected.get("GLOBAL") != "YES" or selected.get("NSET") not in {
                        "TOP",
                        "SUPPORT",
                        "MIDSPAN",
                    }:
                        raise ValueError("unsupported node print")
                    node_prints.append((selected["NSET"], []))
                    mode = "node_print"
                continue
            values = [item.strip().upper() for item in line.split(",")]
            if mode == "node":
                if len(values) != 4:
                    raise ValueError("invalid node row")
                node = int(values[0])
                if node <= 0 or node in nodes:
                    raise ValueError("duplicate node")
                nodes[node] = tuple(float(value) for value in values[1:])
            elif mode == "nset":
                assert set_name is not None
                nsets[set_name].update(int(value) for value in values)
            elif mode == "elastic":
                if elastic is not None or len(values) != 2:
                    raise ValueError("invalid elastic row")
                elastic = (float(values[0]), float(values[1]))
            elif mode == "boundary":
                boundary_rows.append(values)
            elif mode == "node_print":
                node_prints[-1][1].extend(values)
        if (
            set(nsets) != {"SUPPORT", "TOP", "MIDSPAN"}
            or not all(nsets.values())
            or nsets["MIDSPAN"] != {11}
            or not set().union(*nsets.values()) <= set(nodes)
            or elastic is None
            or not all(math.isfinite(value) for value in elastic)
            or any(
                not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=0)
                for actual, expected in zip(elastic, declared["declared_material"], strict=True)
            )
        ):
            raise ValueError("deck nodes, sets, or material differ from declaration")
        boundaries: dict[tuple[int, int], float] = {}
        support_dofs: set[int] = set()
        for row in boundary_rows:
            if row[0] == "SUPPORT":
                if len(row) != 3 or row[1:] != ["1", "3"]:
                    raise ValueError("SUPPORT must fix all translations")
                support_dofs.update({1, 2, 3})
                continue
            if len(row) != 4:
                raise ValueError("invalid TOP twist row")
            node, dof = int(row[0]), int(row[1])
            if node not in nsets["TOP"] or dof not in {2, 3} or (node, dof) in boundaries:
                raise ValueError("invalid TOP twist node or component")
            boundaries[(node, dof)] = float(row[3])
        if support_dofs != {1, 2, 3} or set(boundaries) != {
            (node, dof) for node in nsets["TOP"] for dof in (2, 3)
        }:
            raise ValueError("TOP/SUPPORT boundary coverage differs from declaration")
        twist = declared["declared_twist"]
        for node in nsets["TOP"]:
            _, y, z = nodes[node]
            if not (
                math.isclose(boundaries[(node, 2)], -twist * z, rel_tol=0, abs_tol=1e-12)
                and math.isclose(boundaries[(node, 3)], twist * y, rel_tol=0, abs_tol=1e-12)
            ):
                raise ValueError("TOP displacement field differs from declared x rotation")
        if node_prints != [("TOP", ["U", "RF"]), ("SUPPORT", ["RF"]), ("MIDSPAN", ["U"])]:
            raise ValueError("torsion node-print requests differ from the admitted result set")
        requests = (
            {("U", "TOP", node) for node in nsets["TOP"]}
            | {("RF", "TOP", node) for node in nsets["TOP"]}
            | {("RF", "SUPPORT", node) for node in nsets["SUPPORT"]}
            | {("U", "MIDSPAN", 11)}
        )
        xs = [xyz[0] for xyz in nodes.values()]
        if min(xs) != 0 or max(xs) != 1:
            raise ValueError("torsion annulus must span one metre in x")
        return _DirectDeckAdmission(requests, 1.0, nodes)
    except (UnicodeError, ValueError, KeyError) as error:
        raise AnalysisRefused(f"direct C3D20 torsion deck: {error}") from error


def _declared_sliding_contact(structural: Any, procedure: StructuralProcedure) -> dict[str, float]:
    """Bind the two retained face-contact procedures to their declared inputs."""
    deck = structural.model.model_artifact
    if (
        procedure.kind != "nonlinear_static"
        or deck is None
        or deck.format != "calculix_inp"
        or structural.model.mesh_artifact is not None
        or len(procedure.assignment_ids) != 1
        or len(procedure.load_ids) != 2
        or len(procedure.contact_ids) != 1
        or procedure.mass_ids
        or procedure.damping_ids
        or procedure.geometric_nonlinearity is not True
        or procedure.plasticity is not False
        or procedure.max_increments != 100
    ):
        raise AnalysisRefused(
            "sliding-contact-v1 requires the declared nonlinear contact procedure"
        )
    assignment = next(
        row for row in structural.assignments if row.id == procedure.assignment_ids[0]
    )
    section = next(row for row in structural.sections if row.id == assignment.section_id)
    material = next(row for row in structural.materials if row.id == section.material_id)
    contact = next(row for row in structural.contacts if row.id == procedure.contact_ids[0])
    loads = {row.id: row for row in structural.loads if row.id in procedure.load_ids}
    normal = loads.get("upper-normal")
    slide = loads.get("upper-slide")
    if (
        assignment.region.kind != "mesh_region"
        or assignment.region.name != "UPPER"
        or section.kind != "solid"
        or material.id != "elastic-block"
        or material.kind != "linear_isotropic"
        or units.convert(material.youngs_modulus.value, material.youngs_modulus.unit, "Pa")
        != 1000.0
        or material.poisson_ratio.base() != 0.3
        or contact.master.kind != "surface_set"
        or contact.master.name != "LOWER_TOP"
        or contact.slave.kind != "surface_set"
        or contact.slave.name != "UPPER_BOTTOM"
        or contact.frame != structural.model.source_frame
        or contact.normal.kind != "linear"
        or contact.normal.pressure_overclosure_slope is None
        or contact.normal.pressure_overclosure_slope.base() != 1_000_000.0
        or contact.tangential is None
        or normal is None
        or slide is None
        or normal.kind != "force"
        or normal.region.kind != "node_set"
        or normal.region.name != "UPPER_TOP"
        or normal.vector is None
        or normal.application_frame != structural.model.source_frame
        or normal.amplitude is not None
        or slide.kind != "prescribed_displacement"
        or slide.region.kind != "node_set"
        or slide.region.name != "UPPER_TOP"
        or slide.vector is None
        or slide.application_frame != structural.model.source_frame
        or slide.amplitude is not None
    ):
        raise AnalysisRefused("sliding-contact-v1 declared material, contact, or load differs")
    friction = contact.tangential.friction_coefficient.base()
    force = tuple(units.convert(value, normal.vector.unit, "N") for value in normal.vector.values)
    displacement = tuple(
        units.convert(value, slide.vector.unit, "m") for value in slide.vector.values
    )
    if friction != 0.2 or force != (0.0, 0.0, -40.0) or displacement[1:] != (0.0, 0.0):
        raise AnalysisRefused(
            "sliding-contact-v1 declared friction, normal load, or travel differs"
        )
    if displacement[0] not in {0.02, 0.04}:
        raise AnalysisRefused("sliding-contact-v1 requires the declared 20 mm or 40 mm travel")
    return {
        "tangential_displacement_m": displacement[0],
        "friction_coefficient": friction,
        "normal_load_n": abs(force[2]),
    }


def _admit_sliding_contact_deck(
    data: bytes,
    *,
    tangential_displacement_m: float,
    friction_coefficient: float,
    normal_load_n: float,
) -> _SlidingContactAdmission:
    """Bind exactly one declared face-to-face sliding fixture before launch."""
    expected_hash = (
        _SLIDING_CONTACT_SHA256
        if tangential_displacement_m == 0.02
        else _SLIDING_CONTACT_EXTENDED_SHA256
        if tangential_displacement_m == 0.04
        else None
    )
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise AnalysisRefused("sliding-contact-v1 requires the declared fixture SHA-256")
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise AnalysisRefused("sliding-contact-v1 deck is not UTF-8") from error
    required = (
        "*CONTACT PAIR, INTERACTION=SLIDING_LAW, TYPE=SURFACE TO SURFACE\n" + _SLIDING_CONTACT_PAIR,
        f"*CONTACT PRINT, FREQUENCY={'999999' if tangential_displacement_m == 0.02 else '1'}, SLAVE=UPPER_BOTTOM, MASTER=LOWER_TOP\n"
        "CDIS, CSTR, CNUM, CF, CFN, CFS",
    )
    if any(token not in text for token in required):
        raise AnalysisRefused("sliding-contact-v1 contact pair or final output differs")
    return _SlidingContactAdmission(
        history_blocks=1 if tangential_displacement_m == 0.02 else 24,
        tangential_displacement_m=tangential_displacement_m,
        friction_coefficient=friction_coefficient,
        normal_load_n=normal_load_n,
    )


def _parse_sliding_contact_output(
    text: str, admission: _SlidingContactAdmission
) -> dict[str, float]:
    """Consume only the retained final CCX face-contact grammar."""

    def number(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise AnalysisRefused("nonfinite sliding-contact output")
        return result

    def blocks(
        pattern: str, label: str, *, flags: int = re.IGNORECASE | re.MULTILINE
    ) -> list[re.Match[str]]:
        matches = list(re.finditer(pattern, text, flags))
        if len(matches) != admission.history_blocks:
            raise AnalysisRefused(
                f"sliding-contact output requires {admission.history_blocks} {label} block(s)"
            )
        return matches

    time_sets: list[list[float]] = []
    rows_by_kind: dict[str, int] = {}
    cdis_rows: list[tuple[str, str, str]] = []
    cstr_rows: list[tuple[str, str, str]] = []
    for kind, heading in (("CDIS", "relative contact displacement"), ("CSTR", "contact stress")):
        matched_blocks = blocks(
            rf"^\s*{heading} .*? time\s+(?P<time>{_CCX_NUMBER})\s*$"
            rf"(?P<body>.*?)(?=^\s*(?:relative contact displacement|contact stress|total number of contact elements|statistics)\b)",
            kind,
            flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        kind_times: list[float] = []
        for match in matched_blocks:
            kind_times.append(number(match["time"]))
            rows = []
            for line in match["body"].splitlines():
                if not line.strip():
                    continue
                row = re.fullmatch(
                    rf"\s*2\s+1\s+({_CCX_NUMBER})\s+({_CCX_NUMBER})\s+({_CCX_NUMBER})\s*",
                    line,
                )
                if row is None:
                    raise AnalysisRefused(
                        f"sliding-contact {kind} rows are incomplete or malformed"
                    )
                rows.append(row.groups())
            if len(rows) != admission.contact_rows or any(
                not all(math.isfinite(number(value)) for value in row) for row in rows
            ):
                raise AnalysisRefused(f"sliding-contact {kind} rows are incomplete or malformed")
            rows_by_kind[kind] = len(rows)
            if match is matched_blocks[-1]:
                if kind == "CDIS":
                    cdis_rows = rows
                else:
                    cstr_rows = rows
        time_sets.append(kind_times)
    cnum_headings = re.findall(
        r"^[ \t]*total number of contact elements for time\b.*$", text, re.IGNORECASE | re.MULTILINE
    )
    if len(cnum_headings) != admission.history_blocks:
        raise AnalysisRefused(
            f"sliding-contact output requires {admission.history_blocks} CNUM block(s)"
        )
    counts = blocks(
        rf"^[ \t]*total number of contact elements for time[ \t]+(?P<time>{_CCX_NUMBER})[ \t]*\r?\n"
        rf"(?:[ \t]*\r?\n)*[ \t]*(?P<count>\d+)[ \t]*$",
        "CNUM",
    )
    count_times: list[float] = []
    for count in counts:
        count_times.append(number(count["time"]))
        if int(count["count"]) != admission.contact_rows:
            raise AnalysisRefused("sliding-contact CNUM differs from the retained row count")
    time_sets.append(count_times)
    blocks = list(
        re.finditer(
            rf"^\s*statistics for slave set UPPER_BOTTOM, master set LOWER_TOP and time\s+(?P<time>{_CCX_NUMBER})\s*$"
            r"(?P<body>.*?)(?=^\s*statistics for slave set|\Z)",
            text,
            re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
    )
    if len(blocks) != 3 * admission.history_blocks:
        raise AnalysisRefused("sliding-contact CF/CFN/CFS blocks are missing or duplicated")
    forces: dict[str, tuple[float, float, float]] = {}
    labels = {
        "CF": "total surface force",
        "CFN": "total normal surface force",
        "CFS": "total shear surface force",
    }
    force_times: list[float] = []
    group_labels: set[str] = set()
    for index, block in enumerate(blocks):
        time = number(block["time"])
        if index % 3 == 0:
            force_times.append(time)
            group_labels = set()
        elif time != force_times[-1]:
            raise AnalysisRefused("sliding-contact CF/CFN/CFS times differ")
        matched = [
            key
            for key, label in labels.items()
            if re.search(rf"^\s*{label} ", block["body"], re.IGNORECASE | re.MULTILINE)
        ]
        if len(matched) != 1 or matched[0] in group_labels:
            raise AnalysisRefused("sliding-contact CF/CFN/CFS blocks are malformed")
        group_labels.add(matched[0])
        area = re.search(
            rf"area,\s+normal force \(\+ = tension\) and shear force \(size\)\s*$\s*"
            rf"(?P<area>{_CCX_NUMBER})\s+(?P<normal>{_CCX_NUMBER})\s+(?P<shear>{_CCX_NUMBER})\s*$",
            block["body"],
            re.IGNORECASE | re.MULTILINE,
        )
        if area is None:
            raise AnalysisRefused("sliding-contact force summary is malformed")
        if index // 3 == admission.history_blocks - 1:
            forces[matched[0]] = tuple(number(area[name]) for name in ("area", "normal", "shear"))
        if index % 3 == 2 and group_labels != set(labels):
            raise AnalysisRefused("sliding-contact CF/CFN/CFS blocks are malformed")
    time_sets.append(force_times)
    if any(
        len(values) != admission.history_blocks
        or not math.isclose(values[-1], admission.final_time_s, rel_tol=0.0, abs_tol=1e-12)
        or any(right <= left for left, right in pairwise(values))
        for values in time_sets
    ) or any(
        any(
            not math.isclose(value, reference, rel_tol=0.0, abs_tol=1e-12)
            for value, reference in zip(values, time_sets[0], strict=True)
        )
        for values in time_sets[1:]
    ):
        raise AnalysisRefused("sliding-contact output differs from the retained final time")
    if any(number(row[0]) <= 0 for row in cstr_rows):
        raise AnalysisRefused("sliding-contact CSTR requires positive pressure")
    return {
        "contact_rows": float(rows_by_kind["CDIS"]),
        "normal_force_n": abs(forces["CF"][1]),
        "shear_force_n": forces["CFS"][2],
        "tangential_displacement_m": max(abs(number(row[1])) for row in cdis_rows),
        "positive_tangential_motion": float(all(number(row[1]) > 0 for row in cdis_rows)),
        "compressive_contact": float(
            all(
                number(cdis[0]) < 0 and number(cstr[0]) > 0
                for cdis, cstr in zip(cdis_rows, cstr_rows, strict=True)
            )
        ),
        "coulomb_admissible": float(
            all(
                0
                <= math.hypot(number(cstr[1]), number(cstr[2]))
                <= admission.friction_coefficient * number(cstr[0])
                for cstr in cstr_rows
            )
        ),
        "coulomb_mobilized_at_printed_precision": float(
            any(
                round(math.hypot(number(cstr[1]), number(cstr[2])) / number(cstr[0]), 6)
                == round(admission.friction_coefficient, 6)
                for cstr in cstr_rows
            )
        ),
    }


def _score_sliding_contact(
    structural: Any, admission: _SlidingContactAdmission, parsed: dict[str, float]
) -> dict[str, Any]:
    limits = structural.acceptance_limits
    if len(limits) != 1 or not isinstance(limits[0], ScalarAgreementLimit):
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "sliding contact requires one analytic limit",
        }
    limit = limits[0]
    metric = (
        "sliding_contact_static"
        if admission.tangential_displacement_m == 0.02
        else "sliding_contact_extended_travel"
    )
    if (
        limit.metric != metric
        or limit.reference != "analytic"
        or limit.comparator != "relative"
        or limit.threshold.dimension() != "dimensionless"
        or not math.isfinite(limit.threshold.base())
        or not 0 < limit.threshold.base() <= 0.05
    ):
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "sliding contact requires a relative analytic limit at or below 5%",
        }
    expected_normal, friction = admission.normal_load_n, admission.friction_coefficient
    expected_shear = friction * parsed["normal_force_n"]
    comparisons = [
        {
            "metric": "sliding_contact_positive_tangential_motion",
            "observed": {"value": parsed["positive_tangential_motion"], "unit": "1"},
            "expected": {"value": 1.0, "unit": "1"},
            "relative_error": 0.0 if parsed["positive_tangential_motion"] == 1.0 else math.inf,
            "passed": parsed["positive_tangential_motion"] == 1.0,
        },
        {
            "metric": "sliding_contact_compressive_contact",
            "observed": {"value": parsed["compressive_contact"], "unit": "1"},
            "expected": {"value": 1.0, "unit": "1"},
            "relative_error": 0.0 if parsed["compressive_contact"] == 1.0 else math.inf,
            "passed": parsed["compressive_contact"] == 1.0,
        },
        {
            "metric": "sliding_contact_coulomb_admissible",
            "observed": {"value": parsed["coulomb_admissible"], "unit": "1"},
            "expected": {"value": 1.0, "unit": "1"},
            "relative_error": 0.0 if parsed["coulomb_admissible"] == 1.0 else math.inf,
            "passed": parsed["coulomb_admissible"] == 1.0,
        },
        {
            "metric": "sliding_contact_coulomb_mobilized_at_printed_precision",
            "observed": {"value": parsed["coulomb_mobilized_at_printed_precision"], "unit": "1"},
            "expected": {"value": 1.0, "unit": "1"},
            "relative_error": 0.0
            if parsed["coulomb_mobilized_at_printed_precision"] == 1.0
            else math.inf,
            "passed": parsed["coulomb_mobilized_at_printed_precision"] == 1.0,
        },
    ]
    for metric, observed, expected in (
        ("sliding_contact_normal_load", parsed["normal_force_n"], expected_normal),
        ("sliding_contact_coulomb_shear", parsed["shear_force_n"], expected_shear),
    ):
        error = abs(observed - expected) / expected
        comparisons.append(
            {
                "metric": metric,
                "observed": {"value": observed, "unit": "N"},
                "expected": {"value": expected, "unit": "N"},
                "relative_error": error,
                "passed": math.isfinite(error) and error <= limit.threshold.base(),
            }
        )
    return {
        "simulation_validity": "GO" if all(row["passed"] for row in comparisons) else "NO_GO",
        "acceptance_limit_id": limit.id,
        "acceptance_provenance": limit.provenance.model_dump(mode="json"),
        "threshold": limit.threshold.model_dump(mode="json"),
        "analytic_operands": {
            "friction_coefficient": {"value": friction, "unit": "1"},
            "declared_normal_load": {"value": expected_normal, "unit": "N"},
            "positive_tangential_displacement": {
                "value": parsed["tangential_displacement_m"],
                "unit": "m",
            },
        },
        "comparisons": comparisons,
        "reason": "declared Coulomb sliding-contact outputs "
        + ("agree" if all(row["passed"] for row in comparisons) else "disagree")
        + "; physical conformance remains unscored",
    }


def _declared_frictionless_impact(
    structural: Any, procedure: StructuralProcedure
) -> dict[str, Any]:
    """The one-translation-DOF fixture has no inferred mass or damping defaults."""
    artifact = structural.model.model_artifact
    if (
        procedure.kind != "impact"
        or artifact is None
        or artifact.format != "calculix_inp"
        or structural.model.mesh_artifact is not None
        or set(procedure.assignment_ids) != {"wall-solid", "impactor-solid"}
        or set(procedure.support_ids) != {"wall-fixed", "ref-xy", "rotation-fixed"}
        or procedure.mass_ids != ["ref-point-mass"]
        or procedure.contact_ids != ["elastic-contact"]
        or procedure.load_ids
        or procedure.damping_ids
        or procedure.damping_mode != "undamped"
        or procedure.duration is None
        or procedure.duration.base() != 0.15
        or procedure.time_step is None
        or procedure.time_step.base() != 1e-5
        or procedure.max_increments != 30000
        or procedure.initial_velocity is None
        or procedure.initial_velocity.frame != structural.model.source_frame
        or tuple(procedure.initial_velocity.values) != (0.0, 0.0, -1.0)
    ):
        raise AnalysisRefused(
            "frictionless-impact-v1 requires the declared undamped impact procedure"
        )
    assignments = {row.id: row for row in structural.assignments}
    sections = {row.id: row for row in structural.sections}
    materials = {row.id: row for row in structural.materials}
    for aid, region in (("wall-solid", "WALL"), ("impactor-solid", "IMPACTOR")):
        assignment = assignments[aid]
        section = sections[assignment.section_id]
        material = materials[section.material_id]
        if (
            assignment.region.kind != "mesh_region"
            or assignment.region.name != region
            or section.kind != "solid"
            or material.kind != "linear_isotropic"
            or units.convert(material.youngs_modulus.value, material.youngs_modulus.unit, "Pa")
            != 1000.0
            or material.poisson_ratio.base() != 0.3
            or material.density is None
            or material.density.base() != 1.0
        ):
            raise AnalysisRefused("frictionless-impact-v1 material or solid region differs")
    mass = next(row for row in structural.masses if row.id == "ref-point-mass")
    if (
        mass.region.kind != "node_set"
        or mass.region.name != "REF"
        or mass.mass.base() != 0.9
        or mass.rotational_inertia is not None
    ):
        raise AnalysisRefused("frictionless-impact-v1 requires the distinct REF point mass")
    supports = {row.id: row for row in structural.supports}
    for sid, region, axes in (
        ("wall-fixed", "WALL_FIXED", {"x", "y", "z"}),
        ("ref-xy", "REF", {"x", "y"}),
        ("rotation-fixed", "101", {"x", "y", "z"}),
    ):
        support = supports[sid]
        if (
            support.region.kind != "node_set"
            or support.region.name != region
            or set(support.dofs.translation) != axes
            or support.dofs.rotation
            or support.dofs.frame != structural.model.source_frame
            or support.dofs.prescribed_translation
        ):
            raise AnalysisRefused("frictionless-impact-v1 support differs")
    contact = next(row for row in structural.contacts if row.id == "elastic-contact")
    if (
        contact.master.kind != "surface_set"
        or contact.master.name != "WALL_TOP"
        or contact.slave.kind != "surface_set"
        or contact.slave.name != "IMPACTOR_BOTTOM"
        or contact.frame != structural.model.source_frame
        or contact.normal.kind != "linear"
        or contact.normal.pressure_overclosure_slope is None
        or contact.normal.pressure_overclosure_slope.base() != 1e7
        or contact.tangential is not None
        or contact.damping is not None
    ):
        raise AnalysisRefused("frictionless-impact-v1 contact law differs")
    return {"mass_kg": 1.0, "incident_velocity_m_s": -1.0, "time_step_s": 1e-5, "increments": 15000}


def _admit_frictionless_impact_deck(data: bytes, **_declared: Any) -> None:
    if hashlib.sha256(data).hexdigest() != _FRICTIONLESS_IMPACT_SHA256:
        raise AnalysisRefused("frictionless-impact-v1 requires the declared fixture SHA-256")


def _parse_frictionless_impact_output(
    text: str, stdout: str, *, increments: int = 15000, time_step_s: float = 1e-5
) -> dict[str, float]:
    """Require every native increment before deriving one rigid-body impact history."""

    def finite(raw: str) -> float:
        try:
            value = float(raw)
        except ValueError as error:
            raise AnalysisRefused("impact output has a malformed number") from error
        if not math.isfinite(value):
            raise AnalysisRefused("impact output has a nonfinite active value")
        return value

    def section(lines: list[str], label: str, expected_time: float) -> list[list[str]]:
        headings = [index for index, line in enumerate(lines) if line.strip().startswith(label)]
        if len(headings) != 1:
            raise AnalysisRefused(f"impact output requires one {label} section per increment")
        heading = lines[headings[0]]
        match = re.search(rf"(?:and|for) time[ \t]+({_CCX_NUMBER})[ \t]*$", heading)
        if match is None or not math.isclose(finite(match[1]), expected_time, abs_tol=1e-9):
            raise AnalysisRefused("impact output section times differ")
        rows: list[list[str]] = []
        for line in lines[headings[0] + 1 :]:
            if not line.strip():
                if rows:
                    break
                continue
            parts = line.split()
            if re.fullmatch(_CCX_NUMBER, parts[0]) is None:
                break
            rows.append(parts)
        return rows

    if not re.match(r"^\s*S T E P\s+1\s+INCREMENT", text):
        raise AnalysisRefused("impact output lacks the native step preamble")
    headings = list(re.finditer(r"^[ \t]*INCREMENT[ \t]+(\d+)[ \t]*$", text, re.MULTILINE))
    if len(headings) != increments:
        raise AnalysisRefused("impact output is missing, duplicate, or truncated increments")
    selected = re.findall(rf"SELECTED time increment[^\n]*:[ \t]*({_CCX_NUMBER})", stdout)
    if not selected or any(
        not math.isclose(finite(value), time_step_s, abs_tol=1e-12) for value in selected
    ):
        raise AnalysisRefused("impact solver did not select the declared direct time step")
    if re.search(r"mass[ -]?scal", stdout, re.IGNORECASE):
        raise AnalysisRefused("impact solver reports mass scaling")
    work: list[list[float]] = []
    for label in (
        "actual total time",
        "external work",
        "work performed by the damping forces",
        "energy lost due to friction",
    ):
        values = re.findall(
            rf"^[ \t]*{re.escape(label)}[ \t]*=[ \t]*({_CCX_NUMBER})", stdout, re.MULTILINE
        )
        if len(values) != increments:
            raise AnalysisRefused(f"impact solver work history lacks {label}")
        work.append([finite(value) for value in values])
    if any(
        not math.isclose(time, (index + 1) * time_step_s, abs_tol=1e-9)
        for index, time in enumerate(work[0])
    ):
        raise AnalysisRefused("impact solver time history differs from the direct increments")
    if any(value != 0 for series in work[1:] for value in series):
        raise AnalysisRefused("impact solver reports external, damping, or friction work")

    velocities: list[float] = []
    positions: list[float] = []
    energies: list[float] = []
    forces: list[float] = []
    counts: list[int] = []
    gaps: list[float] = []
    native_elke_last = 0.0
    for index, heading in enumerate(headings):
        if int(heading[1]) != index + 1:
            raise AnalysisRefused("impact output increments are out of order")
        block = text[
            heading.end() : headings[index + 1].start() if index + 1 < increments else len(text)
        ]
        lines = block.splitlines()
        time = (index + 1) * time_step_s
        if (
            len(re.findall(rf"\b(?:and|for) time[ \t]+{_CCX_NUMBER}[ \t]*$", block, re.MULTILINE))
            != 14
        ):
            raise AnalysisRefused("impact output lacks a requested native time section")
        for label in ("displacements (", "velocities ("):
            rows = section(lines, label, time)
            if len(rows) != 1 or len(rows[0]) != 4 or rows[0][0] != "100":
                raise AnalysisRefused("impact REF displacement/velocity row is incomplete")
            vector = [finite(value) for value in rows[0][1:]]
            if any(abs(value) > 1e-8 for value in vector[:2]):
                raise AnalysisRefused("impact REF has undeclared lateral motion")
            if label.startswith("velocities"):
                velocities.append(vector[2])
            else:
                positions.append(vector[2])
        for label in ("internal energy (", "kinetic energy ("):
            rows = section(lines, label, time)
            if (
                len(rows) != 3
                or [row[0] for row in rows] != ["1", "2", "3"]
                or any(len(row) != 2 for row in rows)
            ):
                raise AnalysisRefused("impact native element energy rows are incomplete")
            for row in rows:
                finite(row[1])
            if label.startswith("kinetic"):
                native_elke_last = sum(finite(row[1]) for row in rows)
        for label in ("total internal energy", "total kinetic energy"):
            rows = section(lines, label, time)
            if len(rows) != 1 or len(rows[0]) != 1:
                raise AnalysisRefused("impact native energy total is incomplete")
            finite(rows[0][0])
        count_rows = section(lines, "total number of contact elements", time)
        if len(count_rows) != 1 or len(count_rows[0]) != 1 or count_rows[0][0] not in {"0", "28"}:
            raise AnalysisRefused("impact contact status is missing or unsupported")
        count = int(count_rows[0][0])
        counts.append(count)
        spring_rows: list[float] = []
        contact_rows: dict[str, list[list[str]]] = {}
        for label, width in (
            ("relative contact displacement", 5),
            ("contact stress", 5),
            ("contact spring energy", 3),
        ):
            rows = section(lines, label, time)
            if len(rows) != count or any(
                len(row) != width or row[:2] != ["2", "1"] for row in rows
            ):
                raise AnalysisRefused("impact contact integration rows are missing or malformed")
            for row in rows:
                values = [finite(value) for value in row[2:]]
                if label != "contact spring energy" and any(
                    abs(value) > 1e-8 for value in values[1:]
                ):
                    raise AnalysisRefused("impact contact reports tangential motion or stress")
            if label == "relative contact displacement" and rows:
                gaps.extend(finite(row[2]) for row in rows)
            if label == "contact spring energy":
                spring_rows = [finite(row[2]) for row in rows]
            contact_rows[label] = rows
        for displacement, stress in zip(
            contact_rows["relative contact displacement"],
            contact_rows["contact stress"],
            strict=True,
        ):
            gap, pressure = finite(displacement[2]), finite(stress[2])
            # REF U3 is printed at ~0.1 m to seven significant digits (5e-8 m
            # rounding); CDIS/CSTR are also printed to seven digits. These are
            # output-precision checks of the exact deck's 0.1 m initial gap and
            # 1e7 Pa/m law, separate from the declared 5% acceptance limit.
            if not math.isclose(gap, 0.1 + positions[-1], rel_tol=0, abs_tol=1e-7):
                raise AnalysisRefused("impact active CDIS differs from REF geometric gap")
            if not math.isclose(pressure, -1e7 * gap, rel_tol=0, abs_tol=1e-3):
                raise AnalysisRefused("impact active CSTR differs from the declared linear penalty")
        energy_rows = section(lines, "total contact spring energy", time)
        if len(energy_rows) != 1 or len(energy_rows[0]) != 1:
            raise AnalysisRefused("impact contact spring energy is missing")
        spring_energy = finite(energy_rows[0][0])
        if (
            spring_energy < 0
            or any(value < 0 for value in spring_rows)
            or not math.isclose(sum(spring_rows), spring_energy, rel_tol=0, abs_tol=1e-6)
        ):
            raise AnalysisRefused("impact contact spring energy rows differ from the native total")
        energies.append(0.5 * velocities[-1] ** 2 + spring_energy)
        stats = [
            line_index
            for line_index, line in enumerate(lines)
            if line.strip().startswith("statistics for slave set IMPACTOR_BOTTOM")
        ]
        if len(stats) != 3:
            raise AnalysisRefused("impact CF/CFN/CFS summaries are incomplete")
        force_vectors = []
        for stat_index, start in enumerate(stats):
            stat_time = re.search(rf"and time[ \t]+({_CCX_NUMBER})[ \t]*$", lines[start])
            if stat_time is None or not math.isclose(finite(stat_time[1]), time, abs_tol=1e-9):
                raise AnalysisRefused("impact CF/CFN/CFS summary times differ")
            part = "\n".join(lines[start : stats[stat_index + 1] if stat_index < 2 else len(lines)])
            label = (
                "total surface force",
                "total normal surface force",
                "total shear surface force",
            )[stat_index]
            pattern = rf"^[ \t]*{label}[^\n]*\n(?:[ \t]*\n)*[ \t]*({_CCX_NUMBER}(?:[ \t]+{_CCX_NUMBER}){{5}})[ \t]*$"
            match = re.search(pattern, part, re.MULTILINE)
            if match is None:
                raise AnalysisRefused("impact CF/CFN/CFS force vector is malformed")
            force_vectors.append([finite(value) for value in match[1].split()])
        if any(abs(value) > 1e-6 for value in force_vectors[2][:3]) or not math.isclose(
            force_vectors[0][2], force_vectors[1][2], abs_tol=1e-6
        ):
            raise AnalysisRefused("impact contact force is not frictionless normal force")
        force = force_vectors[1][2]
        if count == 0 and force != 0:
            raise AnalysisRefused("impact inactive contact retains normal force")
        forces.append(force)
    active = [index for index, count in enumerate(counts) if count]
    if (
        not active
        or active[0] == 0
        or active[-1] == increments - 1
        or active != list(range(active[0], active[-1] + 1))
    ):
        raise AnalysisRefused(
            "impact requires initial clearance, one contact interval, and final separation"
        )
    first, last = active[0], active[-1]
    if 0.1 + positions[0] <= 0 or any(0.1 + position <= 0 for position in positions[last + 1 :]):
        raise AnalysisRefused("impact initial clearance or final geometric separation is absent")
    if velocities[0] != -1.0 or velocities[first - 1] >= 0 or velocities[-1] <= 0:
        raise AnalysisRefused("impact incident/rebound velocity history is invalid")
    impulse = sum(forces[first : last + 1]) * time_step_s
    delta_v = velocities[last + 1] - velocities[first - 1]
    peak_force = max(forces)
    if impulse <= 0 or delta_v <= 0 or peak_force <= 0 or not gaps or min(gaps) >= 0:
        raise AnalysisRefused("impact contact has no compressive impulse")
    tail_force = min(forces[first : last + 1])
    if any(force < 0 for force in forces[first:last]) or (
        tail_force < 0 and (forces[last] != tail_force or forces[last + 1] != 0)
    ):
        raise AnalysisRefused("impact tensile tail is not a one-increment release artifact")
    return {
        "first_contact_increment": float(first + 1),
        "last_contact_increment": float(last + 1),
        "contact_rows": 28.0,
        "impulse_n_s": impulse,
        "delta_v_m_s": delta_v,
        "effective_mass_kg": impulse / delta_v,
        "incident_velocity_m_s": velocities[first - 1],
        "rebound_velocity_m_s": velocities[-1],
        "peak_force_n": peak_force,
        "peak_compression_m": -min(gaps),
        "reconstructed_energy_max_j": max(energies),
        "reconstructed_energy_min_j": min(energies),
        "reconstructed_energy_final_j": energies[-1],
        "release_tail_force_n": tail_force,
        "native_elke_final_j_diagnostic_only": native_elke_last,
    }


def _score_frictionless_impact(structural: Any, parsed: dict[str, float]) -> dict[str, Any]:
    limits = structural.acceptance_limits
    if len(limits) != 1 or not isinstance(limits[0], ScalarAgreementLimit):
        return {"simulation_validity": "NOT_SCORED", "reason": "impact requires one analytic limit"}
    limit = limits[0]
    if (
        limit.metric != "frictionless_elastic_impact"
        or limit.reference != "analytic"
        or limit.comparator != "relative"
        or limit.threshold.dimension() != "dimensionless"
        or not math.isfinite(limit.threshold.base())
        or not 0 < limit.threshold.base() <= 0.05
    ):
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "impact requires a relative analytic limit at or below 5%",
        }
    tolerance = limit.threshold.base()
    ideal_energy = 0.5
    ideal_compression = math.sqrt(2 * ideal_energy / 1e7)
    ideal_force = 1e7 * ideal_compression
    comparisons = []
    for metric, observed, expected, unit in (
        ("impact_effective_mass", parsed["effective_mass_kg"], 1.0, "kg"),
        (
            "impact_restitution",
            abs(parsed["rebound_velocity_m_s"] / parsed["incident_velocity_m_s"]),
            1.0,
            "1",
        ),
        (
            "impact_final_reconstructed_energy",
            parsed["reconstructed_energy_final_j"],
            ideal_energy,
            "J",
        ),
        (
            "impact_peak_reconstructed_energy",
            parsed["reconstructed_energy_max_j"],
            ideal_energy,
            "J",
        ),
        (
            "impact_min_reconstructed_energy",
            parsed["reconstructed_energy_min_j"],
            ideal_energy,
            "J",
        ),
        ("impact_impulse", parsed["impulse_n_s"], 2.0, "N s"),
        ("impact_peak_compression", parsed["peak_compression_m"], ideal_compression, "m"),
        ("impact_peak_normal_force", parsed["peak_force_n"], ideal_force, "N"),
        (
            "impact_release_tail_fraction",
            max(0.0, -parsed["release_tail_force_n"]) / ideal_force,
            0.0,
            "1",
        ),
    ):
        error = (
            observed
            if metric == "impact_release_tail_fraction"
            else abs(observed - expected) / abs(expected)
        )
        comparisons.append(
            {
                "metric": metric,
                "observed": {"value": observed, "unit": unit},
                "expected": {"value": expected, "unit": unit},
                "relative_error": error,
                "passed": math.isfinite(error) and error <= tolerance,
            }
        )
    passed = all(row["passed"] for row in comparisons)
    return {
        "simulation_validity": "GO" if passed else "NO_GO",
        "acceptance_limit_id": limit.id,
        "acceptance_provenance": limit.provenance.model_dump(mode="json"),
        "threshold": limit.threshold.model_dump(mode="json"),
        "analytic_operands": {
            "declared_total_mass": {"value": 1.0, "unit": "kg"},
            "solid_mass": {"value": 0.1, "unit": "kg"},
            "distinct_ref_point_mass": {"value": 0.9, "unit": "kg"},
            "incident_kinetic_energy": {"value": ideal_energy, "unit": "J"},
            "normal_penalty": {"value": 1e7, "unit": "N/m^3"},
            "effective_face_area": {"value": 1.0, "unit": "m^2"},
            "native_elke_final_diagnostic_only": {
                "value": parsed["native_elke_final_j_diagnostic_only"],
                "unit": "J",
            },
        },
        "comparisons": comparisons,
        "reason": "complete undamped native impact history "
        + ("agrees" if passed else "disagrees")
        + " with the declared one-DOF reference; native ELKE omits point-mass energy and is diagnostic only; physical conformance remains unscored"
        + _FIXTURE_LABEL,
    }


# Every deck template with a byte-level admission check. Gating admission on this
# set (not on whether a declaration dict happened to come back) keeps templates
# whose declare-step returns None on success, e.g. the two-DOF templates, from
# silently skipping their deck-hash admission.
_DIRECT_DECK_TEMPLATES = frozenset(
    {
        "direct-truss-v1",
        "direct-b31-square-v1",
        "direct-plate-s4-v1",
        "direct-shear-c3d20-v1",
        "direct-torsion-c3d20-v1",
        "direct-modal-truss-v1",
        "direct-two-dof-modal-v1",
        "direct-two-dof-harmonic-v1",
        "direct-two-dof-transient-v1",
        "sliding-contact-v1",
        "frictionless-impact-v1",
    }
)


def prepare_analysis(
    contract_path: Path,
    *,
    expected_revision: str,
    procedure_id: str,
    evidence_root: Path,
    package_manifest_path: Path,
) -> dict[str, Any]:
    """Validate a structural run and return a non-executing, hash-bound launch document."""

    try:
        raw = contract_path.read_bytes()
    except OSError as error:
        raise AnalysisRefused(f"contract cannot be read: {error}") from error
    if len(raw) > MAX_CONTRACT_BYTES:
        raise AnalysisRefused("contract exceeds the size limit")
    revision = hashlib.sha256(raw).hexdigest()
    if revision != expected_revision:
        raise AnalysisRefused("contract revision changed before analysis preparation")
    try:
        contract = load_contract_bytes(raw)
    except (UnicodeError, ValueError) as error:
        raise AnalysisRefused(f"contract is invalid: {error}") from error
    if contract.structural is None:
        raise AnalysisRefused("contract has no structural section")
    procedure = next(
        (row for row in contract.structural.procedures if row.id == procedure_id), None
    )
    if procedure is None:
        raise AnalysisRefused(f"structural procedure {procedure_id!r} is not declared")

    manifest, manifest_sha256 = _load_package_manifest(package_manifest_path)
    profile_key = contract.structural.runtime.package_profile
    profile = manifest.profiles.get(profile_key)
    if profile is None:
        raise AnalysisRefused(f"runtime package profile {profile_key!r} is not admitted")
    if procedure.kind not in profile.admitted_procedures:
        raise AnalysisRefused(f"procedure kind {procedure.kind!r} is not admitted by the package")
    template = profile.procedure_templates.get(procedure.kind)
    if template is None:
        raise AnalysisRefused(f"procedure kind {procedure.kind!r} has no admitted deck template")
    if procedure.damping_mode == "undamped" and procedure.kind not in profile.undamped_procedures:
        raise AnalysisRefused(f"undamped {procedure.kind!r} is not admitted by the package")
    selected_contacts = {
        row.id: row for row in contract.structural.contacts if row.id in procedure.contact_ids
    }
    if any(row.damping is not None for row in selected_contacts.values()) and (
        procedure.kind not in profile.contact_damping_procedures
    ):
        raise AnalysisRefused(
            f"contact damping for {procedure.kind!r} is not admitted by the package"
        )

    direct_declaration = (
        _declared_direct_truss(contract.structural, procedure)
        if template == "direct-truss-v1"
        else _declared_direct_b31_square(contract.structural, procedure)
        if template == "direct-b31-square-v1"
        else _declared_direct_plate_s4(contract.structural, procedure)
        if template == "direct-plate-s4-v1"
        else _declared_direct_thick_shear(contract.structural, procedure)
        if template == "direct-shear-c3d20-v1"
        else _declared_direct_torsion_c3d20(contract.structural, procedure)
        if template == "direct-torsion-c3d20-v1"
        else _declared_direct_modal_truss(contract.structural, procedure)
        if template == "direct-modal-truss-v1"
        else _declared_two_dof_modal(contract.structural, procedure)
        if template == "direct-two-dof-modal-v1"
        else _declared_two_dof_response(contract.structural, procedure, template)
        if template in {"direct-two-dof-harmonic-v1", "direct-two-dof-transient-v1"}
        else _declared_sliding_contact(contract.structural, procedure)
        if template == "sliding-contact-v1"
        else _declared_frictionless_impact(contract.structural, procedure)
        if template == "frictionless-impact-v1"
        else None
    )

    inspected: list[dict[str, str]] = []
    series_by_hash = {
        load.amplitude.artifact.ref.sha256: load.amplitude
        for load in contract.structural.loads
        if load.id in procedure.load_ids and isinstance(load.amplitude, SeriesAmplitude)
    }
    for artifact in _input_artifacts(procedure, contract.structural):
        if artifact.format not in profile.allowed_artifact_formats:
            raise AnalysisRefused(
                f"artifact format {artifact.format!r} is not admitted by the package profile"
            )
        try:
            data = read_artifact(artifact.ref, evidence_root, max_bytes=MAX_ANALYSIS_ARTIFACT_BYTES)
        except (OSError, ValueError) as error:
            raise AnalysisRefused(f"artifact {artifact.ref.path!r} is invalid: {error}") from error
        _inspect_artifact(data, profile, artifact)
        if (
            template in _DIRECT_DECK_TEMPLATES
            and artifact is contract.structural.model.model_artifact
        ):
            if template == "direct-plate-s4-v1":
                _admit_direct_plate_s4_deck(data, **direct_declaration)
            elif template == "direct-shear-c3d20-v1":
                _admit_direct_thick_shear_deck(data, **direct_declaration)
            elif template == "direct-torsion-c3d20-v1":
                _admit_direct_torsion_c3d20_deck(data, **direct_declaration)
            elif template == "direct-modal-truss-v1":
                _admit_direct_modal_truss_deck(data, **direct_declaration)
            elif template == "direct-two-dof-modal-v1":
                _admit_direct_two_dof_modal_deck(data)
            elif template in {"direct-two-dof-harmonic-v1", "direct-two-dof-transient-v1"}:
                _admit_direct_two_dof_response_deck(data, template)
            elif template == "sliding-contact-v1":
                _admit_sliding_contact_deck(data, **direct_declaration)
            elif template == "frictionless-impact-v1":
                _admit_frictionless_impact_deck(data, **direct_declaration)
            else:
                _admit_direct_truss_deck(data, **direct_declaration)
        if artifact.ref.sha256 in series_by_hash:
            _validate_series(data, series_by_hash[artifact.ref.sha256])
        inspected.append(
            {"role": artifact.role, "path": artifact.ref.path, "sha256": artifact.ref.sha256}
        )

    return {
        "document_version": "1",
        "kind": "analysis_preparation",
        "status": "PREPARED",
        "solver_started": False,
        "contract_revision": revision,
        "procedure_id": procedure.id,
        "procedure_kind": procedure.kind,
        "runtime_profile": profile_key,
        "package_manifest_sha256": manifest_sha256,
        "solver": {"backend": profile.backend, "version": profile.solver_version},
        "deck_template": template,
        "artifacts": inspected,
        "physical_conformance": "NOT_SCORED",
        "simulation_validity": "NOT_SCORED",
        "reason": "declarations and admission validated; no solver evidence exists",
    }


# Exact E5.4 retained macOS package hashes. The child stages and checks
# the complete runtime beside the binary so its existing relative rpath resolves.
# 2026-09-28 source rebuild; recipe in docs/calculix_benchmarks.md
_MACOS_CCX_SHA256 = "75034d36242002cdf6faa77cd6c5d7a3c7a751f331959d81e93a876f44d47068"
_PACKAGED_CCX_SHA256 = _MACOS_CCX_SHA256
_MACOS_CCX_LIBRARIES = {
    "libgcc_s.1.1.dylib": (
        "6aea48f65ead6e183168726eab47fe2dacb09719a04e1b8d702129e04f32791f",
        219600,
    ),
    "libgfortran.5.dylib": (
        "be01eeece9ce9c0ebc327278077a8fb23320d9f48aaf6b6b221ee448b55ec00a",
        2186928,
    ),
    "libquadmath.0.dylib": (
        "27e10f28a8231022286520d7a2ee012c8cdf7ef79de379b0aa2a092848b757c1",
        360752,
    ),
    "libgomp.1.dylib": (
        "1066d0b2f869b69ad22e7cad32d6f4f4d3693b5d734eb0cf57dab88bfe7e25ed",
        459856,
    ),
}
_MAX_SOLVER_OUTPUT = 8 * 1024 * 1024


def _copy_verified_ccx_resource(
    source: Path, target: Path, expected_hash: str, *, expected_size: int | None, mode: int
) -> str:
    digest = hashlib.sha256()
    created = False
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as original:
            info = os.fstat(original.fileno())
            if not stat.S_ISREG(info.st_mode) or (
                expected_size is not None and info.st_size != expected_size
            ):
                raise AnalysisRefused(f"pinned CalculiX resource {source.name} differs")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
            with os.fdopen(fd, "wb") as copied:
                while block := original.read(1024 * 1024):
                    digest.update(block)
                    copied.write(block)
                copied.flush()
                os.fsync(copied.fileno())
        if digest.hexdigest() != expected_hash:
            raise AnalysisRefused(f"pinned CalculiX resource {source.name} hash differs")
        target.chmod(mode)
    except (OSError, AnalysisRefused):
        if created:
            target.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def _verified_ccx(solver_dir: Path) -> tuple[Path, str]:
    """Copy the pinned executable and its macOS runtime beside its existing rpath."""
    named = os.environ.get("LOCKWIRE_CCX", "")
    source = Path(named)
    if not named or not source.is_absolute() or source.name != "ccx_2.23":
        raise AnalysisRefused("pinned CalculiX executable is unavailable")
    binary_dir = solver_dir / "bin"
    target = binary_dir / "ccx_2.23"
    try:
        binary_dir.mkdir(mode=0o700)
        digest = _copy_verified_ccx_resource(
            source, target, _PACKAGED_CCX_SHA256, expected_size=None, mode=0o500
        )
        if digest == _MACOS_CCX_SHA256:
            library_dir = solver_dir / "lib"
            library_dir.mkdir(mode=0o700)
            for name, (expected_hash, expected_size) in _MACOS_CCX_LIBRARIES.items():
                _copy_verified_ccx_resource(
                    source.parent.parent / "lib" / name,
                    library_dir / name,
                    expected_hash,
                    expected_size=expected_size,
                    mode=0o400,
                )
    except (OSError, AnalysisRefused) as error:
        if isinstance(error, AnalysisRefused):
            raise
        raise AnalysisRefused(f"pinned CalculiX resource cannot be copied: {error}") from error
    return target, digest


def _ccx_owner_argv(parent_fd: int, binary: Path, solver_dir: Path) -> list[str]:
    prefix = (
        [sys.executable, "--internal-ccx-owner"]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "lockwire_pro._ccx_owner"]
    )
    return [*prefix, str(parent_fd), str(binary), str(solver_dir)]


def _run_ccx(binary: Path, solver_dir: Path, *, timeout: float = 120) -> int:
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    process: subprocess.Popen[bytes] | None = None
    parent_fd, owner_fd = os.pipe()

    def cancel(_signum: int, _frame: Any) -> None:
        raise AnalysisRefused("CalculiX job was cancelled")

    try:
        # The separate owner inherits only the read end. Kernel EOF on the
        # write end also reaches it when this analysis process is SIGKILLed.
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        try:
            signal.signal(signal.SIGTERM, cancel)
            signal.signal(signal.SIGINT, cancel)
            process = subprocess.Popen(  # noqa: S603 - fixed internal module and argv
                _ccx_owner_argv(parent_fd, binary, solver_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=(
                    {key: value for key, value in os.environ.items() if not key.startswith("DYLD_")}
                    if sys.platform == "darwin"
                    else None
                ),
                pass_fds=(parent_fd,),
                start_new_session=True,
            )
        except OSError as error:
            raise AnalysisRefused(f"CalculiX could not start: {error}") from error
        finally:
            os.close(parent_fd)
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        try:
            output, errors = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise AnalysisRefused("CalculiX timed out") from error
        if process.returncode != 0:
            raise AnalysisRefused(
                errors.decode(errors="replace").strip() or "CalculiX owner exited"
            )
        try:
            return int(output.strip())
        except ValueError as error:
            raise AnalysisRefused("CalculiX owner returned no exit code") from error
    finally:
        # On normal completion this is inert; on TERM it asks the owner to
        # stop and reap CCX. Block repeat cancellation until the owner is gone.
        cleanup_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        try:
            os.close(owner_fd)
            if process is not None and process.poll() is None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, cleanup_mask)


def _read_node_print(path: Path, *, max_bytes: int | None = None) -> bytes:
    if max_bytes is None:
        max_bytes = _MAX_SOLVER_OUTPUT
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise AnalysisRefused("CalculiX node-print output is not a regular file")
            raw = handle.read(max_bytes + 1)
    except OSError as error:
        raise AnalysisRefused(f"CalculiX node-print output cannot be read: {error}") from error
    if not raw or len(raw) > max_bytes:
        raise AnalysisRefused("CalculiX node-print output is missing or oversized")
    return raw


def _score_direct_b31(
    structural: Any,
    declaration: dict[str, Any],
    admission: _DirectDeckAdmission,
    parsed: dict[tuple[str, str, int], tuple[float, float, float]],
) -> dict[str, Any]:
    """Score only the declared square-cantilever analytic comparison."""
    limits = structural.acceptance_limits
    if not limits:
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "B31 results recorded; no structural acceptance limit is declared",
        }
    if len(limits) != 1 or not isinstance(limits[0], ScalarAgreementLimit):
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "B31 results recorded; acceptance limits are not all supported",
        }
    limit = limits[0]
    if limit.metric != "b31_tip_uy" or limit.reference != "analytic":
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": f"B31 results recorded; unsupported acceptance metric/reference {limit.metric!r}",
        }
    if (
        limit.comparator != "relative"
        or limit.threshold.dimension() != "dimensionless"
        or not math.isfinite(limit.threshold.base())
        or not 0 < limit.threshold.base() <= _MAX_RELATIVE_THRESHOLD
    ):
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "B31 tip-Uy requires a relative threshold above 0 and up to 5 percent",
        }
    assert admission.span_m is not None
    root_set = next(iter({name for name, _ in declaration["declared_boundaries"]}))
    tip_set = next(iter({name for name, _ in declaration["declared_loads"]}))
    tip_rows = [
        values for (kind, name, _), values in parsed.items() if kind == "U" and name == tip_set
    ]
    root_rows = [
        values for (kind, name, _), values in parsed.items() if kind == "RF" and name == root_set
    ]
    if len(tip_rows) != 1 or len(root_rows) != 1:
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "B31 tip-Uy and root-RF node-print outputs are required for scoring",
        }
    force_y = declaration["declared_loads"][(tip_set, 2)]
    youngs_pa = declaration["declared_material"][0]
    inertia_m4 = declaration["declared_side"] ** 4 / 12
    expected_uy = force_y * admission.span_m**3 / (3 * youngs_pa * inertia_m4)
    if not math.isfinite(expected_uy) or expected_uy == 0:
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "B31 analytic tip-Uy is not finite and nonzero",
        }
    tolerance = limit.threshold.base()
    frame = structural.model.source_frame
    displacement_error = abs(tip_rows[0][1] - expected_uy) / abs(expected_uy)
    expected_reaction = (0.0, -force_y, 0.0)
    balance_error = math.dist(root_rows[0], expected_reaction) / abs(force_y)
    comparisons = [
        {
            "metric": "b31_tip_uy",
            "observed": {"value": tip_rows[0][1], "unit": "m", "frame": frame},
            "expected": {"value": expected_uy, "unit": "m", "frame": frame},
            "relative_error": displacement_error,
            "passed": math.isfinite(displacement_error) and displacement_error <= tolerance,
        },
        {
            "metric": "b31_root_force_balance",
            "observed": {"values": list(root_rows[0]), "unit": "N", "frame": frame},
            "expected": {"values": list(expected_reaction), "unit": "N", "frame": frame},
            "relative_error": balance_error,
            "passed": math.isfinite(balance_error) and balance_error <= tolerance,
        },
    ]
    passed = all(row["passed"] for row in comparisons)
    return {
        "simulation_validity": "GO" if passed else "NO_GO",
        "acceptance_limit_id": limit.id,
        "acceptance_provenance": limit.provenance.model_dump(mode="json"),
        "threshold": limit.threshold.model_dump(mode="json"),
        "analytic_operands": {
            "span": {"value": admission.span_m, "unit": "m", "frame": frame},
            "youngs_modulus": {"value": youngs_pa, "unit": "Pa"},
            "second_moment": {"value": inertia_m4, "unit": "m^4"},
            "tip_force_y": {"value": force_y, "unit": "N", "frame": frame},
        },
        "comparisons": comparisons,
        "reason": "declared analytic B31 tip-Uy and root force balance "
        + ("agree" if passed else "disagree")
        + "; physical conformance remains unscored",
    }


def _navier_square_plate_coefficient() -> float:
    """Navier coefficient for center deflection of a simply-supported unit square."""
    total = 0.0
    for m in range(1, 160, 2):
        for n in range(1, 160, 2):
            sign = -1.0 if ((m - 1) // 2 + (n - 1) // 2) % 2 else 1.0
            total += sign / (m * n * (m * m + n * n) ** 2)
    return 16.0 * total / math.pi**6


def _score_direct_plate_s4(
    structural: Any,
    declaration: dict[str, Any],
    admission: _DirectDeckAdmission,
    parsed: dict[tuple[str, str, int], tuple[float, float, float]],
) -> dict[str, Any]:
    """Compare the bound shell center displacement with the Navier plate solution."""
    center_rows = [
        values for (kind, name, _), values in parsed.items() if kind == "U" and name == "CENTER"
    ]
    if len(center_rows) != 1 or admission.span_m is None:
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "one center U3 result is required for the plate reference",
        }
    youngs_pa, poisson_ratio = declaration["declared_material"]
    thickness_m = declaration["declared_thickness"]
    pressure_pa = next(iter(declaration["declared_loads"].values()))
    side_m = admission.span_m
    flexural_rigidity = youngs_pa * thickness_m**3 / (12.0 * (1.0 - poisson_ratio**2))
    coefficient = _navier_square_plate_coefficient()
    expected = coefficient * pressure_pa * side_m**4 / flexural_rigidity
    observed = center_rows[0][2]
    if not math.isfinite(expected) or expected == 0 or not math.isfinite(observed):
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "plate analytic reference and observed center U3 must be finite and nonzero",
        }
    limit = declaration["acceptance_limit"]
    tolerance = limit.threshold.base()
    relative_error = abs(observed - expected) / abs(expected)
    passed = math.isfinite(relative_error) and relative_error <= tolerance
    frame = structural.model.source_frame
    return {
        "simulation_validity": "GO" if passed else "NO_GO",
        "acceptance_limit_id": limit.id,
        "acceptance_provenance": limit.provenance.model_dump(mode="json"),
        "threshold": limit.threshold.model_dump(mode="json"),
        "analytic_operands": {
            "side": {"value": side_m, "unit": "m", "frame": frame},
            "youngs_modulus": {"value": youngs_pa, "unit": "Pa"},
            "poisson_ratio": {"value": poisson_ratio, "unit": "1"},
            "thickness": {"value": thickness_m, "unit": "m"},
            "pressure": {"value": pressure_pa, "unit": "Pa", "frame": frame},
            "flexural_rigidity": {"value": flexural_rigidity, "unit": "N m"},
            "navier_coefficient": {"value": coefficient, "unit": "1"},
        },
        "comparisons": [
            {
                "metric": "plate_center_u3",
                "observed": {"value": observed, "unit": "m", "frame": frame},
                "expected": {"value": expected, "unit": "m", "frame": frame},
                "relative_error": relative_error,
                "passed": passed,
            }
        ],
        "reason": "S4 simply-supported plate center displacement "
        + ("agrees" if passed else "disagrees")
        + " with the Navier reference; physical conformance remains unscored",
    }


def _score_direct_thick_shear(
    structural: Any,
    declaration: dict[str, Any],
    admission: _DirectDeckAdmission,
    parsed: dict[tuple[str, str, int], tuple[float, float, float]],
) -> dict[str, Any]:
    """Score the fixture-bound homogeneous shear displacement and force balance."""
    top = [row for (kind, name, _), row in parsed.items() if kind == "U" and name == "TOP"]
    support = [row for (kind, name, _), row in parsed.items() if kind == "RF" and name == "SUPPORT"]
    support_count = len({key[2] for key in admission.requests if key[:2] == ("RF", "SUPPORT")})
    if not top or len(support) != support_count:
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "complete TOP U and SUPPORT RF output is required",
        }
    youngs_pa, poisson_ratio = declaration["declared_material"]
    shear_modulus = youngs_pa / (2.0 * (1.0 + poisson_ratio))
    shear_stress = 1e6
    expected_u1 = shear_stress / shear_modulus
    observed_u1 = sum(row[0] for row in top) / len(top)
    observed_rf1 = sum(row[0] for row in support)
    expected_rf1 = -shear_stress
    if (
        not all(math.isfinite(value) for value in (expected_u1, observed_u1, observed_rf1))
        or expected_u1 == 0
    ):
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "shear reference and observed results must be finite",
        }
    displacement_error = abs(observed_u1 - expected_u1) / abs(expected_u1)
    reaction_error = abs(observed_rf1 - expected_rf1) / abs(expected_rf1)
    limit = declaration["acceptance_limit"]
    tolerance = limit.threshold.base()
    passed = displacement_error <= tolerance and reaction_error <= tolerance
    frame = structural.model.source_frame
    return {
        "simulation_validity": "GO" if passed else "NO_GO",
        "acceptance_limit_id": limit.id,
        "acceptance_provenance": limit.provenance.model_dump(mode="json"),
        "threshold": limit.threshold.model_dump(mode="json"),
        "analytic_operands": {
            "youngs_modulus": {"value": youngs_pa, "unit": "Pa"},
            "poisson_ratio": {"value": poisson_ratio, "unit": "1"},
            "shear_stress": {"value": shear_stress, "unit": "Pa", "frame": frame},
            "shear_modulus": {"value": shear_modulus, "unit": "Pa"},
            "height": {"value": 1.0, "unit": "m"},
            "area": {"value": 1.0, "unit": "m^2"},
        },
        "comparisons": [
            {
                "metric": "thick_shear_top_u1",
                "observed": {"value": observed_u1, "unit": "m", "frame": frame},
                "expected": {"value": expected_u1, "unit": "m", "frame": frame},
                "relative_error": displacement_error,
                "passed": displacement_error <= tolerance,
            },
            {
                "metric": "thick_shear_support_rf1",
                "observed": {"value": observed_rf1, "unit": "N", "frame": frame},
                "expected": {"value": expected_rf1, "unit": "N", "frame": frame},
                "relative_error": reaction_error,
                "passed": reaction_error <= tolerance,
            },
        ],
        "reason": "byte-pinned homogeneous C3D20 shear displacement and support resultant "
        + ("agree" if passed else "disagree")
        + " with the analytic reference"
        + _FIXTURE_LABEL,
    }


def _score_direct_torsion_c3d20(
    structural: Any,
    declaration: dict[str, Any],
    admission: _DirectDeckAdmission,
    parsed: dict[tuple[str, str, int], tuple[float, float, float]],
) -> dict[str, Any]:
    """Compare the admitted annulus result to Saint-Venant torsion without inventing a model."""
    if admission.nodes is None or admission.span_m is None:
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "torsion deck coordinates and span are required for the analytic reference",
        }
    midspan_rows = [
        values
        for (kind, name, node), values in parsed.items()
        if kind == "U" and name == "MIDSPAN" and node == 11
    ]
    top_rows = [
        (node, values)
        for (kind, name, node), values in parsed.items()
        if kind == "RF" and name == "TOP"
    ]
    support_rows = [
        (node, values)
        for (kind, name, node), values in parsed.items()
        if kind == "RF" and name == "SUPPORT"
    ]
    if len(midspan_rows) != 1 or not top_rows or not support_rows:
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "MIDSPAN U3 and TOP/SUPPORT reaction results are required for torsion scoring",
        }
    youngs_pa, poisson_ratio = declaration["declared_material"]
    twist = declaration["declared_twist"]
    span = admission.span_m
    radii = [math.hypot(y, z) for _, y, z in admission.nodes.values()]
    inner_radius, outer_radius = min(radii), max(radii)
    shear_modulus = youngs_pa / (2.0 * (1.0 + poisson_ratio))
    polar_moment = math.pi * (outer_radius**4 - inner_radius**4) / 2.0
    expected_torque = shear_modulus * polar_moment * twist / span
    _, mid_y, _ = admission.nodes[11]
    expected_u3 = twist * (admission.nodes[11][0] / span) * mid_y
    top_torque = sum(
        admission.nodes[node][1] * values[2] - admission.nodes[node][2] * values[1]
        for node, values in top_rows
    )
    support_torque = sum(
        admission.nodes[node][1] * values[2] - admission.nodes[node][2] * values[1]
        for node, values in support_rows
    )
    observed_u3 = midspan_rows[0][2]
    values = (expected_torque, expected_u3, top_torque, support_torque, observed_u3)
    if (
        any(not math.isfinite(value) for value in values)
        or expected_torque <= 0
        or expected_u3 <= 0
    ):
        return {
            "simulation_validity": "NOT_SCORED",
            "reason": "torsion analytic operands and result values must be finite and nonzero",
        }
    tolerance = declaration["acceptance_limit"].threshold.base()
    errors = {
        "midspan": abs(observed_u3 - expected_u3) / abs(expected_u3),
        "top": abs(top_torque - expected_torque) / abs(expected_torque),
        "support": abs(support_torque + expected_torque) / abs(expected_torque),
    }
    passed = {name: math.isfinite(error) and error <= tolerance for name, error in errors.items()}
    frame = structural.model.source_frame
    comparisons = [
        {
            "metric": "torsion_midspan_u3",
            "observed": {"value": observed_u3, "unit": "m", "frame": frame},
            "expected": {"value": expected_u3, "unit": "m", "frame": frame},
            "relative_error": errors["midspan"],
            "passed": passed["midspan"],
        },
        {
            "metric": "torsion_top_torque_x",
            "observed": {"value": top_torque, "unit": "N m", "frame": frame},
            "expected": {"value": expected_torque, "unit": "N m", "frame": frame},
            "relative_error": errors["top"],
            "passed": passed["top"],
        },
        {
            "metric": "torsion_support_torque_x",
            "observed": {"value": support_torque, "unit": "N m", "frame": frame},
            "expected": {"value": -expected_torque, "unit": "N m", "frame": frame},
            "relative_error": errors["support"],
            "passed": passed["support"],
        },
    ]
    return {
        "simulation_validity": "GO" if all(passed.values()) else "NO_GO",
        "acceptance_limit_id": declaration["acceptance_limit"].id,
        "acceptance_provenance": declaration["acceptance_limit"].provenance.model_dump(mode="json"),
        "threshold": declaration["acceptance_limit"].threshold.model_dump(mode="json"),
        "analytic_operands": {
            "span": {"value": span, "unit": "m", "frame": frame},
            "inner_radius": {"value": inner_radius, "unit": "m", "frame": frame},
            "outer_radius": {"value": outer_radius, "unit": "m", "frame": frame},
            "twist": {"value": twist, "unit": "rad", "frame": frame},
            "youngs_modulus": {"value": youngs_pa, "unit": "Pa"},
            "poisson_ratio": {"value": poisson_ratio, "unit": "1"},
            "shear_modulus": {"value": shear_modulus, "unit": "Pa"},
            "polar_moment": {"value": polar_moment, "unit": "m^4"},
        },
        "comparisons": comparisons,
        "reason": "C3D20 annular torsion results "
        + ("agree" if all(passed.values()) else "disagree")
        + " with the declared Saint-Venant reference; physical conformance remains unscored"
        + _FIXTURE_LABEL,
    }


def execute_direct_truss(
    contract_path: Path,
    *,
    expected_revision: str,
    procedure_id: str,
    evidence_root: Path,
    package_manifest_path: Path,
    work_dir: Path,
) -> dict[str, Any]:
    """Run an admitted direct linear-static deck with byte-bound U/RF results."""
    prepared = prepare_analysis(
        contract_path,
        expected_revision=expected_revision,
        procedure_id=procedure_id,
        evidence_root=evidence_root,
        package_manifest_path=package_manifest_path,
    )
    if prepared["deck_template"] not in _DIRECT_DECK_TEMPLATES:
        return prepared
    model = next(row for row in prepared["artifacts"] if row["role"] == "structural_model")
    try:
        contract_bytes = contract_path.read_bytes()
        if hashlib.sha256(contract_bytes).hexdigest() != expected_revision:
            raise AnalysisRefused("contract revision changed before solver launch")
        contract = load_contract_bytes(contract_bytes)
        assert contract.structural is not None
        artifact = contract.structural.model.model_artifact
        assert artifact is not None
        procedure = next(
            (row for row in contract.structural.procedures if row.id == procedure_id), None
        )
        if procedure is None:
            raise AnalysisRefused("bound structural procedure changed")
        direct_declaration = (
            _declared_direct_truss(contract.structural, procedure)
            if prepared["deck_template"] == "direct-truss-v1"
            else _declared_direct_b31_square(contract.structural, procedure)
            if prepared["deck_template"] == "direct-b31-square-v1"
            else _declared_direct_plate_s4(contract.structural, procedure)
            if prepared["deck_template"] == "direct-plate-s4-v1"
            else _declared_direct_thick_shear(contract.structural, procedure)
            if prepared["deck_template"] == "direct-shear-c3d20-v1"
            else _declared_direct_torsion_c3d20(contract.structural, procedure)
            if prepared["deck_template"] == "direct-torsion-c3d20-v1"
            else _declared_direct_modal_truss(contract.structural, procedure)
            if prepared["deck_template"] == "direct-modal-truss-v1"
            else _declared_two_dof_modal(contract.structural, procedure)
            if prepared["deck_template"] == "direct-two-dof-modal-v1"
            else _declared_two_dof_response(
                contract.structural, procedure, prepared["deck_template"]
            )
            if prepared["deck_template"]
            in {"direct-two-dof-harmonic-v1", "direct-two-dof-transient-v1"}
            else _declared_sliding_contact(contract.structural, procedure)
            if prepared["deck_template"] == "sliding-contact-v1"
            else _declared_frictionless_impact(contract.structural, procedure)
            if prepared["deck_template"] == "frictionless-impact-v1"
            else None
        )
        raw = read_artifact(
            artifact.ref,
            evidence_root,
            max_bytes=MAX_ANALYSIS_ARTIFACT_BYTES,
        )
    except (OSError, ValueError) as error:
        raise AnalysisRefused(f"bound truss deck changed: {error}") from error
    if hashlib.sha256(raw).hexdigest() != model["sha256"]:
        raise AnalysisRefused("bound truss deck changed before solver launch")
    if prepared["deck_template"] == "direct-modal-truss-v1":
        _admit_direct_modal_truss_deck(raw, **direct_declaration)
    elif prepared["deck_template"] == "direct-two-dof-modal-v1":
        _admit_direct_two_dof_modal_deck(raw)
    elif prepared["deck_template"] in {"direct-two-dof-harmonic-v1", "direct-two-dof-transient-v1"}:
        _admit_direct_two_dof_response_deck(raw, prepared["deck_template"])
    elif prepared["deck_template"] == "sliding-contact-v1":
        admission = _admit_sliding_contact_deck(raw, **direct_declaration)
    elif prepared["deck_template"] == "frictionless-impact-v1":
        _admit_frictionless_impact_deck(raw, **direct_declaration)
    else:
        if prepared["deck_template"] not in {
            "direct-truss-v1",
            "direct-b31-square-v1",
            "direct-plate-s4-v1",
            "direct-shear-c3d20-v1",
            "direct-torsion-c3d20-v1",
        }:  # L48: never reach the truss admission without its declaration
            raise AnalysisRefused(f"no admission for deck template {prepared['deck_template']!r}")
        admission = (
            _admit_direct_plate_s4_deck(raw, **direct_declaration)
            if prepared["deck_template"] == "direct-plate-s4-v1"
            else _admit_direct_thick_shear_deck(raw, **direct_declaration)
            if prepared["deck_template"] == "direct-shear-c3d20-v1"
            else _admit_direct_torsion_c3d20_deck(raw, **direct_declaration)
            if prepared["deck_template"] == "direct-torsion-c3d20-v1"
            else _admit_direct_truss_deck(raw, **direct_declaration)
        )
    # The solver receives only a private copy whose bytes have been admitted and pinned.
    solver_dir = work_dir / "solver"
    solver_dir.mkdir()
    binary, binary_hash = _verified_ccx(solver_dir)
    (solver_dir / "model.inp").write_bytes(raw)
    returncode = _run_ccx(binary, solver_dir)
    if returncode != 0:
        raise AnalysisRefused(f"CalculiX exited {returncode}")
    output_bytes = _read_node_print(
        solver_dir / "model.dat",
        max_bytes=MAX_ANALYSIS_ARTIFACT_BYTES
        if prepared["deck_template"] == "frictionless-impact-v1"
        else _MAX_SOLVER_OUTPUT,
    )
    if prepared["deck_template"] in {"direct-modal-truss-v1", "direct-two-dof-modal-v1"}:
        try:
            frequencies = parse_modal_frequencies(output_bytes.decode("utf-8"))
        except UnicodeError as error:
            raise AnalysisRefused("CalculiX modal output is not UTF-8") from error
        eig_bytes = _read_node_print(solver_dir / "model.eig")
        score = (
            _score_two_dof_modal(contract.structural, frequencies)
            if prepared["deck_template"] == "direct-two-dof-modal-v1"
            else {
                "analytic_operands": {},
                "simulation_validity": "NOT_SCORED",
                "reason": "bound modal frequencies recorded; no supported modal acceptance metric is declared",
            }
        )
        return {
            **prepared,
            "kind": "analysis_result",
            "status": "COMPLETED",
            "solver_started": True,
            "solver": {**prepared["solver"], "binary_sha256": binary_hash},
            "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
            "output_text": output_bytes.decode("utf-8"),
            "results": [
                {"kind": "modal_frequency", "mode": mode, "value": frequency, "unit": "Hz"}
                for mode, frequency in frequencies
            ],
            "analytic_operands": {
                **score["analytic_operands"],
                "eigen_artifact_sha256": hashlib.sha256(eig_bytes).hexdigest(),
            },
            **{key: value for key, value in score.items() if key != "analytic_operands"},
        }
    if prepared["deck_template"] in {"direct-two-dof-harmonic-v1", "direct-two-dof-transient-v1"}:
        try:
            output_text = output_bytes.decode("utf-8")
        except UnicodeError as error:
            raise AnalysisRefused("CalculiX dynamic output is not UTF-8") from error
        results, score = _score_two_dof_response(
            contract.structural, prepared["deck_template"], output_text
        )
        return {
            **prepared,
            "kind": "analysis_result",
            "status": "COMPLETED",
            "solver_started": True,
            "solver": {**prepared["solver"], "binary_sha256": binary_hash},
            "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
            "output_text": output_text,
            "results": results,
            **score,
        }
    if prepared["deck_template"] == "sliding-contact-v1":
        try:
            output_text = output_bytes.decode("utf-8")
        except UnicodeError as error:
            raise AnalysisRefused("CalculiX contact output is not UTF-8") from error
        parsed = _parse_sliding_contact_output(output_text, admission)
        score = _score_sliding_contact(contract.structural, admission, parsed)
        return {
            **prepared,
            "kind": "analysis_result",
            "status": "COMPLETED",
            "solver_started": True,
            "solver": {**prepared["solver"], "binary_sha256": binary_hash},
            "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
            "output_text": output_text,
            "results": [
                {
                    "kind": "contact",
                    "contact_rows": int(parsed["contact_rows"]),
                    "normal_force_n": parsed["normal_force_n"],
                    "shear_force_n": parsed["shear_force_n"],
                    "tangential_displacement_m": parsed["tangential_displacement_m"],
                }
            ],
            **score,
        }
    if prepared["deck_template"] == "frictionless-impact-v1":
        try:
            output_text = output_bytes.decode("utf-8")
            stdout = _read_node_print(solver_dir / "stdout.txt").decode("utf-8")
        except UnicodeError as error:
            raise AnalysisRefused("CalculiX impact output is not UTF-8") from error
        parsed = _parse_frictionless_impact_output(
            output_text,
            stdout,
            **{
                "increments": direct_declaration["increments"],
                "time_step_s": direct_declaration["time_step_s"],
            },
        )
        score = _score_frictionless_impact(contract.structural, parsed)
        return {
            **prepared,
            "kind": "analysis_result",
            "status": "COMPLETED",
            "solver_started": True,
            "solver": {**prepared["solver"], "binary_sha256": binary_hash},
            "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
            "output_text": output_text,
            "results": [{"kind": "impact", **parsed}],
            **score,
        }
    try:
        parsed = parse_linear_static_node_print(output_bytes.decode("utf-8"))
    except UnicodeError as error:
        raise AnalysisRefused("CalculiX node-print output is not UTF-8") from error
    if set(parsed) != admission.requests:
        raise AnalysisRefused("CalculiX node-print results differ from requested nodes")
    score = (
        _score_direct_b31(contract.structural, direct_declaration, admission, parsed)
        if prepared["deck_template"] == "direct-b31-square-v1"
        else _score_direct_plate_s4(contract.structural, direct_declaration, admission, parsed)
        if prepared["deck_template"] == "direct-plate-s4-v1"
        else _score_direct_thick_shear(contract.structural, direct_declaration, admission, parsed)
        if prepared["deck_template"] == "direct-shear-c3d20-v1"
        else _score_direct_torsion_c3d20(contract.structural, direct_declaration, admission, parsed)
        if prepared["deck_template"] == "direct-torsion-c3d20-v1"
        else {
            "simulation_validity": "NOT_SCORED",
            "reason": "admitted deck executed; physical conformance awaits independent reference",
        }
    )
    return {
        **prepared,
        "kind": "analysis_result",
        "status": "COMPLETED",
        "solver_started": True,
        "solver": {**prepared["solver"], "binary_sha256": binary_hash},
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "output_text": output_bytes.decode("utf-8"),
        "results": [
            {
                "kind": kind,
                "node_set": node_set,
                "node": node,
                "values": list(values),
                "unit": "m" if kind == "U" else "N",
            }
            for (kind, node_set, node), values in sorted(parsed.items())
        ],
        **score,
    }


def build_analysis_argv(
    contract_path: Path,
    *,
    expected_revision: str,
    procedure_id: str,
    evidence_root: Path,
    package_manifest_path: Path,
    document_path: Path,
) -> list[str]:
    """Build distinct argv elements for the existing job controller; never a shell command."""

    prefix = (
        [sys.executable, "--internal-analysis"]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "lockwire_pro.analysis"]
    )
    return [
        *prefix,
        "--contract",
        str(contract_path),
        "--expected-revision",
        expected_revision,
        "--procedure",
        procedure_id,
        "--evidence-root",
        str(evidence_root),
        "--package-manifest",
        str(package_manifest_path),
        "--document",
        str(document_path),
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lockwire_pro.analysis")
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--procedure", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--package-manifest", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    return parser


def _write_document_atomically(path: Path, document: dict[str, Any]) -> None:
    encoded = (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            if handle.write(encoded) != len(encoded):
                raise OSError("short write while writing analysis document")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with _contract_lock(args.contract.resolve()):
        try:
            document = execute_direct_truss(
                args.contract,
                expected_revision=args.expected_revision,
                procedure_id=args.procedure,
                evidence_root=args.evidence_root,
                package_manifest_path=args.package_manifest,
                work_dir=args.document.parent,
            )
        except AnalysisRefused as error:
            reason = " ".join(str(error).split())[:512]
            print(f"analysis refused: {reason}" if reason else "analysis refused", file=sys.stderr)
            return 2
        _write_document_atomically(args.document, document)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
