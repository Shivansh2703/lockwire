// Real, measured output from `uv run lockwire log --json -- tests/fixtures/x500/contract.yaml`
// (E0-35 worktree, 2026-09-08), three of the eleven real commits touching that fixture
// contract -- not invented data. Picked for size and variety: two entries with a small
// `fact_diff` (6 and 4 changes) and the repo's earliest commit touching the file, whose
// `fact_diff` is `null` because it has no parent to diff against.

import type { LogEntry } from "./historyPayload";

export const X500_HISTORY: LogEntry[] = [
  {
    "sha": "202f5f6895eecffa32e7cf04cd2b9e1895a12e83",
    "author": "Shivansh",
    "date": "2026-09-06T04:32:05-04:00",
    "message": "fix(x500): RFD900x figures re-read from the datasheet PDF \u2014 review caught a table not in the source",
    "verdict": "NO-GO",
    "fact_diff": {
      "changed": true,
      "changes": [
        {
          "path": "device_models/mdl.radio.rfd900x/input_voltage_range/max/value",
          "old": 5.25,
          "new": 5.5,
          "witness": "w0",
          "source": "https://files.rfdesign.com.au/Files/documents/RFD900x%20DataSheet%20V1.2.pdf",
          "limit_raised": false
        },
        {
          "path": "device_models/mdl.radio.rfd900x/input_voltage_range/min/value",
          "old": 4.75,
          "new": 5.0,
          "witness": "w0",
          "source": "https://files.rfdesign.com.au/Files/documents/RFD900x%20DataSheet%20V1.2.pdf",
          "limit_raised": false
        },
        {
          "path": "device_models/mdl.radio.rfd900x/provenance/note",
          "old": "RE-FETCHED 2026-09-06, first-hand from the vendor PDF (earlier lock was search-engine paraphrase and is superseded). Power supply voltage 4.75-5.25 V; TX current ~860 mA at +30 dBm; RX current ~110 mA; TX power 0-30 dBm adjustable; default UART baud 57600 bps (user-selectable: 9600/19200/38400/57600/115200/400k/921k); RF connector SMA, no bundled antenna gain published.",
          "new": "Corrected 2026-09-06 04:3x against the vendor PDF text itself (an earlier lock carried supply, current and sensitivity figures the PDF does not contain; caught in review). PDF, Power Requirements: supply +5 V nominal (+5 V min, +5.5 V max, +6 V abs max); transmit current 1 A peak at 30 dBm; receive/standby current 60 mA typical. Sensitivity table (10^-5 BER): 12 kbps -111 dBm, 56 kbps -107 dBm, 64 kbps -105 dBm, 100 kbps -102 dBm. Default air data rate 64k (RFDesign store page, same day); default serial 57600 bps (PDF, Point-to-Point/SiK defaults). TX power 0-30 dBm adjustable; RF connector SMA, no bundled antenna gain published.",
          "witness": "w0",
          "source": "https://files.rfdesign.com.au/Files/documents/RFD900x%20DataSheet%20V1.2.pdf",
          "limit_raised": false
        },
        {
          "path": "device_models/mdl.radio.rfd900x/radio/air_rate/value",
          "old": 57.6,
          "new": 64.0,
          "witness": "w0",
          "source": "https://files.rfdesign.com.au/Files/documents/RFD900x%20DataSheet%20V1.2.pdf",
          "limit_raised": false
        },
        {
          "path": "device_models/mdl.radio.rfd900x/radio/rx_sensitivity/value",
          "old": -103.0,
          "new": -105.0,
          "witness": "w0",
          "source": "https://files.rfdesign.com.au/Files/documents/RFD900x%20DataSheet%20V1.2.pdf",
          "limit_raised": false
        },
        {
          "path": "devices/dev.radio.rfd900x/provenance/note",
          "old": "Mass 14.5 g, fetched first-hand 2026-09-06 from RFDesign's own store product page ('Weight: 14.5g') \u2014 supersedes the earlier 14 g search-engine-paraphrase figure. TX/RX current is omitted here because it is state-dependent (see mdl.radio.rfd900x for the datasheet's ~860 mA TX / ~110 mA RX figures). Power draw is not routed onto a modeled rail: Pixhawk board current draw is UNKNOWN (no sourced companion/TELEM rail capacity to budget against), so the radio's current is deliberately left undeclared rather than folded onto rail.pm02-5v and diluting the D11 headline finding.",
          "new": "Mass 14.5 g, fetched first-hand 2026-09-06 from RFDesign's own store product page ('Weight: 14.5g') \u2014 supersedes the earlier 14 g search-engine-paraphrase figure. TX/RX current is omitted here because it is state-dependent (see mdl.radio.rfd900x for the datasheet's 1 A peak TX / 60 mA typical receive figures). Power draw is not routed onto a modeled rail: Pixhawk board current draw is UNKNOWN (no sourced companion/TELEM rail capacity to budget against), so the radio's current is deliberately left undeclared rather than folded onto rail.pm02-5v and diluting the D11 headline finding.",
          "witness": "w0",
          "source": "https://store.rfdesign.com.au/rfd-900x-modem/",
          "limit_raised": false
        }
      ],
      "added": [],
      "removed": [],
      "findings_new": [],
      "findings_gone": []
    }
  },
  {
    "sha": "4e34062df356a9fd24789b964fd36ce162761a06",
    "author": "Shivansh",
    "date": "2026-07-26T08:33:25-04:00",
    "message": "build(B31): the MAVLink path exists in the model - UART, baud honestly UNKNOWN",
    "verdict": "NO-GO",
    "fact_diff": {
      "changed": true,
      "changes": [
        {
          "path": "devices/dev.companion.jetson-orin-nano/identity/bus_ref",
          "old": "bus.usb.companion",
          "new": "bus.uart.companion",
          "witness": "w0",
          "source": "NVIDIA module datasheet; NVIDIA power-mode docs; NVIDIA staff forum reply; docs/reference_robot_specs.md",
          "limit_raised": false
        },
        {
          "path": "devices/dev.companion.jetson-orin-nano/identity/host",
          "old": "companion-computer",
          "new": "flight-controller",
          "witness": "w0",
          "source": "NVIDIA module datasheet; NVIDIA power-mode docs; NVIDIA staff forum reply; docs/reference_robot_specs.md",
          "limit_raised": false
        },
        {
          "path": "devices/dev.companion.jetson-orin-nano/provenance/note",
          "old": "Bare module on a custom carrier. Draw is 25 W published mode; 5 A is derived from 25 W / 5 V module VDD_IN. Module-only mass is UNKNOWN: dev-kit 176 g is the kit, not the module.",
          "new": "Bare module on a custom carrier. Draw is 25 W published mode; 5 A is derived from 25 W / 5 V module VDD_IN. Module-only mass is UNKNOWN: dev-kit 176 g is the kit, not the module. Identity is on bus.uart.companion (Pixhawk TELEM/UART); baud UNKNOWN.",
          "witness": "w0",
          "source": "NVIDIA module datasheet; NVIDIA power-mode docs; NVIDIA staff forum reply; docs/reference_robot_specs.md",
          "limit_raised": false
        },
        {
          "path": "model/source_sha256",
          "old": "46d539b2be6da8c4880d3df5ab93c6fbdb75f28999618678efe4361bc5a52c05",
          "new": "c8c9ce97375d59f6c40f006ecaa0522a943ebaaa0dd5b92533f3d72fbc9779ca",
          "witness": "w0",
          "source": "holybro.com/products/x500-v2-kits",
          "limit_raised": false
        }
      ],
      "added": [
        {
          "kind": "buses",
          "id": "bus.uart.companion"
        }
      ],
      "removed": [],
      "findings_new": [
        {
          "severity": "not_scored",
          "quantity": "topology/buses/bus.uart.companion/bandwidth"
        }
      ],
      "findings_gone": []
    }
  },
  {
    "sha": "f5509acc312c46812109c44cf4c499c452ba2280",
    "author": "Shivansh",
    "date": "2026-07-25T18:03:37-04:00",
    "message": "build(B4): X500-class reference contract carrying the real power violation",
    "verdict": "NO-GO",
    "fact_diff": null
  }
];

// SYNTHETIC, not measured -- `lockwire log` does not exist at this base (e0-35 is
// unmerged/bounced), so no real whitespace-only commit's `--json` output was available to
// capture. Shaped by hand to match `gitlog.LogEntry`/`factdiff.FactDiff`: a commit with a
// parent whose `fact_diff.changed` is `false` and `changes` is empty, the field-for-field
// shape a whitespace-only change to the contract text must produce (the decisive case the
// reviewer named: it must show no changed facts, never `git diff` text).
export const WHITESPACE_ONLY_ENTRY: LogEntry = {
  sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  author: "Shivansh",
  date: "2026-09-08T00:00:00-04:00",
  message: "style: reindent lockwire.yaml, no facts touched",
  verdict: "GO",
  fact_diff: {
    changed: false,
    changes: [],
    added: [],
    removed: [],
    findings_new: [],
    findings_gone: [],
  },
};
