import difflib
import hashlib
import json
import struct
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from lockwire_pro import cli_pro, ulog
from lockwire.cli import app

from conftest import plain

FIXTURES = Path(__file__).parent / "fixtures"
SMALL_ULOG = FIXTURES / "ulog" / "1b8e43d5-04d8-4130-8227-628898f319c5" / "baseline.ulg"
PINNED_SHA256 = "eedf6ef480a3b7e75c32a19da3a1fc74bf56c5b1b7e4d289b395c74e1e225880"  # (28 scored) + (21 not scored) + not-compared list; no milestone code in the coverage lines


def _ulog_bytes(topics: dict[str, tuple[str, ...]], rows: list[tuple[str, bytes]]) -> bytes:
    """A minimal synthetic ULog: a header, one 'F' format record per topic, one
    'A' subscription record per topic (message_id = its position in `topics`),
    then the given (topic, encoded_field_bytes) 'D' data rows, in order."""
    body = b""
    message_ids = {name: index + 1 for index, name in enumerate(topics)}
    for name, fields in topics.items():
        description = (f"{name}:" + ";".join(fields)).encode()
        body += struct.pack("<HB", len(description), ord("F")) + description
    for name, message_id in message_ids.items():
        payload = struct.pack("<BH", 0, message_id) + name.encode()
        body += struct.pack("<HB", len(payload), ord("A")) + payload
    for name, encoded in rows:
        payload = struct.pack("<H", message_ids[name]) + encoded
        body += struct.pack("<HB", len(payload), ord("D")) + payload
    return ulog._MAGIC + b"\x00" * (ulog._HEADER_SIZE - len(ulog._MAGIC)) + body


def test_read_ulog_rows_decodes_a_real_synthetic_topic_and_refuses_missing_ones():
    raw = _ulog_bytes(
        {"vehicle_status": ("uint64_t timestamp",)},
        [("vehicle_status", struct.pack("<Q", 1_000_000))],
    )
    rows = ulog.read_ulog_rows(raw, ("vehicle_status",))
    assert rows == {"vehicle_status": [{"timestamp": 1_000_000}]}

    try:
        ulog.read_ulog_rows(raw, ("vehicle_local_position",))
    except ValueError as error:
        assert "missing required topic" in str(error)
    else:
        raise AssertionError("expected a ValueError for an absent topic")


def _page_data(page: bytes) -> dict:
    text = page.decode()
    payload = text.split('<script id="lockwire-data" type="application/json">', 1)[1].split(
        "</script>", 1
    )[0]
    return json.loads(payload)


def test_preflight_real_ulog_writes_deterministic_page_end_to_end(tmp_path, monkeypatch):
    # The recording openers moved into `lockwire.api` (the library door),
    # so the ulog loader the CLI reaches is `lockwire.api.load_ulog`.
    from lockwire_pro import api_pro as api

    load_ulog = api.load_ulog
    sample_counts: list[int] = []

    # **kwargs, not a bare (path): the CLI passes `context=` since --context
    # landed, and a double narrower than the real signature turns a keyword it
    # does not accept into a TypeError — which the CLI's broad handler reports as
    # "tool failed, exit 2". The page then never gets written and the failure
    # reads as a rendering bug rather than a stale test double.
    def _load_with_presence_floor(path, **kwargs):
        recording = load_ulog(path, **kwargs)
        sample_counts.append(sum(len(stream.recv_times) for stream in recording.devices.values()))
        return recording

    monkeypatch.setattr(api, "load_ulog", _load_with_presence_floor)
    page = tmp_path / "preflight.html"
    args = [
        "preflight",
        str(FIXTURES / "contract_empty.yaml"),
        "--from",
        str(SMALL_ULOG),
        "--html",
        str(page),
        "--no-ledger",
    ]

    first = CliRunner().invoke(app, args)
    first_page = page.read_bytes()
    second = CliRunner().invoke(app, args)
    second_page = page.read_bytes()
    data = _page_data(first_page)

    # contract_empty.yaml scores nothing -> NOT SCORED, exit 3, not GO.
    assert first.exit_code == second.exit_code == 3, first.output + second.output
    assert first.output == second.output
    assert first_page == second_page
    assert len(first_page) > 1_000
    assert len(sample_counts) == 2
    assert min(sample_counts) > 0  # Presence floor: both real loads carried samples.
    assert len(data["findings"]) > 0  # Presence floor: the gate emitted disclosures.
    assert data["not_scored"] > 0
    assert data["recording"] == str(SMALL_ULOG)
    assert "GO" in plain(first.output)
    assert "Traceback" not in plain(first.output)


def test_preflight_recording_directory_output_is_byte_pinned(monkeypatch):
    # E0-62d: the fixture was rendered at COLUMNS=80 (measured — rendering at 200
    # produces a different hash). `preflight` prints through the module-level
    # `cli.console`, which is built once at import time and caches its width from
    # COLUMNS at construction (rich reads env only in Console.__init__, not per
    # print) — so a plain os.environ/monkeypatch.setenv("COLUMNS", ...) here does
    # NOT reach it (measured: still hashed to the 200-wide render). Swap the
    # cached console itself for one built at width=80, independent of whatever
    # conftest.py's ambient COLUMNS pin is set to.
    monkeypatch.setattr(cli_pro, "console", Console(width=80))
    result = CliRunner().invoke(
        app,
        [
            "preflight",
            str(FIXTURES / "contract_clean.yaml"),
            "--from",
            str(FIXTURES / "bags" / "clean"),
            "--no-ledger",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(result.output) > 1_000
    # contract_clean.yaml's three physical-less devices
    # (dev.barometer, dev.gnss, dev.esc.busless) now disclose as not_scored
    # for mass instead of a silent skip, so this pinned byte hash moved.
    # G2 audit 2 §5 (2026-09-06): the redundant-sensors/camera-histogram
    # refusals dropped their "await B94/B95" filed-box wording for plain
    # operator sentences, and the coverage disclosures dropped their milestone codes —
    # both are in this stage's output, moving the hash again.
    #
    # E0-57: the hash alone tells a reader nothing on mismatch — it recruits
    # the next engineer into re-pinning blind, which is how this box went red
    # for ~40 merges. The checked-in .expected.txt is the last-known-good
    # rendered text; on a mismatch we show its unified diff against the actual
    # output so a reader can tell "a stage was added on purpose" from "broke".
    expected_path = FIXTURES / "preflight_clean_directory.expected.txt"
    expected_text = expected_path.read_text()
    actual_text = result.output
    diff = "".join(
        difflib.unified_diff(
            expected_text.splitlines(keepends=True),
            actual_text.splitlines(keepends=True),
            fromfile=str(expected_path),
            tofile="actual preflight output",
        )
    )
    assert hashlib.sha256(expected_text.encode()).hexdigest() == PINNED_SHA256, (
        f"fixture {expected_path} has drifted from pin"
    )
    assert hashlib.sha256(actual_text.encode()).hexdigest() == PINNED_SHA256, (
        diff or "hash mismatch but texts are byte-identical (encoding difference?)"
    )
