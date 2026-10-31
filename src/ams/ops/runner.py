"""The one place that spawns processes: argv lists only, never a shell, always a timeout."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

log = logging.getLogger("ams.ops.runner")


@dataclass(frozen=True)
class CmdResult:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


Runner = Callable[..., CmdResult]


def run(argv: Sequence[str], timeout: float = 30.0) -> CmdResult:
    """Run ``argv`` (no shell). rc 124 = timeout, 127 = not executable."""
    try:
        cp = subprocess.run(  # noqa: S603 - argv list, no shell
            list(argv),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        log.warning("timeout after %.0fs: %s", timeout, argv[0])
        return CmdResult(124, "", f"timeout after {timeout:.0f}s")
    except OSError as e:
        return CmdResult(127, "", f"{type(e).__name__}: {e}")
    return CmdResult(cp.returncode, cp.stdout, cp.stderr)
