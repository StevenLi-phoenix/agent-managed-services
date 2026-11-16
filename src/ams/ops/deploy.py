"""Follow a branch and deploy each new commit: the ``[[deploy]]`` entries of ``watch.toml``.

Work on a repository ends with a push to its branch; nobody runs a deploy by hand. Every
``every_s`` the follower fetches the branch, fast-forwards the working copy when it is clean and
behind, and starts the repository's own deploy command in a process of its own, so a build that
takes minutes never stalls the tick. Each commit gets one result, escalated through the intake
like every incident: ``deployed`` (info) or ``deploy failed`` (error, with the tail of the log). A
commit whose deploy failed is not tried again; the next push is.

The follower never resets, stashes or checks anything out. A working copy on another branch, with
uncommitted changes to tracked files, or with commits the remote does not have is left alone and
escalated once per commit and cause.

The command runs as the owner inside the owner's GUI session (``ams watch`` is the owner's
LaunchAgent), so a code-signing build reaches the login keychain, which a process started over SSH
cannot. ``python -m ams.ops.deploy --rc-file F -- argv...`` is the wrapper that runs it and
writes its exit code to ``F``, so the result survives a restart of ``ams watch``.
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ams.ops import freeze as freeze_mod
from ams.ops.config import Config, DeployConfig
from ams.ops.runner import CmdResult, Runner
from ams.ops.runner import run as default_run
from ams.ops.text import clip, sanitize

log = logging.getLogger("ams.ops.deploy")

GIT = "/usr/bin/git"
GIT_TIMEOUT_S = 30
FETCH_TIMEOUT_S = 120
FETCH_FAILS_BEFORE_ESCALATE = 6  # 30 minutes at the default every_s
MAX_INTERRUPTED = 3  # a run that died without an exit code (reboot) is retried this many times
LOG_TAIL_LINES = 15
KILL_GRACE_S = 10


class Handle(Protocol):
    pid: int

    def poll(self) -> int | None: ...


Spawn = Callable[[list[str], Path, dict[str, str], Path], Handle]


def spawn(argv: list[str], cwd: Path, env: dict[str, str], log_path: Path) -> Handle:
    """Start ``argv`` in a session of its own (it outlives a restart of ``ams watch``)."""
    with open(log_path, "ab") as out:
        return subprocess.Popen(  # noqa: S603 - argv list, no shell
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


@dataclass(frozen=True)
class Event:
    """One result worth telling the operator: an escalation and a journal record."""

    deploy: str
    decision: str  # deployed | deploy_failed | deploy_blocked | fetch_failing
    severity: str  # info | warn | error
    title: str
    text: str
    sha: str

    @property
    def key(self) -> str:
        return f"deploy-{self.deploy}-{self.sha[:12] or 'none'}-{self.decision}-{self.title[-24:]}"


def _empty() -> dict[str, Any]:
    return {
        "last_check": 0.0,
        "deployed": None,  # last commit deployed successfully
        "failed": None,  # last commit whose deploy failed: not tried again
        "blocked": None,  # "<sha>|<cause>" escalated last, so a cause is told once per commit
        "fetch_fails": 0,
        "attempts": {"sha": None, "n": 0},
        "run": None,  # {sha, pid, started, log, rc_file, attempt} while a deploy runs
    }


def _short(sha: str) -> str:
    return sha[:8] if sha else "?"


class Deployer:
    def __init__(
        self,
        cfg: Config,
        state: dict[str, Any],
        *,
        runner: Runner = default_run,
        spawn: Spawn = spawn,
        sleep: Callable[[float], None] = time.sleep,
        kill: Callable[[int, int], None] = os.killpg,
    ) -> None:
        self.cfg, self.state, self.runner = cfg, state, runner
        self.spawn, self.sleep, self.kill = spawn, sleep, kill
        self._handles: dict[str, Handle] = {}

    @property
    def log_dir(self) -> Path:
        return self.cfg.state_dir / "deploy"

    # ---- git ----------------------------------------------------------------------------
    def _git(self, d: DeployConfig, *args: str, timeout: float = GIT_TIMEOUT_S) -> CmdResult:
        return self.runner([GIT, "-C", str(d.repo), *args], timeout=timeout)

    def _fetch(self, d: DeployConfig) -> CmdResult:
        opts: list[str] = []
        if d.ssh_key is not None:
            ssh = f"ssh -i {shlex.quote(str(d.ssh_key))} -o IdentitiesOnly=yes -o BatchMode=yes"
            opts = ["-c", f"core.sshCommand={ssh}"]
        refspec = f"+refs/heads/{d.branch}:refs/remotes/{d.remote}/{d.branch}"
        return self.runner(
            [GIT, "-C", str(d.repo), *opts, "fetch", "--quiet", "--no-tags", d.url or d.remote,
             refspec],
            timeout=FETCH_TIMEOUT_S,
        )  # fmt: skip

    def _subject(self, d: DeployConfig, sha: str) -> str:
        res = self._git(d, "log", "-1", "--format=%s", sha)
        return clip(sanitize(res.out.strip(), 120), 120) if res.ok else ""

    # ---- the tick -----------------------------------------------------------------------
    def tick(self, now: float, freezes: dict[str, freeze_mod.Freeze]) -> list[Event]:
        events: list[Event] = []
        for d in self.cfg.deploys:
            st = self.state.setdefault(d.id, _empty())
            for k, v in _empty().items():
                st.setdefault(k, v)
            try:
                events += self._one(d, st, now, freezes)
            except Exception:  # one bad entry must not take the others or the loop down
                log.exception("deploy %s: check crashed", d.id)
        return events

    def _one(
        self, d: DeployConfig, st: dict[str, Any], now: float, freezes: dict[str, freeze_mod.Freeze]
    ) -> list[Event]:
        if st["run"]:
            return self._check_run(d, st, now)
        if now - st["last_check"] < d.every_s:
            return []
        st["last_check"] = now
        fz = freeze_mod.lookup(freezes, d.id)
        if fz is not None:
            log.info("deploy %s: frozen (%s), not checking", d.id, fz.reason)
            return []
        head_ref = self._git(d, "rev-parse", "--abbrev-ref", "HEAD")
        if not head_ref.ok:
            return self._blocked(
                d, st, "", "unreadable", f"读不到 {d.repo} 的 git 状态：{head_ref.err.strip()}"
            )
        if head_ref.out.strip() != d.branch:
            on = head_ref.out.strip()
            return self._blocked(
                d, st, "", f"branch-{on}", f"{d.repo} 在分支 {on} 上，不是 {d.branch}，没有部署"
            )
        fetched = self._fetch(d)
        if not fetched.ok:
            st["fetch_fails"] += 1
            err = clip(sanitize((fetched.err or fetched.out).strip(), 300), 300)
            log.warning("deploy %s: fetch failed (%d in a row): %s", d.id, st["fetch_fails"], err)
            if st["fetch_fails"] == FETCH_FAILS_BEFORE_ESCALATE:
                return [
                    Event(
                        d.id,
                        "fetch_failing",
                        "warn",
                        f"{d.id} fetch failing",
                        f"连续 {st['fetch_fails']} 次拉取 {d.url or d.remote} 失败，"
                        f"新提交不会部署：{err}",
                        "",
                    )
                ]
            return []
        st["fetch_fails"] = 0
        target_res = self._git(d, "rev-parse", f"refs/remotes/{d.remote}/{d.branch}")
        head_res = self._git(d, "rev-parse", "HEAD")
        if not (target_res.ok and head_res.ok):
            return self._blocked(d, st, "", "unreadable", f"{d.repo}: rev-parse 失败")
        target, head = target_res.out.strip(), head_res.out.strip()
        if target in (st["deployed"], st["failed"]):
            return []
        if head != target:
            dirty = self._git(d, "status", "--porcelain", "--untracked-files=no")
            if not dirty.ok or dirty.out.strip():
                files = ", ".join(ln[3:] for ln in dirty.out.splitlines()[:5])
                return self._blocked(
                    d, st, target, "dirty",
                    f"{d.repo} 有未提交的改动（{files or dirty.err.strip()}），"
                    f"{_short(target)} 没有部署",
                )  # fmt: skip
            behind = self._git(d, "merge-base", "--is-ancestor", head, target)
            if not behind.ok:
                return self._blocked(
                    d, st, target, "diverged",
                    f"{d.repo} 的 {d.branch} 有远端没有的提交（HEAD {_short(head)}），"
                    f"{_short(target)} 没有部署；先把这些提交推上去或合并",
                )  # fmt: skip
            if self.cfg.mode == "dry-run":
                log.info("deploy %s: dry-run, would fast-forward to %s", d.id, _short(target))
                return []
            merged = self._git(d, "merge", "--ff-only", "--quiet", target)
            if not merged.ok:
                return self._blocked(
                    d, st, target, "merge",
                    f"{d.repo} 快进到 {_short(target)} 失败：{clip(merged.err.strip(), 200)}",
                )  # fmt: skip
            log.info("deploy %s: fast-forwarded %s -> %s", d.id, _short(head), _short(target))
        elif self.cfg.mode == "dry-run":
            log.info("deploy %s: dry-run, would deploy %s", d.id, _short(target))
            return []
        return self._start(d, st, target, now)

    def _blocked(
        self, d: DeployConfig, st: dict[str, Any], sha: str, cause: str, text: str
    ) -> list[Event]:
        key = f"{sha}|{cause}"
        log.warning("deploy %s: blocked (%s): %s", d.id, cause, text)
        if st["blocked"] == key:
            return []
        st["blocked"] = key
        return [Event(d.id, "deploy_blocked", "warn", f"{d.id} deploy blocked {cause}", text, sha)]

    # ---- running the command ------------------------------------------------------------
    def _start(self, d: DeployConfig, st: dict[str, Any], sha: str, now: float) -> list[Event]:
        att = st["attempts"]
        attempt = att["n"] + 1 if att["sha"] == sha else 1
        st["attempts"] = {"sha": sha, "n": attempt}
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = self.log_dir / f"{d.id}.log"
        rc_file = self.log_dir / f"{d.id}.{int(now)}.rc"
        rc_file.unlink(missing_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        log_path.write_text(
            f"# {stamp} deploy {d.id} {sha} (attempt {attempt})\n"
            f"# cwd {d.repo}\n# {shlex.join(d.command)}\n",
            encoding="utf-8",
        )
        env = {**os.environ, **dict(d.env), "AMS_DEPLOY_SHA": sha}
        argv = [sys.executable, "-m", "ams.ops.deploy", "--rc-file", str(rc_file), "--", *d.command]
        try:
            handle = self.spawn(argv, d.repo, env, log_path)
        except OSError as e:
            st["failed"] = sha
            return [self._failed(d, sha, f"启动失败：{type(e).__name__}: {e}", log_path)]
        self._handles[d.id] = handle
        st["blocked"] = None
        st["run"] = {
            "sha": sha,
            "pid": handle.pid,
            "started": now,
            "log": str(log_path),
            "rc_file": str(rc_file),
            "attempt": attempt,
        }
        log.info("deploy %s: started %s pid=%d attempt=%d", d.id, _short(sha), handle.pid, attempt)
        return []

    def _orphan_alive(self, run: dict[str, Any]) -> bool:
        """After a restart of ams watch: is the run's wrapper still alive (matched by its rc file,
        so a reused pid is never mistaken for it)?"""
        res = self.runner(["/bin/ps", "-o", "command=", "-p", str(run["pid"])], timeout=5)
        return res.ok and run["rc_file"] in res.out

    def _terminate(self, run: dict[str, Any], handle: Handle | None) -> None:
        pid = int(run["pid"])
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                self.kill(pid, sig)  # the wrapper leads its own process group
            except (ProcessLookupError, PermissionError):
                return
            waited = 0.0
            while waited < KILL_GRACE_S:
                if (handle.poll() is not None) if handle else not self._orphan_alive(run):
                    return
                self.sleep(1)
                waited += 1

    def _check_run(self, d: DeployConfig, st: dict[str, Any], now: float) -> list[Event]:
        run = st["run"]
        handle = self._handles.get(d.id)
        running = handle.poll() is None if handle is not None else self._orphan_alive(run)
        sha, log_path = run["sha"], Path(run["log"])
        if running:
            if now - run["started"] <= d.timeout_s:
                return []
            log.error("deploy %s: %s timed out after %.0fs", d.id, _short(sha), d.timeout_s)
            self._terminate(run, handle)
            self._handles.pop(d.id, None)
            st["run"], st["failed"] = None, sha
            return [self._failed(d, sha, f"超过 {int(d.timeout_s // 60)} 分钟，已终止", log_path)]
        self._handles.pop(d.id, None)
        st["run"] = None
        rc_path = Path(run["rc_file"])
        try:
            rc: int | None = int(rc_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            rc = None
        rc_path.unlink(missing_ok=True)
        took = int(now - run["started"])
        if rc is None:
            if run["attempt"] < MAX_INTERRUPTED:
                log.warning("deploy %s: %s ended without an exit code; retrying", d.id, _short(sha))
                st["last_check"] = 0.0
                return []
            st["failed"] = sha
            return [self._failed(d, sha, f"连续 {run['attempt']} 次没有退出码就结束", log_path)]
        if rc == 0:
            st["deployed"], st["failed"] = sha, None
            log.info("deploy %s: %s deployed in %ds", d.id, _short(sha), took)
            subject = self._subject(d, sha)
            return [
                Event(
                    d.id,
                    "deployed",
                    "info",
                    f"{d.id} deployed",
                    f"{_short(sha)} 已部署（{subject or '无提交说明'}），"
                    f"用时 {took // 60} 分 {took % 60} 秒",
                    sha,
                )
            ]
        st["failed"] = sha
        log.error("deploy %s: %s failed rc=%d after %ds", d.id, _short(sha), rc, took)
        return [self._failed(d, sha, f"退出码 {rc}，用时 {took // 60} 分", log_path)]

    def _failed(self, d: DeployConfig, sha: str, why: str, log_path: Path) -> Event:
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        tail = [sanitize(ln, 200) for ln in lines[-LOG_TAIL_LINES:] if ln.strip()]
        subject = self._subject(d, sha)
        text = "\n".join(
            [
                f"{_short(sha)}（{subject or '无提交说明'}）部署失败：{why}",
                *[f"| {ln}" for ln in tail],
                f"完整日志：{log_path}",
                "这个提交不会再自动重试；修好后推到 "
                f"{d.branch}，新提交会自动部署。正在运行的还是上一个版本。",
            ]
        )
        return Event(d.id, "deploy_failed", "error", f"{d.id} deploy failed", clip(text, 7800), sha)

    # ---- status -------------------------------------------------------------------------
    def status(self) -> dict[str, dict[str, Any]]:
        out = {}
        for d in self.cfg.deploys:
            st = self.state.get(d.id, {})
            run = st.get("run") or {}
            out[d.id] = {
                "deployed": st.get("deployed"),
                "failed": st.get("failed"),
                "blocked": st.get("blocked"),
                "running": run.get("sha"),
                "last_check": st.get("last_check"),
            }
        return out


def _leave_background_band() -> None:
    """A LaunchAgent with ``ProcessType = Background`` (as ``ams watch`` is) runs in Darwin's
    background band, and its children inherit it: a build would get throttled CPU and I/O.
    Leave the band for this process, so the command and everything it starts run at normal
    priority (what ``taskpolicy -B`` does)."""
    prio = getattr(os, "PRIO_DARWIN_PROCESS", None)
    if prio is None:
        return
    try:
        if os.getpriority(prio, 0):
            os.setpriority(prio, 0, 0)
            print("ams deploy: left the background band", flush=True)
    except OSError as e:
        print(f"ams deploy: still in the background band ({e})", flush=True)


def main(argv: list[str] | None = None) -> int:
    """The wrapper: run the command, then write its exit code (atomically) to ``--rc-file``."""
    p = argparse.ArgumentParser(prog="python -m ams.ops.deploy")
    p.add_argument("--rc-file", type=Path, required=True)
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        p.error("no command")
    _leave_background_band()
    try:
        rc = subprocess.run(cmd, stdin=subprocess.DEVNULL, check=False).returncode  # noqa: S603
    except OSError as e:
        print(f"ams deploy: cannot run {cmd[0]}: {e}", file=sys.stderr, flush=True)
        rc = 127
    tmp = args.rc_file.with_suffix(".tmp")
    tmp.write_text(f"{rc}\n", encoding="utf-8")
    os.replace(tmp, args.rc_file)
    return rc


if __name__ == "__main__":
    sys.exit(main())
