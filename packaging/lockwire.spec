# -*- mode: python ; coding: utf-8 -*-
"""Freeze the FREE lockwire only (no child modes; the paid build is pro/lockwire.spec) into the folder-style binary the desktop app launches as a sidecar.

Build it by hand on the machine that ships it:

    uv run pyinstaller packaging/lockwire.spec --noconfirm

PyInstaller cannot cross-compile, so this produces a binary for *this* machine's
OS and architecture only — macOS arm64, macOS Intel and Linux x64 are three builds
on three machines. releases are built and signed on the owner's M2 and CI is
Linux-only, so nothing here may assume a Mac runner. This spec builds unsigned;
signing under the hardened runtime is E6's.

Measured size, macOS arm64, 2026-09-08, unsigned: **56 MB** (`du -sh dist/lockwire`). The
41 MB figure that used to be quoted for this file was the pywebview build; this is the
sidecar, measured.

Three things a static analyser cannot see, and they are the whole reason this file
is longer than four lines:

* **uvicorn names its pieces at runtime.** `uvicorn.loops.auto`, the h11/httptools
  protocol implementations and `uvicorn.lifespan.on` are chosen by *string* from
  the config and are reachable from no import statement. A spec without them builds
  cleanly and then the frozen sidecar dies on its first request. `collect_submodules`
  is deliberately blunt here: the cost is a few hundred KB, the alternative is a
  binary that serves nothing.

  Measured 2026-09-08: `pyinstaller-hooks-contrib` currently ships `hook-uvicorn.py`,
  which collects the same modules on its own once uvicorn is anywhere in the graph —
  a build with this line deleted still had all three. The line stays anyway. It is a
  third-party hook in a package that versions weekly (2026.7 today), the failure mode
  it guards is a binary that serves nothing, and the cost of keeping it is one line.
* **`report.py` imports rich lazily** so that `import lockwire.api` costs no CLI
  dependency. Lazy imports are exactly what the analyser walks past.

`webview` (pywebview) is *excluded*, not hidden-imported: the Electron app is the
window now, and the sidecar has no GUI at all. Leaving its back-ends in dragged
PyObjC/Cocoa into the bundle for a window that is never opened.

`require_extras.check()` runs before `Analysis` and FAILS the build when an extra the
binary's CLI offers unconditionally is not importable on this host. Without it the extras
fail open — `collect_submodules` answers `[]` for an absent package instead of raising —
and the result is a green 39 MB binary whose `mcp` door exits 2 and tells the user to run
`uv sync`, inside a binary with no venv to sync. See `packaging/require_extras.py`.

Folder-style (`exclude_binaries=True` + `COLLECT`), never onefile: onefile unpacks
the whole bundle to a temp directory on every launch, and the app starts this
process per session. Folder-style starts in milliseconds.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

# Absolute, derived from this spec's own location. The app must launch the sidecar by
# its absolute path inside the bundle and never by searching PATH — a PATH lookup finds
# whatever `lockwire` the user happens to have installed (a different version, or a
# shim), which is a silent wrong-binary bug rather than a crash. Nothing in this
# directory looks anything up on PATH.
PACKAGING = Path(SPECPATH).resolve()  # noqa: F821 — PyInstaller injects SPECPATH
PROJECT_ROOT = PACKAGING.parent
ENTRY_SCRIPT = PACKAGING / "lockwire_entry.py"

# The extras gate, BEFORE Analysis so no build can reach the bundle without passing it.
# `collect_submodules` returns [] for a module that is not installed rather than raising,
# so every line below would fail open on a build host missing an extra and ship a green,
# smaller binary whose doors die on first use. `require_extras` is the only thing standing
# between that host and a release. See its docstring for the measurement (E6-MX).
sys.path.insert(0, str(PACKAGING))
import require_extras  # noqa: E402 — must follow the sys.path line above

require_extras.check(require_extras.FREE_CLAIMED)

hiddenimports = [
    # Named at runtime by uvicorn's config, from no import statement anywhere.
    *collect_submodules("uvicorn"),
    # Imported lazily inside functions (see the module docstring).
    *collect_submodules("lockwire"),
    "rich.console",
    "rich.table",
    "rich.markup",
    # Extras. Gated above, so these are no longer allowed to come back empty.
    *collect_submodules("mcp"),
]

a = Analysis(  # noqa: F821
    [str(ENTRY_SCRIPT)],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    # Non-.py files read next to their module: lockwire/catalog_seed.yaml (catalog.py).
    # Without it the frozen sidecar dies on first read.
    datas=[*copy_metadata("lockwire"), *collect_data_files("lockwire")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "webview",  # pywebview: the Electron app is the window; the sidecar has no GUI
        "tkinter",
        "pytest",
        "PyInstaller",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # folder-style; see the module docstring
    name="lockwire",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # PyInstaller cannot cross-compile: this is the build host's arch
    codesign_identity=None,  # unsigned; signing is E6's
    entitlements_file=None,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="lockwire",
)
