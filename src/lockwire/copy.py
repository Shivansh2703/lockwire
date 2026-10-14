"""The one plain-words map: check labels and human part names.

A label is <= 6 everyday words, no snake_case, no ids. Anything the app shows as a row
title or chip comes from here (via gui.py), so the app never parses a contract path.
"""

from __future__ import annotations

import re

# Path segments that only say where a thing lives, never what was checked.
STRUCTURE = frozenset(
    {
        "scene",
        "devices",
        "buses",
        "rails",
        "hosts",
        "budgets",
        "signals",
        "envelopes",
        "links",
        "joints",
        "cross_signal",
        "coverage",
        "stage",
        "plan",
        "preflight",
        "steps",
        "topology",
        "wires",
        "live",
        "physical",
        "timestamps",
        "provenance",
        "actuator_cases",
        "device_models",
        "health",
        "identity",
        "driver",
    }
)

# Matched against the trailing non-structure segments, longest first.
CHECK_LABELS: dict[str, str] = {
    "budgets": "Budget total",
    "variance": "Signal not frozen",
    "range": "Reading in range",
    "nan": "No bad readings",
    "hard_limit": "Within hard limit",
    "rate_hz": "Update rate",
    "recv_gap": "Gaps between messages",
    "enumeration": "Device seen",
    "liveness": "Device alive",
    "liveness/topic": "Topic alive",
    "sustained_rate_liveness": "Stays alive",
    "thermal": "Temperature",
    "rail": "Power rail",
    "stationary_|g|": "IMU at rest",
    "redundant_sensors": "Sensors agree",
    "camera_histogram": "Camera image sane",
    "flow": "Data flowing",
    "cross_record": "Records agree",
    "conditions": "Conditions match",
    "monotonicity/live_path": "Clock moves forward",
    "wall_clock_currency": "Clock is current",
    "recording": "Recording present",
    "record": "Commands recorded",
    "channel": "Commands on channel",
    "in_recording": "Commands in recording",
    "mass": "Mass declared",
    "mount_frame": "Mount frame",
    "mount_xyz": "Mount position",
    "mount_shape": "Mount shape",
    "upstream": "Rail source",
    "voltage": "Rail voltage",
    "current_limit": "Current limit",
    "efficiency": "Converter efficiency",
    "charged_by": "Charged by",
    "ampacity": "Wire current",
    "bandwidth": "Bus load",
    "host": "Host link",
    "bus_ref": "Bus link",
    "structure_hash": "CAD file linked",
    "placement": "Placement",
    "drives_joint": "Joint driven",
    "limit": "Budget limit",
    "cg": "Centre of gravity",
    "effort": "Joint effort",
    "origin/xyz": "Joint position",
    "origin/rpy": "Joint angles",
    "parent": "Joint parent",
    "model": "Device model",
    "owner": "Owner",
    "unclaimed": "Part not declared",
    "scored": "Checks compared",
    "rail_load": "Rail load",
    "rail_load/rail": "Rail load",
    "joint_target/effort": "Joint effort",
    "joint_target": "Joint target",
    "link_budget": "Radio link",
    "serial_number": "Serial number",
    "family": "Device family",
    "evidence": "Test evidence",
    "i2c-sysfs": "I2C bus read",
    "device-tree": "Device tree read",
    "comm_links_under_representative_load": "Comm links under load",
    "live_hardware_software_reconciliation": "Hardware matches software",
    "dds_requested_deadline": "Message deadline",
    "compared_from": "Compared from",
    "stationary_g": "IMU at rest",
}


def check_label(quantity: str) -> str:
    """The plain label of the check behind a contract path, e.g. "Update rate"."""
    parts = quantity.split("/")
    if "commands" in parts[:-1]:  # preflight/commands/<record|channel>/<command id>
        parts = parts[parts.index("commands") + 1 : parts.index("commands") + 2]
    words = [
        p
        for p in parts
        if p not in STRUCTURE and not p.isdigit() and "." not in p and not p.startswith(("{", "<"))
    ]
    for size in (2, 1):
        key = "/".join(words[-size:])
        if len(words) >= size and key in CHECK_LABELS:
            return CHECK_LABELS[key]
    if not words and parts[0] == "budgets":
        # "bud.mass-model" -> "mass": the budget's own word, so two totals read differently.
        kind = re.split(r"[.-]", parts[1].removeprefix("bud."))[0] if len(parts) > 1 else ""
        return f"{CHECK_LABELS['budgets']} \u00b7 {kind}" if kind else CHECK_LABELS["budgets"]
    # Unmapped: still readable, still no underscores (the test lists every known path).
    last = (words[-1] if words else quantity.split("/")[-1]).replace("_", " ").replace("-", " ")
    text = " ".join(last.replace("|", "").split())
    return text[:1].upper() + text[1:] or "Check"


ACRONYMS = {"imu", "esc", "gnss", "gps", "cpu", "can", "uart", "usb", "spi", "hbc"}
_PART_NUMBER = re.compile(r"^[a-z]+-\d+-[a-z]", re.I)  # "icm-42688-p": one part number, keep its hyphens
# ponytail: fixed list of the standard legged-robot convention (ANYmal); extend when another
# convention shows up. Matched only on the upper-case token, as written in the id.
LEG_CODES = {
    "LF": "left front",
    "RF": "right front",
    "LH": "left hind",
    "RH": "right hind",
    "HAA": "hip abduction",
    "HFE": "hip flexion",
    "KFE": "knee",
}


def human_name(identifier: str) -> str:
    """A person-readable part name from a contract id: "dev.adis_imu" -> "Adis IMU".

    Drops the kind prefix, splits snake/kebab case, drops a word repeated as the last word
    ("camera ... camera") and a word that opens the next one ("esc esc1" -> "ESC1"). Short
    codes with a digit and known acronyms are upper-cased ("pm02" -> "PM02"); a long word with
    a digit is split ("computer1" -> "Computer 1"); other words are title-cased.
    """
    for prefix in ("dev.", "rail.", "bus.", "bud.", "host.", "fam.", "mdl."):
        if identifier.startswith(prefix):
            identifier = identifier[len(prefix) :]
    identifier = re.sub(r"(\d)([vV])\b", r"\1 V", identifier)
    words: list[str] = []
    for token in identifier.replace(".", " ").replace("_", " ").split():
        words += [token] if _PART_NUMBER.match(token) else token.split("-")
    words = [x for w in words for x in LEG_CODES.get(w, w).split() if w]
    kept = [
        w
        for i, w in enumerate(words)
        if not (w == words[-1] and i + 1 < len(words)) and not (len(w) >= 3 and i + 1 < len(words) and words[i + 1].startswith(w))
    ]

    def case(w: str) -> str:
        if w.lower() in ACRONYMS:
            return w.upper()
        m = re.match(r"([a-z]{6,})(\d+)$", w, re.I)
        if m:
            return f"{m[1].capitalize()} {m[2]}"
        if len(w) <= 11 and any(c.isdigit() for c in w):
            return w.upper()
        return w.capitalize()

    return " ".join(case(w) for w in kept) or identifier


_QUOTED = re.compile(r"'([^']+)'")


def instead_row(text: str, kind: str) -> dict[str, str]:
    """One short "Instead" row: a verb, the number, and the kind word. The quoted ids in the
    engine's sentence become part names, and "derived-by-<check>" becomes "derived"."""
    m = re.match(r"move the device to rail '([^']+)', which has (.+) free$", text)
    if m:
        short = f"Move to {human_name(m[1])} rail, {m[2]} free"
    else:
        m = re.match(r"add a (.+) regulator fed from rail '([^']+)'$", text)
        if m:
            short = f"Add {m[1]} regulator"
        elif text.startswith("no other rail has headroom"):
            short = "No other rail has room"
        else:
            short = _QUOTED.sub(lambda q: human_name(q[1]), text)
            short = short[:1].upper() + short[1:]
    return {"text": short, "kind": "derived" if kind.startswith("derived-by-") else kind}


# Engine fix lines too long or too technical for the app, by their stable start.
_PLAIN_FIX = (
    ("declare the traffic each driver", "Declare each device's traffic on this bus"),
    ("lock a cited bitrate", "Set this bus's speed from its datasheet"),
    ("lock model provenance", "Re-lock the model file so changes are caught"),
    ("restore the URDF", "Put the model file back, or fix its path"),
    ("fill in the link's inertial", "Add this part's mass in the robot model"),
    (
        "declare the rail's voltage, or accept upstream",
        "Declare the rail's voltage, or skip its voltage checks",
    ),
    (
        "add device power witnesses or remove",
        "Add each device's power use, or remove the empty budget",
    ),
    ("add power draws to devices", "Add each device's power use to see battery time"),
    ("fix the driven-joint reference", "Fix the joint a device drives, or add the missing joint"),
)


def fix_text(action: str) -> str:
    """The engine's fix line in plain words (Finding.action itself stays engineer text)."""
    for start, plain in _PLAIN_FIX:
        if action.startswith(start):
            return plain
    m = re.match(r"shed load on rail '([^']+)'", action)
    if m:
        return f"Use less power on {human_name(m[1])}, or raise its limit"
    if action.startswith("remove mass or re-lock"):
        return "Remove weight, or raise the weight limit"
    text = _QUOTED.sub(lambda q: human_name(q[1]), action.replace(" with provenance", ""))
    return text[:1].upper() + text[1:]


# Row title for N repeats of one kind of finding, keyed by the trailing path shape.
GROUP_TITLES: dict[str, str] = {
    "drives_joint": "{n} drives unmapped",
    "physical/mass": "{n} parts missing mass",
    "placement": "{n} positions not declared",
    "unclaimed": "{n} parts not declared",
    "rate_hz": "{n} update rates off",
    "recv_gap": "{n} devices gap out",
    "enumeration": "{n} devices not seen",
    "liveness": "{n} devices not alive",
}


def group_title(shape: str) -> str:
    """The "{n} ..." row title for a repeated finding, or "" (group only identical titles)."""
    for key, title in GROUP_TITLES.items():
        if shape.endswith(key):
            return title
    return ""


def commit_summary(message: str) -> str:
    """A commit's first line as a sentence: no "docs:" prefix, first letter capital."""
    line = message.strip().splitlines()[0] if message.strip() else ""
    line = re.sub(r"^\w+(\([^)]*\))?!?:\s*", "", line)
    return line[:1].upper() + line[1:]


# Declare list: a value's plain name, from the tail of its contract path (longest tail wins).
_VALUE_NAMES: dict[tuple[str, ...], str] = {
    ("input_voltage_range", "min"): "Lowest voltage",
    ("input_voltage_range", "max"): "Highest voltage",
    ("voltage_range", "min"): "Lowest voltage",
    ("voltage_range", "max"): "Highest voltage",
    ("thermal_limit", "temperature"): "Temperature limit",
    ("radio", "tx_power"): "Radio power",
    ("radio", "rx_sensitivity"): "Radio sensitivity",
    ("radio", "air_rate"): "Radio data rate",
    ("runtime", "max_wall_time"): "Longest run time",
    ("physical", "mass"): "Device mass",
    ("mass",): "Model mass",  # a model link's own mass; the device on the same part says "Device mass"
    ("physical", "mount_xyz"): "Mount position",
    ("power", "draw"): "Power draw",
    ("thermal", "resistance"): "Heat resistance",
    ("thermal", "ambient"): "Surrounding temperature",
    ("tx_antenna", "gain"): "Transmit antenna gain",
    ("rx_antenna", "gain"): "Receive antenna gain",
}


def value_name(parts: tuple[str, ...]) -> str:
    """"input_voltage_range/min" -> "Lowest voltage"; anything unmapped is just capitalised words."""
    for n in (2, 1):
        if (hit := _VALUE_NAMES.get(parts[-n:])) is not None and len(parts) >= n:
            return hit
    if parts[:1] == ("link_budget",) and len(parts) > 1:
        parts = parts[1:]  # "link budget required range" -> "Required range"
    text = " ".join(p.replace("_", " ") for p in parts)
    return text[:1].upper() + text[1:]
