"""The one plain-words map: check labels and human part names.

A label is <= 6 everyday words, no snake_case, no ids. Anything the app shows as a row
title or chip comes from here (via gui.py), so the app never parses a contract path.
"""

from __future__ import annotations

import math
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
    "discharge_temp_c": "Pack temperature window",
    "charge_temp_c": "Pack charging temperature window",
    "sanity": "Model sanity checks",
    "battery/chemistry": "Battery chemistry",
    "internal_resistance_mohm": "Pack internal resistance",
    "capacitance_f": "Rail capacitance",
    "output_tolerance_pct": "Rail output tolerance",
    "estop_category": "Emergency stop category",
    "mass_vs_size": "Mass for its size",
    "mjcf": "MuJoCo model",
    "tvs": "Surge protection (TVS)",
    "c_rate_continuous": "Continuous discharge rate",
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
    "com": "CoM",  # centre of mass, never "Com"
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
    "structure_hash": "Model file vs the lock",
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
    "rail_load/draw": "Rail load",
    "set_power_mode/draw": "Power draw",
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
        kind, _, what = (parts[1].removeprefix("bud.") if len(parts) > 1 else "").partition(".")
        kind = re.split(r"-", kind)[0]
        if kind and what:  # "bud.power.servo-5v" -> "Servo 5 V power budget": it names the budget
            return f"{label_name(what)} {kind} budget"
        return f"Total {kind} budget" if kind else CHECK_LABELS["budgets"]
    if not words and parts[:2] == ["topology", "wires"]:  # the wire's own id is the subject, not the label
        return "Wire check"
    # Unmapped: still readable, still no underscores (the test lists every known path).
    last = (words[-1] if words else quantity.split("/")[-1]).replace("_", " ").replace("-", " ")
    # a field key's unit suffix ("capacitance_f", "temp_c") is no word: never shown as one
    last = re.sub(r"(?<=\w) (c|f|pct|mohm|ohm|hz|s|v|a|w|mm|kg)$", "", last)
    text = " ".join(last.replace("|", "").split())
    return text[:1].upper() + text[1:] or "Check"


ACRONYMS = {"imu", "esc", "gnss", "gps", "cpu", "can", "uart", "usb", "spi", "i2c", "hbc", "nx"}
_MIXED_CASE = {
    "dronecan": "DroneCAN",
    "opencr": "OpenCR",
    "here3": "Here3",
    "anymal": "ANYmal",
    "so101": "SO-101",
    # maker and model spellings the shipped examples use
    "pm02": "PM02",
    "esp32": "ESP32",
    "pi5": "Pi 5",  # the lockfile family name: "Raspberry Pi 5"
    "pixhawk": "Pixhawk",
    "6c": "6C",
    "rfd900x": "RFD900x",
    "ist8310": "IST8310",
    "mg996r": "MG996R",
    "sts3215": "STS3215",
    "st3215": "ST3215",
    "xl330": "XL330",
    "2d": "2D",
    "telem1": "TELEM1",
    "jetson": "Jetson",
    "orin": "Orin",
    "nano": "Nano",
    "devkitc": "DevKitC",
    "wroom": "WROOM",
}
_PART_NUMBER = re.compile(
    r"^[a-z]+-\d+-[a-z]", re.I
)  # "icm-42688-p": one part number, keep its hyphens
# ponytail: fixed list of the standard legged-robot convention (ANYmal); extend when another
# convention shows up. Matched only on the upper-case token, as written in the id.
LEG_CODES = {
    "LF": "left front",
    "RF": "right front",
    "LH": "left hind",
    "RH": "right hind",
    "FL": "front left",
    "FR": "front right",
    "HL": "hind left",
    "HR": "hind right",
    "HAA": "hip abduction",
    "HFE": "hip flexion",
    "KFE": "knee",
}


def human_name(identifier: str) -> str:
    """A person-readable part name from a contract id: "dev.adis_imu" -> "adis IMU".

    Drops the kind prefix, splits snake/kebab case, drops a word repeated as the last word
    ("camera ... camera") and a word that opens the next one ("esc esc1" -> "esc1"). Words
    keep the id's own spelling ("dev.pi5" -> "pi5", never "PI5"): only a known acronym
    ("imu" -> "IMU") or a maker's own spelling ("opencr" -> "OpenCR") is re-cased.
    """
    for prefix in ("dev.", "rail.", "bus.", "bud.", "host.", "fam.", "mdl.", "w."):
        if identifier.startswith(prefix):
            identifier = identifier[len(prefix) :]
    identifier = re.sub(r"(\d)([vV])\b", r"\1 V", identifier)
    # "opencr-1-0" is version 1.0: a lone digit pair after a word; "~" keeps the dot through the split.
    identifier = re.sub(r"(?<=[A-Za-z])[-_ ](\d)[-_ ](\d)$", r" \1~\2", identifier)
    words: list[str] = []
    for token in identifier.replace(".", " ").replace("_", " ").split():
        words += [token] if _PART_NUMBER.match(token) else token.split("-")
    words = [x for w in words for x in LEG_CODES.get(w, w).split() if w]
    # Filler words of CAD/URDF naming: "freejoint", a trailing "link"/"BOX0", and a model tag
    # ("so101 v1") that sits before that trailing word. Never empties the name.
    words = [w for w in words if w.lower() != "freejoint"] or words
    tail = len(words) > 1 and (
        words[-1].lower() in ("link",)
        or re.fullmatch(r"(box|sphere|cylinder|mesh|geom)\d+", words[-1], re.I)
    )
    if tail:
        words.pop()
        if len(words) > 2 and re.fullmatch(r"v\d+", words[-1], re.I):
            del words[-2:]
    kept = [
        w
        for i, w in enumerate(words)
        if not (w == words[-1] and i + 1 < len(words))
        and not (len(w) >= 3 and i + 1 < len(words) and words[i + 1].startswith(w))
    ]

    def case(w: str) -> str:
        if w.lower() in ACRONYMS:
            return w.upper()
        if w.isalpha() and w.isupper() and len(w) > 1:  # a shouted word ("UPPER_LEG") reads lower
            return w.lower()
        return _MIXED_CASE.get(w.lower(), w)  # as written: "15W" and "pi5" keep their case

    return " ".join(case(w) for w in kept).replace("~", ".") or identifier


def label_name(identifier: str) -> str:
    """``human_name`` where it stands alone as a title, heading, label or table cell: only the
    first letter is capitalised ("dev.servo1" -> "Servo1"); a name inside a sentence uses human_name."""
    name = human_name(identifier)
    return name[:1].upper() + name[1:]


def robot_title(name: str) -> str:
    """The robot's name as the app titles it: "anymal" -> "ANYmal", "my arm" -> "My arm"."""
    words = [_MIXED_CASE.get(w.lower(), w) for w in name.split()]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


_QUOTED = re.compile(r"'([^']+)'")


def instead_row(text: str, kind: str) -> dict[str, str]:
    """One short "Instead" row: a verb, the number, and the kind word. The quoted ids in the
    engine's sentence become part names, and "derived-by-<check>" becomes "derived"."""
    m = re.match(r"move '([^']+)' to rail '([^']+)' \((.+) free\); (.+)$", text)
    if m:
        w = re.match(
            r"[\d.]+", m[3]
        )  # whole watts; the trailing "clears the overload" clause is dropped
        x = float(w[0]) if w else 0.0  # headroom is floored, never rounded up
        free = (
            (f"{math.floor(x)} W" if x >= 10 else f"{math.floor(x * 10) / 10:g} W") if w else m[3]
        )
        short = f"Move {' '.join(human_name(m[1]).split()[:2])} to {re.sub(r' motor$', '', human_name(m[2]))} rail · {free} free"
    else:
        m = re.match(r"add a (.+) regulator fed from rail '([^']+)'$", text)
        if m:
            short = f"Add {math.ceil(round(float(m[1].split()[0]) * 10, 6)) / 10:g} W regulator"  # capacity rounds up
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
    ("restore the model file", "Put the model file back, or fix its path"),
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
    text = re.sub(r"\bre-lock(?: the model)?\b", "lock it again", text)
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


def group_title(shape: str, *, weak: bool = False) -> str:
    """The "{n} ..." row title for a repeated finding, or "" (group only identical titles).

    `weak`: a drives_joint finding from the torque check (servo too weak for the joint's limit),
    which shares its shape with the unmapped-joint reference check."""
    if weak:
        return "{n} servos can't reach their joint limit"
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
    # a link's (model) and a device's mass tolerance sit in one group for a part that is both
    ("mass_tolerance", "abs"): "Model mass tolerance (absolute)",
    ("mass_tolerance", "rel_pct"): "Model mass tolerance (percent)",
    ("mass_tolerance", "min"): "Lowest model mass",
    ("mass_tolerance", "typ"): "Typical model mass",
    ("mass_tolerance", "max"): "Highest model mass",
    ("physical", "mass_tolerance", "abs"): "Device mass tolerance (absolute)",
    ("physical", "mass_tolerance", "rel_pct"): "Device mass tolerance (percent)",
    ("physical", "mass_tolerance", "min"): "Lowest device mass",
    ("physical", "mass_tolerance", "typ"): "Typical device mass",
    ("physical", "mass_tolerance", "max"): "Highest device mass",
    ("input_voltage_range", "max"): "Highest voltage",
    ("voltage_range", "min"): "Lowest voltage",
    ("voltage_range", "max"): "Highest voltage",
    ("thermal_limit", "temperature"): "Temperature limit",
    ("radio", "tx_power"): "Radio power",
    ("radio", "rx_sensitivity"): "Radio sensitivity",
    ("radio", "air_rate"): "Radio data rate",
    ("runtime", "max_wall_time"): "Longest run time",
    ("physical", "mass"): "Device mass",
    (
        "mass",
    ): "Model mass",  # a model link's own mass; the device on the same part says "Device mass"
    ("physical", "mount_xyz"): "Mount position",
    ("power", "draw"): "Power draw",
    ("thermal", "resistance"): "Heat resistance",
    ("thermal", "ambient"): "Surrounding temperature",
    ("tx_antenna", "gain"): "Transmit antenna gain",
    ("rx_antenna", "gain"): "Receive antenna gain",
}


def value_name(parts: tuple[str, ...]) -> str:
    """ "input_voltage_range/min" -> "Lowest voltage"; anything unmapped is just capitalised words."""
    for n in (3, 2, 1):
        if (hit := _VALUE_NAMES.get(parts[-n:])) is not None and len(parts) >= n:
            return hit
    if parts[:1] == ("link_budget",) and len(parts) > 1:
        parts = parts[1:]  # "link budget required range" -> "Required range"
    text = " ".join(p.replace("_", " ") for p in parts)
    return text[:1].upper() + text[1:]
