"""The core control plane, through the api tree's own ``scripts/corectl.mjs``.

core listens on a unix socket (``CORE_SOCKET``, JSON lines ``{"id","op","args"}``)
whose file mode 0660 is its only access control, and it is owned by the *service*
uid -- the harness itself cannot connect in isolated mode. So every call here runs
the upstream client, ``node <tree>/scripts/corectl.mjs --socket <sock> <command>``,
through a :class:`Runner`:

- :func:`isolated_runner` -- :func:`ams.userns.run_as_service`, i.e. as the service's
  own identity inside its runtime map (production);
- :func:`plain_runner` -- ``subprocess.run`` as the current user (dev, macOS,
  ``--no-isolation``).

The protocol is **not** re-implemented: corectl already does the CAS dance
(``expectedGeneration`` from a fresh ``status``) and the artifact base64 upload, and
a second implementation would drift from the one production uses. corectl prints
its result as JSON on stdout and exits 1 when a transition's ``outcome != ok`` --
that is a *result* (:meth:`CoreControl.deploy` returns it), not a crash. Anything
else non-zero -- core unreachable, an unknown plugin, a CAS conflict raised as an
error -- is a :class:`CoreControlError` carrying the stderr tail.

Upstream shapes this module relies on (``packages/core/control.ts``/``manager.ts``):
``status`` -> ``{autoDeploy, dailyQuota, plugins: [{pluginId, desired{artifactId,
enabled, privileges, autoDeploy} | null, observed{generationId, artifactId, phase,
reason, lastKnownGood, previousArtifactId, ...} | null, live{generationId, phase,
artifactId, commit, ...} | null}]}``; a transition is ``{pluginId, kind, outcome
ok|failed|rejected, reason, fromArtifact, toArtifact, generationId, commit}``.
corectl has no ``ping`` command, so :meth:`CoreControl.ping` is a ``status``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ams.uidmap import UidBlock
from ams.userns import AdminResult

log = logging.getLogger("ams.platform.corectl")

CORECTL_REL = ("scripts", "corectl.mjs")
#: corectl records ``control:<SUDO_USER>`` as the actor of every change, which is how
#: ams's deploys are told apart from a human's in ``corectl transitions``.
ACTOR = "ams"
DEFAULT_TIMEOUT_S = 60.0
#: An upload is a base64 artifact of up to 64 MiB on one line.
UPLOAD_TIMEOUT_S = 300.0
#: A deploy includes the plugin's isolated start and first health check.
DEPLOY_TIMEOUT_S = 180.0
STDERR_TAIL_LINES = 20
PRIVILEGES = ("ops.read", "ops.deploy")
_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{64}$")


#: corectl reports a core-side refusal as ``corectl: <code>: <message>`` on stderr
#: (``Control.call`` in upstream ``scripts/corectl.mjs``); a transport failure
#: (``connect ENOENT``) or a client-side error has no such code.
_ERROR_CODE_RE = re.compile(r"^corectl: ([a-z][a-z0-9_]*): ", re.MULTILINE)


class CoreControlError(RuntimeError):
    """corectl could not produce a result (core down, bad request, protocol drift).

    ``code`` is core's own error code when core refused the request
    (``invalid_manifest``, ``artifact_too_large``, ``unknown_plugin`` ...), and
    ``None`` when no answer came from core at all -- the distinction a caller
    needs to tell "these bytes are refused" from "try again".
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def error_code(stderr: str) -> str | None:
    """The last ``corectl: <code>: ...`` code in corectl's stderr, if any."""
    codes = _ERROR_CODE_RE.findall(stderr)
    return codes[-1] if codes else None


class Runner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str | None,
        timeout_s: float,
    ) -> AdminResult: ...


def isolated_runner(block: UidBlock) -> Runner:
    """Run as the service's own uid (``userns.run_as_service``).

    A :class:`~ams.userns.SpawnError` (tool missing on ``PATH``, the namespace
    could not be set up, ``cwd`` not enterable) becomes rc 127, the shape
    :func:`plain_runner` reports, so :class:`CoreControl` turns it into a
    :class:`CoreControlError` like any other failed call instead of letting it
    escape a tick's error handling.
    """

    def run(
        argv: Sequence[str], *, env: Mapping[str, str], cwd: str | None, timeout_s: float
    ) -> AdminResult:
        from ams import userns

        try:
            return userns.run_as_service(list(argv), block, env=env, cwd=cwd, timeout_s=timeout_s)
        except userns.SpawnError as e:
            log.warning("isolated runner: %s: %s", argv[0] if argv else "?", e)
            return AdminResult(tuple(argv), 127, b"", str(e).encode())

    return run


def plain_runner() -> Runner:
    """``subprocess.run`` as the current user; ``argv[0]`` resolved on ``env["PATH"]``.

    Never a shell. A missing executable is rc 127 and a timeout a negative rc, the
    same shapes ``run_as_service`` reports, so callers need no second error path.
    """

    def run(
        argv: Sequence[str], *, env: Mapping[str, str], cwd: str | None, timeout_s: float
    ) -> AdminResult:
        args = list(argv)
        exe = shutil.which(args[0], path=env.get("PATH", ""))
        if exe is None:
            msg = f"{args[0]!r} not found on PATH={env.get('PATH', '')}"
            log.warning("plain runner: %s", msg)
            return AdminResult(tuple(args), 127, b"", msg.encode())
        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, never a shell string
                [exe, *args[1:]],
                env=dict(env),
                cwd=cwd,
                capture_output=True,
                timeout=timeout_s,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as e:
            log.warning("plain runner: %s timed out after %.0fs", " ".join(args), timeout_s)
            out = e.stdout if isinstance(e.stdout, bytes) else b""
            return AdminResult(tuple(args), -9, out, f"timed out after {timeout_s}s".encode())
        except OSError as e:
            return AdminResult(tuple(args), 126, b"", str(e).encode())
        log.debug(
            "plain %s -> rc=%d in %.1fs",
            " ".join(args),
            proc.returncode,
            time.monotonic() - started,
        )
        return AdminResult(tuple(args), proc.returncode, proc.stdout, proc.stderr)

    return run


def _tail(data: bytes, lines: int = STDERR_TAIL_LINES) -> str:
    return "\n".join(data.decode(errors="replace").strip().splitlines()[-lines:])


def _plugin(plugin_id: str) -> str:
    # Also what keeps an id from being read as one of corectl's own --options.
    if not _PLUGIN_ID_RE.match(plugin_id):
        raise ValueError(f"not a plugin id: {plugin_id!r}")
    return plugin_id


class CoreControl:
    """One core, addressed through one tree's ``corectl.mjs`` and one runner."""

    def __init__(self, runner: Runner, tree: Path, socket: Path, path_env: str) -> None:
        self.runner = runner
        self.tree = Path(tree)
        self.socket = Path(socket)
        self.path_env = path_env

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CoreControl(tree={str(self.tree)!r}, socket={str(self.socket)!r})"

    def _env(self) -> dict[str, str]:
        return {"PATH": self.path_env, "LANG": "C.UTF-8", "SUDO_USER": ACTOR}

    def _call(self, args: Sequence[str], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Any:
        argv = ["node", str(self.tree.joinpath(*CORECTL_REL)), "--socket", str(self.socket), *args]
        what = f"corectl {' '.join(args[:2])}"
        started = time.monotonic()
        result = self.runner(argv, env=self._env(), cwd=str(self.tree), timeout_s=timeout_s)
        elapsed = time.monotonic() - started
        text = result.stdout.decode(errors="replace").strip()
        parsed: Any = None
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        if result.returncode == 0:
            if parsed is None and text != "null":
                raise CoreControlError(f"{what}: output is not JSON: {text[:200]!r}")
            log.debug("%s -> ok in %.1fs", what, elapsed)
            return parsed
        if isinstance(parsed, dict) and "outcome" in parsed:
            # corectl's own convention: exit 1 = a transition that did not succeed.
            log.info(
                "%s -> outcome=%s reason=%s in %.1fs",
                what,
                parsed.get("outcome"),
                parsed.get("reason"),
                elapsed,
            )
            return parsed
        stderr = _tail(result.stderr) or "(no stderr)"
        code = error_code(stderr)
        log.warning(
            "%s -> rc=%d code=%s in %.1fs: %s", what, result.returncode, code, elapsed, stderr
        )
        raise CoreControlError(f"{what} failed (rc={result.returncode}): {stderr}", code=code)

    # ------------------------------------------------------------------ reads

    def status(self) -> dict[str, Any]:
        out = self._call(["status"])
        if not isinstance(out, dict) or not isinstance(out.get("plugins"), list):
            raise CoreControlError("corectl status: expected {plugins: [...]}")
        return out

    def transitions(self, plugin_id: str, limit: int = 20) -> list[dict[str, Any]]:
        out = self._call(["transitions", _plugin(plugin_id), "--limit", str(int(limit))])
        return [t for t in out if isinstance(t, dict)] if isinstance(out, list) else []

    def failures(self, plugin_id: str, limit: int = 5) -> list[dict[str, Any]]:
        out = self._call(["failures", _plugin(plugin_id), "--limit", str(int(limit))])
        return [f for f in out if isinstance(f, dict)] if isinstance(out, list) else []

    def ping(self) -> bool:
        """``True`` when core answers on its socket (corectl has no ping: a status)."""
        try:
            self.status()
        except CoreControlError as e:
            log.info("core not answering: %s", e)
            return False
        return True

    # ---------------------------------------------------------------- changes

    def upload(self, artifact_file: Path) -> str:
        out = self._call(["upload", str(artifact_file)], timeout_s=UPLOAD_TIMEOUT_S)
        artifact_id = out.get("artifactId") if isinstance(out, dict) else None
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.match(artifact_id):
            raise CoreControlError("corectl upload: no artifactId in the result")
        log.info("uploaded %s as %s", Path(artifact_file).name, artifact_id[:12])
        return artifact_id

    def deploy(self, artifact_id: str) -> dict[str, Any]:
        if not _ARTIFACT_ID_RE.match(artifact_id):
            raise ValueError(f"not an artifact id: {artifact_id!r}")
        out = self._call(["deploy", artifact_id], timeout_s=DEPLOY_TIMEOUT_S)
        if not isinstance(out, dict) or "outcome" not in out:
            raise CoreControlError("corectl deploy: expected a transition with an outcome")
        return out

    def restart(self, plugin_id: str) -> dict[str, Any]:
        """New generation of the same artifact (how a privilege change takes effect)."""
        out = self._call(["restart", _plugin(plugin_id)], timeout_s=DEPLOY_TIMEOUT_S)
        if not isinstance(out, dict):
            raise CoreControlError("corectl restart: expected a transition")
        return out

    def gc(self) -> dict[str, Any]:
        """Delete stored artifacts nothing references (desired, lastKnownGood,
        previous, active). Without it the store only grows, and upstream's
        ``corectl deploy`` reads and hashes *every* stored artifact first."""
        out = self._call(["gc"])
        if not isinstance(out, dict):
            raise CoreControlError("corectl gc: expected {removed, kept}")
        removed = out.get("removed")
        log.info(
            "corectl gc: removed %d artifact(s), kept %s",
            len(removed) if isinstance(removed, list) else 0,
            out.get("kept"),
        )
        return out

    def privileges(self, plugin_id: str, privileges: Sequence[str]) -> dict[str, Any]:
        bad = [p for p in privileges if p not in PRIVILEGES]
        if bad:
            raise ValueError(f"unknown privilege(s) {bad}; allowed: {list(PRIVILEGES)}")
        out = self._call(["privileges", _plugin(plugin_id), *privileges])
        if not isinstance(out, dict):
            raise CoreControlError("corectl privileges: expected the desired row")
        log.info("privileges %s -> %s", plugin_id, ",".join(privileges) or "(none)")
        return out
