"""Measure the stacked baseline gate against real public PX4 flight logs.

The gate under test is ``lockwire_pro.baseline.check_recording`` — the same composed
path (every ``Mechanism`` family stacked on one recording) that
``scripts/calibrate_fp_budget.py`` and ``scripts/measure_stacked_gate.py``
measure. Those two harnesses draw clean/faulty recordings from the synthetic
AR(1) generator in ``tests/fixtures/bags/generate.py``. This harness measures
the same gate on real captured vehicle data instead: PX4 ``.ulg`` flight logs
published at review.px4.io (the same source ``lockwire preflight`` was written
to consume), so the false-alarm rate and detection power are not an artifact
of the synthetic model.

The measurement has three stages, run as subcommands:

``select``
    Pull the PX4 public log index and pick per-vehicle triples/pairs — see
    ``select_cohorts`` for the cohort design.

``fetch``
    Download each selected log, cut it into small time windows with
    ``derive``, and discard the full log. Disk stays bounded to the windows.

``run``
    Bless a baseline from one flight's window and gate another flight's
    window at the *same* offset, per ``lockwire_pro.baseline.check_recording``.
    Tabulates the false-refusal rate on healthy controls and the discrimination
    rate against human-labelled failures.

Why offsets are matched between the baseline window and the check window: PX4
starts logging at arming, so two windows at the same offset-since-log-start are
roughly the same flight phase (e.g. both early climb, or both mid-cruise).
Blessing a baseline from a takeoff window and then gating a cruise window would
manufacture false alarms that belong to the *comparison*, not to the tool —
the gate would be "detecting" a phase change it was never asked to tolerate.
Matching offsets isolates the thing actually under test: does the same flight
phase, on a different flight, pass; does the labelled-failure flight, at that
same phase, get refused.

Why ``PX4_SITL`` logs are excluded from every cohort: simulation is not real
hardware. This harness exists specifically to get off the synthetic generator;
admitting PX4's own software-in-the-loop simulator back in would defeat that.

The PX4 error-label table (``LABELS`` below) is the numeric ``error_labels``
vocabulary flight_review uses to mark human-reviewed failures. Source:
https://github.com/PX4/flight_review/blob/main/app/plot_app/config_tables.py
    1 Other, 2 Vibration, 3 Airframe-design, 4 Sensor-error,
    5 Component-failure, 6 Software, 7 Human-error, 8 External-conditions
Only a subset (vibration, sensor-error, component-failure) is something a
physical-assumption contract could plausibly speak to; ``TARGET_LABELS``
restricts the "failure" role in a cohort to those.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import struct
import sys
import traceback
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lockwire_pro.baseline import build_baseline, check_recording  # noqa: E402
from lockwire_pro.ulog import _decode, _format_fields, load_ulog  # noqa: E402

from scripts.fetch_px4_logs import INDEX_URL, _entries  # noqa: E402

# ---------------------------------------------------------------------------
# Section 1: ULog window derivation
#
# Reuses lockwire's own format/decoder helpers (``_decode``, ``_format_fields``)
# so field parsing matches what ``lockwire_pro.ulog.load_ulog`` does. This module
# keeps a private byte-offset-tracking record walker (``_records``) because
# ``lockwire_pro.ulog._records`` yields only ``(type, payload)`` — it has no reason
# to know where a message starts and ends in the file, but window derivation
# needs exactly that to copy raw message bytes through unchanged.
# ---------------------------------------------------------------------------

_MAGIC = b"ULog\x01\x12\x35"
_HEADER_SIZE = 16

# Physical sensor topics windows are cut on. Same set as
# tests/fixtures/ulog/derive_fixture.py's TOPICS, plus sensor_mag: that fixture
# derivation only needed accel/baro/combined/gyro for its one committed log,
# but the public cohort spans many airframes and some only carry magnetometer
# data on this list of topics.
PHYS_TOPICS = {"sensor_accel", "sensor_baro", "sensor_combined", "sensor_gyro", "sensor_mag"}
# Topics carrying an explicit armed/disarmed signal, used only to locate the
# first-arm timestamp for reporting; never windowed themselves.
ARM_TOPICS = {"actuator_armed", "vehicle_status"}


def _records(raw: bytes) -> Iterator[tuple[str, bytes, int, int]]:
    """Walk ULog messages, yielding (type, payload, message_start, message_end)."""
    position = _HEADER_SIZE
    n = len(raw)
    while position + 3 <= n:
        size, message_type = struct.unpack_from("<HB", raw, position)
        start = position
        position += 3
        end = position + size
        if end > n:
            break
        yield chr(message_type), raw[position:end], start, end
        position = end


@dataclass(frozen=True, slots=True)
class ArmTimeline:
    """Arming state changes and the physical-sensor span they were read against.

    ``events`` timestamps are seconds since the earliest physical-sensor
    sample, i.e. the same frame ``derive`` windows on — not the ULog header
    (logging-start) time and not vehicle boot time.
    """

    events: tuple[tuple[float, bool], ...]
    span_s: float
    topics: frozenset[str]

    def first_arm_s(self) -> float | None:
        for t, armed in self.events:
            if armed:
                return t
        return None


def survey(raw: bytes) -> ArmTimeline:
    """Read a full ULog file's arming timeline and physical-sensor span."""
    if raw[:7] != b"ULog\x01\x12\x35":
        raise ValueError("not a ULog file")
    formats: dict[str, list] = {}
    subscriptions: dict[int, str] = {}
    arm_raw: list[tuple[int, bool]] = []
    phys_min: int | None = None
    phys_max: int | None = None
    topics_seen: set[str] = set()

    for message_type, payload, _start, _end in _records(raw):
        if message_type == "F":
            try:
                topic, fields = _format_fields(payload.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                continue
            formats[topic] = fields
        elif message_type == "A":
            if len(payload) < 3:
                continue
            message_id = struct.unpack_from("<H", payload, 1)[0]
            topic = payload[3:].decode("utf-8", "replace").rstrip("\0")
            if topic in PHYS_TOPICS or topic in ARM_TOPICS:
                subscriptions[message_id] = topic
                topics_seen.add(topic)
        elif message_type == "D":
            if len(payload) < 2:
                continue
            message_id = struct.unpack_from("<H", payload)[0]
            topic = subscriptions.get(message_id)
            if topic is None:
                continue
            if topic in PHYS_TOPICS:
                if len(payload) < 10:
                    continue
                timestamp = struct.unpack_from("<Q", payload, 2)[0]
                phys_min = timestamp if phys_min is None else min(phys_min, timestamp)
                phys_max = timestamp if phys_max is None else max(phys_max, timestamp)
            else:
                fields = formats.get(topic)
                if not fields:
                    continue
                try:
                    row = _decode(fields, payload[2:])
                except Exception:  # noqa: BLE001
                    continue
                timestamp = row.get("timestamp")
                if timestamp is None:
                    continue
                armed = None
                if topic == "actuator_armed":
                    value = row.get("armed")
                    if value is not None:
                        armed = bool(value)
                else:  # vehicle_status
                    if "armed" in row and row.get("armed") is not None:
                        armed = bool(row["armed"])
                    elif row.get("arming_state") is not None:
                        # PX4 ARMING_STATE_ARMED == 2 across the v1.x line.
                        armed = int(row["arming_state"]) == 2
                if armed is not None:
                    arm_raw.append((int(timestamp), armed))

    if phys_min is None:
        raise ValueError("no physical sensor samples")
    arm_raw.sort()
    events = tuple(((t - phys_min) / 1e6, a) for t, a in arm_raw)
    span_s = (phys_max - phys_min) / 1e6 if phys_max is not None else 0.0
    return ArmTimeline(events=events, span_s=span_s, topics=frozenset(topics_seen))


def derive(raw: bytes, offset_us: int, window_us: int) -> bytes:
    """Cut every physical-sensor subscription to one window, rebased on its own first sample.

    Same windowing rule as tests/fixtures/ulog/derive_fixture.py: each kept
    subscription's elapsed time is measured from *that subscription's* first
    sample, not from the file's logging-start header, so multi-topic logs with
    staggered startup don't get an uneven window. ``PHYS_TOPICS`` here is the
    superset described above (adds sensor_mag).
    """
    out = bytearray(raw[:_HEADER_SIZE])
    subscriptions: dict[int, str] = {}
    first_timestamps: dict[int, int] = {}
    for message_type, payload, start, end in _records(raw):
        keep = message_type in {"B", "F", "I", "M", "P", "Q"}
        if message_type == "A":
            if len(payload) >= 3:
                message_id = struct.unpack_from("<H", payload, 1)[0]
                topic = payload[3:].decode("utf-8", "replace").rstrip("\0")
                if topic in PHYS_TOPICS:
                    subscriptions[message_id] = topic
                    keep = True
        elif message_type == "D":
            if len(payload) >= 10:
                message_id = struct.unpack_from("<H", payload)[0]
                if message_id in subscriptions:
                    timestamp = struct.unpack_from("<Q", payload, 2)[0]
                    first_timestamp = first_timestamps.setdefault(message_id, timestamp)
                    elapsed = timestamp - first_timestamp
                    keep = offset_us <= elapsed <= offset_us + window_us
                else:
                    keep = False
            else:
                keep = False
        if keep:
            out.extend(raw[start:end])
    return bytes(out)


# ---------------------------------------------------------------------------
# Section 2: cohort selection from the PX4 flight-review index
#
# Design: per PHYSICAL VEHICLE (vehicle_uuid) we want a triple
#   baseline  - a healthy log, source of the blessed baseline
#   control   - a DIFFERENT healthy log, same vehicle+hw  -> any NO-GO is a
#               false refusal
#   failure   - a human-labelled failure log, same vehicle+hw -> NO-GO is a
#               candidate catch
# Same baseline, same procedure, same airframe. The only variable is which
# flight — that is what isolates the gate's behaviour from airframe-to-airframe
# noise.
# ---------------------------------------------------------------------------

LABELS = {
    1: "Other",
    2: "Vibration",
    3: "Airframe-design",
    4: "Sensor-error",
    5: "Component-failure",
    6: "Software",
    7: "Human-error",
    8: "External-conditions",
}
# The labels a physical-assumption contract could plausibly speak to.
TARGET_LABELS = {2, 4, 5}


def healthy(row: dict[str, Any]) -> bool:
    """A log with no human error labels and zero logged errors, long enough to matter."""
    return (
        not row.get("error_labels")
        and (row.get("num_logged_errors") or 0) == 0
        and (row.get("duration_s") or 0) >= 60
    )


def usable(row: dict[str, Any]) -> bool:
    """A log worth downloading at all: real hardware, identified vehicle, sane duration."""
    hw = row.get("sys_hw")
    return bool(
        hw
        and hw != "PX4_SITL"  # simulation is not real hardware; excluded on purpose
        and row.get("vehicle_uuid")
        and (row.get("duration_s") or 0) >= 60
        and (row.get("duration_s") or 0) <= 1200
    )


def select_cohorts(
    rows: list[dict[str, Any]],
    *,
    max_triples: int = 40,
    max_healthy_pairs: int = 60,
) -> dict[str, list[dict[str, Any]]]:
    """Build the (baseline, control, failure) triples and extra healthy pairs."""
    rows = [r for r in rows if usable(r)]
    by_vehicle: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        by_vehicle[(row["vehicle_uuid"], row["sys_hw"])].append(row)

    triples = []
    for key, logs in by_vehicle.items():
        good = [r for r in logs if healthy(r)]
        bad = [r for r in logs if set(r.get("error_labels") or ()) & TARGET_LABELS]
        if len(good) >= 2 and bad:
            good.sort(key=lambda r: r["log_date"])
            for b in bad:
                triples.append(
                    {
                        "vehicle_uuid": key[0],
                        "sys_hw": key[1],
                        "airframe": b.get("airframe_name"),
                        "baseline": good[0]["log_id"],
                        "control": good[1]["log_id"],
                        "failure": b["log_id"],
                        "failure_labels": b["error_labels"],
                        "failure_label_names": [LABELS.get(x, str(x)) for x in b["error_labels"]],
                        "meta": {"baseline": good[0], "control": good[1], "failure": b},
                    }
                )

    # Prefer sensor-error, then component-failure, then vibration; cap the set.
    def rank(t: dict[str, Any]) -> tuple[int, int]:
        s = set(t["failure_labels"])
        return (0 if 4 in s else 1 if 5 in s else 2, len(s))

    triples.sort(key=rank)
    # De-duplicate: at most one triple per physical vehicle.
    seen: set[str] = set()
    picked = []
    for t in triples:
        if t["vehicle_uuid"] in seen:
            continue
        seen.add(t["vehicle_uuid"])
        picked.append(t)
        if len(picked) >= max_triples:
            break

    # Extra pure-healthy pairs (baseline + control, no failure) to grow the
    # false-refusal sample beyond the triples above.
    extra = []
    for key, logs in by_vehicle.items():
        if key[0] in seen:
            continue
        good = [r for r in logs if healthy(r)]
        if len(good) >= 2:
            good.sort(key=lambda r: r["log_date"])
            extra.append(
                {
                    "vehicle_uuid": key[0],
                    "sys_hw": key[1],
                    "airframe": good[0].get("airframe_name"),
                    "baseline": good[0]["log_id"],
                    "control": good[1]["log_id"],
                    "meta": {"baseline": good[0], "control": good[1]},
                }
            )
    # Spread across distinct hardware so the result is not one board's quirk.
    extra.sort(key=lambda t: (t["sys_hw"], t["vehicle_uuid"]))
    by_hw: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for t in extra:
        by_hw[t["sys_hw"]].append(t)
    healthy_pairs = []
    round_i = 0
    while len(healthy_pairs) < max_healthy_pairs and round_i < 50:
        for hw in sorted(by_hw):
            if round_i < len(by_hw[hw]) and len(healthy_pairs) < max_healthy_pairs:
                healthy_pairs.append(by_hw[hw][round_i])
        round_i += 1

    return {"triples": picked, "healthy_pairs": healthy_pairs}


def _fetch_index(index_url: str) -> list[dict[str, Any]]:
    """Stream the PX4 public log index (reuses fetch_px4_logs's chunked JSON decoder)."""
    request = urllib.request.Request(index_url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request) as response:  # noqa: S310
        return list(_entries(response))


def cmd_select(args: argparse.Namespace) -> int:
    if args.index_file:
        rows = json.load(gzip.open(args.index_file))
    else:
        rows = _fetch_index(args.index_url)
    cohorts = select_cohorts(
        rows, max_triples=args.max_triples, max_healthy_pairs=args.max_healthy_pairs
    )
    args.output.write_text(json.dumps(cohorts, indent=1))
    lab = Counter()
    for t in cohorts["triples"]:
        for name in t["failure_label_names"]:
            lab[name] += 1
    n_logs = len(cohorts["triples"]) * 3 + len(cohorts["healthy_pairs"]) * 2
    print(f"triples: {len(cohorts['triples'])}   healthy_pairs: {len(cohorts['healthy_pairs'])}")
    print("failure labels in triples:", lab.most_common())
    print("logs to download:", n_logs)
    print(f"wrote {args.output}")
    return 0


# ---------------------------------------------------------------------------
# Section 3: fetch + derive
#
# Disk stays bounded: only the windowed derivations survive; the full
# downloaded log is deleted as soon as its windows are cut.
# ---------------------------------------------------------------------------

CDN = "https://cdn.logs.px4.io/{}.ulg"
DEFAULT_WINDOW_S = 20
DEFAULT_MAX_WINDOWS = 5


def _download(log_id: str, *, cdn: str, timeout_s: float) -> bytes | None:
    try:
        with urllib.request.urlopen(cdn.format(log_id), timeout=timeout_s) as response:  # noqa: S310
            raw = response.read()
    except Exception:  # noqa: BLE001
        return None
    if raw[:7] != _MAGIC[:7]:
        return None
    return raw


def process_log(
    log_id: str,
    n_windows: int,
    *,
    work_dir: Path,
    window_s: int = DEFAULT_WINDOW_S,
    cdn: str = CDN,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """Download one log, cut it into windows, discard the full log. Idempotent."""
    out = work_dir / log_id
    done = out / "meta.json"
    if done.exists():
        return json.loads(done.read_text())
    record: dict[str, Any] = {"log_id": log_id, "ok": False}
    try:
        raw = _download(log_id, cdn=cdn, timeout_s=timeout_s)
        if raw is None:
            record["error"] = "download or magic failed"
            return record
        record["full_bytes"] = len(raw)
        timeline = survey(raw)
        arm_t = timeline.first_arm_s()
        record.update(
            span_s=round(timeline.span_s, 2),
            topics=sorted(timeline.topics),
            first_arm_s=arm_t,
            n_arm_events=len(timeline.events),
        )
        out.mkdir(parents=True, exist_ok=True)
        windows = []
        for i in range(n_windows):
            offset = i * window_s
            if offset + window_s > timeline.span_s:
                break
            data = derive(raw, offset * 1_000_000, window_s * 1_000_000)
            path = out / f"w{offset:03d}.ulg"
            path.write_bytes(data)
            windows.append(
                {
                    "offset_s": offset,
                    "path": str(path),
                    "bytes": len(data),
                    "armed": None if arm_t is None else bool(offset + window_s > arm_t),
                }
            )
        record["windows"] = windows
        record["ok"] = bool(windows)
        if not windows:
            record["error"] = f"span {timeline.span_s:.1f}s too short"
    except Exception as error:  # noqa: BLE001
        record["error"] = f"{type(error).__name__}: {error}"
        record["trace"] = traceback.format_exc()[-600:]
    if record.get("ok"):
        done.write_text(json.dumps(record, indent=1))
    return record


def _fetch_jobs(cohorts: dict[str, Any], *, max_windows: int) -> dict[str, int]:
    """log_id -> number of windows needed (baseline logs only need one window)."""
    jobs: dict[str, int] = {}
    for t in cohorts.get("triples", []):
        jobs[t["baseline"]] = max(jobs.get(t["baseline"], 0), 1)
        jobs[t["control"]] = max_windows
        jobs[t["failure"]] = max_windows
    for p in cohorts.get("healthy_pairs", []):
        jobs[p["baseline"]] = max(jobs.get(p["baseline"], 0), 1)
        jobs[p["control"]] = max_windows
    return jobs


def cmd_fetch(args: argparse.Namespace) -> int:
    cohorts = json.loads(args.cohorts.read_text())
    jobs = _fetch_jobs(cohorts, max_windows=args.max_windows)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    print(f"{len(jobs)} logs to fetch", flush=True)
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_log,
                log_id,
                n,
                work_dir=args.work_dir,
                window_s=args.window_seconds,
            ): log_id
            for log_id, n in jobs.items()
        }
        for i, future in enumerate(list(futures), 1):
            log_id = futures[future]
            try:
                results[log_id] = future.result()
            except Exception as error:  # noqa: BLE001
                results[log_id] = {"log_id": log_id, "ok": False, "error": str(error)}
            if i % 20 == 0:
                ok = sum(1 for r in results.values() if r.get("ok"))
                print(f"  {i}/{len(jobs)} done, {ok} ok", flush=True)

    args.output.write_text(json.dumps(results, indent=1))
    ok = sum(1 for r in results.values() if r.get("ok"))
    print(f"DONE: {ok}/{len(jobs)} ok", flush=True)
    for r in results.values():
        if not r.get("ok"):
            print("  FAIL", r["log_id"], r.get("error"), flush=True)
    return 0


# ---------------------------------------------------------------------------
# Section 4: the controlled experiment
#
# Per physical vehicle (same vehicle_uuid, same sys_hw), and per matched
# offset K:
#   bless a baseline from log A's window at offset K
#   gate log B's window at offset K   -> healthy control; any finding = FALSE
#                                         REFUSAL
#   gate log F's window at offset K   -> labelled failure; any finding =
#                                         CANDIDATE CATCH
# ---------------------------------------------------------------------------

CONTEXT = "nominal-op"


def _windows_by_offset(work_dir: Path, log_id: str) -> dict[int, dict[str, Any]]:
    meta_path = work_dir / log_id / "meta.json"
    if not meta_path.exists():
        return {}
    meta = json.loads(meta_path.read_text())
    if not meta.get("windows"):
        return {}
    return {w["offset_s"]: w for w in meta["windows"]}


def bless_at(log_id: str, window: dict[str, Any], *, context: str = CONTEXT):
    try:
        recording = load_ulog(Path(window["path"]), context=context)
        artifact = build_baseline(
            recording,
            context=context,
            known_good_rationale=(
                f"PX4 public log {log_id} window +{window['offset_s']}s: "
                "no human error labels, zero logged errors."
            ),
        )
        return artifact, None
    except Exception as error:  # noqa: BLE001
        return None, f"{type(error).__name__}: {error}"


def gate_at(artifact, window: dict[str, Any], *, context: str = CONTEXT) -> dict[str, Any]:
    row: dict[str, Any] = {"offset_s": window["offset_s"], "armed": window.get("armed")}
    try:
        recording = load_ulog(Path(window["path"]), context=context)
        findings = check_recording(recording, artifact)
        row["status"] = "NO-GO" if findings else "GO"
        row["findings"] = [
            {"mechanism": f.mechanism, "channels": list(f.channels), "detail": f.detail}
            for f in findings
        ]
    except KeyError as error:
        row["status"] = "not-comparable"
        row["error"] = f"baseline has no profile for {error}"
    except Exception as error:  # noqa: BLE001
        row["status"] = "error"
        row["error"] = f"{type(error).__name__}: {error}"
        row["trace"] = traceback.format_exc()[-300:]
    return row


def aggregate_verdict(statuses: list[str]) -> str:
    """Roll up one log's per-window trial statuses into a single verdict.

    A log is refused ("NO-GO") if ANY of its windows is refused — a preflight
    gate that only fires on some windows still means the flight doesn't pass.
    Otherwise it is "GO" only if at least one window was actually scored; a
    log whose windows are all "not-comparable" or "error" was never gated at
    all, so it must not read as a clean pass.
    """
    if "NO-GO" in statuses:
        return "NO-GO"
    if "GO" in statuses:
        return "GO"
    return "not-comparable"


def run_pair(
    work_dir: Path, baseline_id: str, other_ids: dict[str, str], *, context: str = CONTEXT
) -> dict[str, list[dict[str, Any]]]:
    """other_ids maps role -> log_id. Returns {role: [trial rows]}."""
    baseline_windows = _windows_by_offset(work_dir, baseline_id)
    out: dict[str, list[dict[str, Any]]] = {role: [] for role in other_ids}
    others = {role: _windows_by_offset(work_dir, lid) for role, lid in other_ids.items()}
    for offset, baseline_window in sorted(baseline_windows.items()):
        if not any(offset in windows for windows in others.values()):
            continue
        artifact, error = bless_at(baseline_id, baseline_window, context=context)
        if artifact is None:
            for role in others:
                out[role].append({"offset_s": offset, "status": "baseline-error", "error": error})
            continue
        for role, windows in others.items():
            if offset in windows:
                out[role].append(gate_at(artifact, windows[offset], context=context))
    return out


def run_experiment(cohorts: dict[str, Any], work_dir: Path) -> dict[str, list[dict[str, Any]]]:
    results: dict[str, list[dict[str, Any]]] = {"triples": [], "healthy_pairs": []}

    for t in cohorts.get("triples", []):
        entry = {k: t.get(k) for k in ("vehicle_uuid", "sys_hw", "airframe", "failure_label_names")}
        entry["baseline_log"] = t["baseline"]
        entry["control_log"] = t["control"]
        entry["failure_log"] = t["failure"]
        entry.update(
            run_pair(work_dir, t["baseline"], {"control": t["control"], "failure": t["failure"]})
        )
        results["triples"].append(entry)

    for p in cohorts.get("healthy_pairs", []):
        entry = {k: p.get(k) for k in ("vehicle_uuid", "sys_hw", "airframe")}
        entry["baseline_log"] = p["baseline"]
        entry["control_log"] = p["control"]
        entry.update(run_pair(work_dir, p["baseline"], {"control": p["control"]}))
        results["healthy_pairs"].append(entry)

    return results


def _trials(entries: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    return [r for e in entries for r in e.get(role, [])]


def _rate(rows: list[dict[str, Any]], label: str) -> tuple[int, int]:
    scored = [r for r in rows if r["status"] in ("GO", "NO-GO")]
    nogo = sum(1 for r in scored if r["status"] == "NO-GO")
    other = Counter(r["status"] for r in rows if r["status"] not in ("GO", "NO-GO"))
    if scored:
        print(f"  {label}: {nogo}/{len(scored)} refused = {100 * nogo / len(scored):.1f}%")
    else:
        print(f"  {label}: no scored trials")
    if other:
        print(f"    (unscored: {dict(other)})")
    return nogo, len(scored)


def summarize(results: dict[str, list[dict[str, Any]]]) -> None:
    triples, healthy_pairs = results["triples"], results["healthy_pairs"]

    print("\n=== FALSE REFUSAL (healthy control flights, matched offset) ===")
    ctrl = _trials(triples, "control") + _trials(healthy_pairs, "control")
    _rate(ctrl, "per-trial")
    per_log = [
        aggregate_verdict([r["status"] for r in e.get("control", [])])
        for e in triples + healthy_pairs
    ]
    per_log = [v for v in per_log if v in ("GO", "NO-GO")]
    if per_log:
        n = per_log.count("NO-GO")
        print(
            f"  per-log (refused if ANY window refused): {n}/{len(per_log)} "
            f"= {100 * n / len(per_log):.1f}%"
        )
    mech = Counter(f["mechanism"] for r in ctrl for f in r.get("findings", []))
    print("  mechanisms firing on healthy flights:", mech.most_common())

    print("\n=== LABELLED SENSOR-ERROR FLIGHTS (matched offset) ===")
    fail = _trials(triples, "failure")
    _rate(fail, "per-trial")
    mech = Counter(f["mechanism"] for r in fail for f in r.get("findings", []))
    print("  mechanisms firing on failure flights:", mech.most_common())

    print("\n=== DISCRIMINATION, PAIRED BY VEHICLE AND OFFSET ===")
    # The only comparison that isolates the flight: same baseline, same offset,
    # healthy control vs labelled-failure flight of the SAME physical vehicle.
    cells: Counter = Counter()
    discriminations = []
    for e in triples:
        c = {r["offset_s"]: r for r in e.get("control", [])}
        f = {r["offset_s"]: r for r in e.get("failure", [])}
        for k in sorted(set(c) & set(f)):
            a, b = c[k]["status"], f[k]["status"]
            if a in ("GO", "NO-GO") and b in ("GO", "NO-GO"):
                cells[(a, b)] += 1
                if a == "GO" and b == "NO-GO":
                    discriminations.append((e, k, f[k]))
    total = sum(cells.values())
    print(f"  paired trials: {total}")
    for kk in sorted(cells):
        print(f"    healthy={kk[0]:6s} failure={kk[1]:6s}  n={cells[kk]}")
    if total:
        agree_bad = cells[("NO-GO", "NO-GO")]
        print(
            f"  DISCRIMINATIONS (healthy GO, failure NO-GO): {len(discriminations)}/{total} "
            f"= {100 * len(discriminations) / total:.1f}%"
        )
        print(f"  refused BOTH (no discrimination): {agree_bad}/{total}")
    seen: set[str] = set()
    for e, k, row in discriminations:
        if e["failure_log"] in seen:
            continue
        seen.add(e["failure_log"])
        print(
            f"    {e['sys_hw']:22s} {str(e['airframe'])[:22]:22s} +{k:3d}s "
            f"{e['failure_log'][:8]} {[f['mechanism'] for f in row['findings']]}"
        )
    print(f"  distinct failure logs cleanly discriminated: {len(seen)}")

    print("\n=== CHANNEL-LEVEL: which channels fire, healthy vs failure ===")
    ch_healthy = Counter(c for r in ctrl for f in r.get("findings", []) for c in f["channels"])
    ch_failure = Counter(c for r in fail for f in r.get("findings", []) for c in f["channels"])
    print("  top healthy-flight channels:", ch_healthy.most_common(6))
    print("  top failure-flight channels:", ch_failure.most_common(6))


def cmd_run(args: argparse.Namespace) -> int:
    cohorts = json.loads(args.cohorts.read_text())
    results = run_experiment(cohorts, args.work_dir)
    args.output.write_text(json.dumps(results, indent=1))
    summarize(results)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    select = subparsers.add_parser("select", help="pick cohorts from the PX4 public index")
    select.add_argument(
        "--index-url", default=INDEX_URL, help="PX4 public log index URL (default: %(default)s)"
    )
    select.add_argument(
        "--index-file",
        type=Path,
        default=None,
        help="use a local gzip JSON index instead of fetching --index-url",
    )
    select.add_argument("--max-triples", type=int, default=40)
    select.add_argument("--max-healthy-pairs", type=int, default=60)
    select.add_argument("--output", type=Path, default=Path("cohorts.json"))
    select.set_defaults(func=cmd_select)

    fetch = subparsers.add_parser("fetch", help="download+derive windows for selected cohorts")
    fetch.add_argument("--cohorts", type=Path, default=Path("cohorts.json"))
    fetch.add_argument("--work-dir", type=Path, default=Path("work"))
    fetch.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_S)
    fetch.add_argument("--max-windows", type=int, default=DEFAULT_MAX_WINDOWS)
    fetch.add_argument("--workers", type=int, default=4)
    fetch.add_argument("--output", type=Path, default=Path("fetch_results.json"))
    fetch.set_defaults(func=cmd_fetch)

    run = subparsers.add_parser("run", help="bless+gate the stacked baseline gate per cohort")
    run.add_argument("--cohorts", type=Path, default=Path("cohorts.json"))
    run.add_argument("--work-dir", type=Path, default=Path("work"))
    run.add_argument("--output", type=Path, default=Path("experiment.json"))
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
