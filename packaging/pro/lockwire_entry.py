"""The script PyInstaller freezes (`packaging/pro/lockwire.spec` names this file).

PyInstaller freezes a *script*, not a console-script entry point, so `lockwire =
"lockwire:main"` in `pyproject.toml` is invisible to it; this file is that script.
Its private child modes run analysis and its CalculiX owner inside the same
frozen executable, which cannot interpret Python's `-m` switch.

`multiprocessing.freeze_support()` runs before anything else and is not optional in a
frozen build: a spawned child re-executes the frozen executable from the top, and
without this call it re-runs the CLI instead of the worker, which is a fork bomb rather
than an error message. It must precede the private child dispatch too.
"""

import multiprocessing
import sys

from lockwire import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if getattr(sys, "frozen", False) and sys.argv[1:2] == ["--internal-analysis"]:
        from lockwire_pro.analysis import main as analysis_main

        raise SystemExit(analysis_main(sys.argv[2:]))
    if getattr(sys, "frozen", False) and sys.argv[1:2] == ["--internal-ccx-owner"]:
        from lockwire_pro._ccx_owner import main as owner_main

        raise SystemExit(owner_main(sys.argv[2:]))
    main()
