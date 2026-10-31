"""launchd job state, read with ``launchctl print`` (works for gui/ and system/ domains)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ams.ops.runner import Runner
from ams.ops.runner import run as default_run

_KV = re.compile(r"^\t(?P<k>state|pid|runs|last exit code|last terminating signal) = (?P<v>.+)$")


@dataclass(frozen=True)
class JobState:
    loaded: bool
    state: str = ""  # "running", "not running", "waiting", ...
    pid: int | None = None
    runs: int | None = None
    last_exit: int | None = None  # None = never exited
    last_signal: str = ""
    detail: str = ""  # why it could not be read

    @property
    def running(self) -> bool:
        return self.loaded and self.state == "running" and self.pid is not None


def parse_print(text: str) -> JobState:
    """Parse the *top-level* keys of ``launchctl print`` (one tab of indent)."""
    got: dict[str, str] = {}
    for line in text.splitlines():
        m = _KV.match(line)
        if m and m["k"] not in got:
            got[m["k"]] = m["v"].strip()

    def num(key: str) -> int | None:
        m = re.match(r"-?\d+", got.get(key, ""))
        return int(m.group()) if m else None

    return JobState(
        loaded=True,
        state=got.get("state", ""),
        pid=num("pid"),
        runs=num("runs"),
        last_exit=num("last exit code"),
        last_signal=got.get("last terminating signal", ""),
    )


def read_job(spec: str, runner: Runner = default_run) -> JobState:
    """``spec`` = ``gui/501/<label>`` or ``system/<label>``."""
    res = runner(["/bin/launchctl", "print", spec], timeout=15)
    if res.rc != 0:
        text = (res.err or res.out).strip().splitlines()
        return JobState(loaded=False, detail=(text[0] if text else f"rc={res.rc}")[:200])
    return parse_print(res.out)
