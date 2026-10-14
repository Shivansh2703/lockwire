# anymal_d.urdf

- Repo: https://github.com/ANYbotics/anymal_d_simple_description
- Path: `urdf/anymal.urdf`
- package.xml version: 1.0.9
- Copyright 2023, ANYbotics AG (per file header)
- Licence: BSD 3-clause (repo `LICENSE`)
- Fetched: 2026-09-06, via the upstream `anymal_d_simple_description` repository
- Meshes intentionally not copied — only the URDF text is needed to parse
  links/joints; `--package-root` mesh resolution is exercised by other fixtures.
- One joint, `base_to_hatch`, is commented out in the source file (line ~1403)
  and is correctly not parsed: 96 `<link>` elements, 95 real (uncommented)
  `<joint>` elements, one root link `base`.
