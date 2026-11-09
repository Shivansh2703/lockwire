"""Own one CalculiX child after its analysis caller exits, including hard death."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from pathlib import Path


def _stop_solver(process: subprocess.Popen[bytes]) -> None:
    """Terminate the solver's own group and reap its direct child on every exit."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    parent_fd = int(args[0])
    binary = Path(args[1])
    solver_dir = Path(args[2])
    process: subprocess.Popen[bytes] | None = None

    def stop(_signum: int, _frame: object) -> None:
        raise SystemExit(2)

    # The analysis parent masks these signals while spawning this process.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with (
            (solver_dir / "stdout.txt").open("wb") as out,
            (solver_dir / "stderr.txt").open("wb") as err,
        ):
            process = subprocess.Popen(  # noqa: S603 - verified copy and fixed argv
                [str(binary), "-i", "model"],
                cwd=solver_dir,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            while True:
                if select.select([parent_fd], [], [], 0.05)[0] and not os.read(parent_fd, 1):
                    return 2
                if (returncode := process.poll()) is not None:
                    print(returncode, flush=True)
                    return 0
    except OSError as error:
        print(f"CalculiX could not start: {error}", file=sys.stderr)
        return 2
    finally:
        if process is not None:
            _stop_solver(process)
        os.close(parent_fd)


if __name__ == "__main__":
    raise SystemExit(main())
