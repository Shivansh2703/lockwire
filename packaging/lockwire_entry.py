"""The script PyInstaller freezes (`packaging/lockwire.spec` names this file).

PyInstaller freezes a *script*, not a console-script entry point, so `lockwire =
"lockwire:main"` in `pyproject.toml` is invisible to it; this file is that script.

`multiprocessing.freeze_support()` runs before anything else and is not optional in a
frozen build: a spawned child re-executes the frozen executable from the top, and
without this call it re-runs the CLI instead of the worker, which is a fork bomb rather
than an error message.
"""

import multiprocessing

from lockwire import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
