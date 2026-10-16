# dev.companion.jetson-orin-nano

- model: mdl.companion.jetson-orin-nano
- family: fam.companion.sbc — Companion computer
- owner: companion-compute
- identity strength: positional
- bus: bus.uart.companion

## Role

component

## Measures

Measures: nothing declared.

## Adjusts

Adjusts: nothing declared.

## Limits enforced

- input_voltage_range: [4.75 V, 5.25 V] (witness: w0, source: https://developer.nvidia.com/downloads/assets/embedded/secure/jetson/orin_nano/docs/jetson_orin_nano_devkit_carrier_board_specification_sp.pdf; https://developer.nvidia.com/downloads/jetson-orin-nx-module-series-data-sheet; NVIDIA power-mode docs; NVIDIA staff forum reply, locked_at: 2026-07-26)
- power draw: 25.00 W on rail.pm02-5v
  - mode 7W: 7.00 W
  - mode 15W: 15.00 W
  - mode 25W: 25.00 W
  - rail rail.pm02-5v capacity: 15.60 W at 5.20 V (witness: w0, source: docs/reference_robot_specs.md, locked_at: 2026-07-25)

## Provenance

- instance:
  - witness: w0
  - source: NVIDIA module datasheet; NVIDIA power-mode docs; NVIDIA staff forum reply; docs/reference_robot_specs.md
  - locked_at: 2026-07-26
  - note: Bare module on a custom carrier. Nominal draw remains 25 W (fallback). Published Orin Nano power modes 7 W / 15 W / 25 W from docs/reference_robot_specs.md (table line citing NVIDIA docs; companion section). 5 A is derived from 25 W / 5 V module VDD_IN. Module-only mass is UNKNOWN: dev-kit 176 g is the kit, not the module. Identity is on bus.uart.companion (Pixhawk TELEM/UART); baud UNKNOWN.
- model:
  - witness: w0
  - source: https://developer.nvidia.com/downloads/assets/embedded/secure/jetson/orin_nano/docs/jetson_orin_nano_devkit_carrier_board_specification_sp.pdf; https://developer.nvidia.com/downloads/jetson-orin-nx-module-series-data-sheet; NVIDIA power-mode docs; NVIDIA staff forum reply
  - locked_at: 2026-07-26
  - note: Bare module on a custom carrier: 5 V module VDD_IN; published power modes 7 W / 15 W / 25 W (docs/reference_robot_specs.md); 25 W mode implies 5 A peak. Module-only mass is UNKNOWN; the published 176 g dev-kit figure is the wrong product. input_voltage_range 4.75-5.25 V: the Orin Nano dev-kit carrier board spec (SP-11324-001 v1.3) says VDD_IN will be 5V only; the tolerance is NVIDIA's legacy-module figure from the Orin NX datasheet DS-10712-001 Table 5-1 (MODULE_ID low). Earlier 5-20 V was the wrong pin rating.
