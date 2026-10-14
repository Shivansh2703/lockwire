"""``python -m lockwire`` — the same CLI the ``lockwire`` script runs.

Exists so ``jobs.py`` can start a run as an argv list against ``sys.executable``
without depending on a console script being on PATH.
"""

from lockwire.cli import app

app()
