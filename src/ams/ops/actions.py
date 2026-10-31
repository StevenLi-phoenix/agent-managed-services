"""The only things auto-ops may *do*: a closed list, each with guards, a cooldown and caps.

The LLM never emits a command. It names an action from the list; this module decides whether it
runs. Changing code is not on the list -- that stays a diagnosis plus advice for a human.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ams.ops import freeze as freeze_mod
from ams.ops.config import Config, ServiceConfig
from ams.ops.observe import health
from ams.ops.observe.launchd import read_job
from ams.ops.runner import Runner
from ams.ops.runner import run as default_run

log = logging.getLogger("ams.ops.actions")

#: Labels that are never kickstarted, whatever the configuration says: a hard kill loses the
#: in-flight subtitle burn (one ffmpeg re-render is tens of minutes). soft_restart_kaorou drains.
NEVER_KICKSTART_SUFFIXES = (".kaorou.daemon",)


@dataclass(frozen=True)
class ActionRequest:
    name: str
    service: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ActionResult:
    name: str
    service: str
    executed: bool
    ok: bool
    detail: str
    refused: str = ""  # why it did not run (empty when it ran)

    def line(self) -> str:
        if self.refused:
            return f"未执行 {self.name}：{self.refused}"
        return f"已执行 {self.name}：{'成功' if self.ok else '失败'}（{self.detail}）"


def kaorou_idle(cfg: Config, runner: Runner) -> tuple[bool, str]:
    """True only when it can be *shown* that no subtitle burn is in progress."""
    if runner(["/usr/bin/pgrep", "-x", "ffmpeg"], timeout=5).rc == 0:
        return False, "有 ffmpeg 进程在运行（烧录可能在进行）"
    jobs = health.get_json(cfg.kaorou.api.rstrip("/") + "/jobs", timeout=8)
    if not isinstance(jobs, dict) or not isinstance(jobs.get("jobs"), list):
        return False, "无法从烤肉 API 确认没有进行中的任务"
    busy = [j for j in jobs["jobs"] if isinstance(j, dict) and j.get("status") == "running"]
    if busy:
        stages = sorted({str(j.get("stage")) for j in busy})
        return False, f"有 {len(busy)} 个任务正在运行（阶段 {','.join(stages)}）"
    system = health.get_json(cfg.kaorou.api.rstrip("/") + "/system", timeout=8)
    daemon = system.get("daemon") if isinstance(system, dict) else None
    if isinstance(daemon, dict) and daemon.get("state") not in (None, "running"):
        return False, f"daemon 状态为 {daemon.get('state')}"
    return True, "idle"


class Executor:
    def __init__(
        self,
        cfg: Config,
        actions: list[dict[str, Any]],
        *,
        runner: Runner = default_run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg, self.history, self.runner, self.sleep = cfg, actions, runner, sleep

    # ---- policy -------------------------------------------------------------------------
    def check(
        self,
        req: ActionRequest,
        svc: ServiceConfig | None,
        freezes: dict[str, freeze_mod.Freeze],
        now: float,
    ) -> str:
        """Why ``req`` must not run (empty string = allowed)."""
        lim = self.cfg.limits
        if svc is None:
            return "未知服务"
        if req.name not in svc.actions:
            return f"{svc.id} 的允许动作里没有 {req.name}"
        fz = freeze_mod.lookup(freezes, svc.id)
        if fz is not None:
            return f"冻结中（{fz.reason}）"
        if req.name == "kickstart" and (svc.label or "").endswith(NEVER_KICKSTART_SUFFIXES):
            return "烤肉 daemon 不允许硬重启（会杀掉进行中的烧录），请用 soft_restart_kaorou"
        recent = [
            h
            for h in self.history
            if h["action"] == req.name and h["target"] == svc.id and h["outcome"] != "refused"
        ]
        if recent and now - recent[-1]["ts"] < lim.cooldown_s:
            return (
                f"冷却中（上次 {int(now - recent[-1]['ts'])} 秒前，冷却 {int(lim.cooldown_s)} 秒）"
            )
        today = [h for h in recent if now - h["ts"] < 86400]
        if len(today) >= lim.max_per_target_per_day:
            return f"{svc.id} 24 小时内已执行 {len(today)} 次，达到上限"
        hour = [h for h in self.history if now - h["ts"] < 3600 and h["outcome"] != "refused"]
        if len(hour) >= lim.max_per_hour:
            return f"全局 1 小时内已执行 {len(hour)} 次，达到上限"
        if req.name == "soft_restart_kaorou":
            ok, why = kaorou_idle(self.cfg, self.runner)
            if not ok:
                return f"保护规则：{why}"
        return ""

    # ---- execution ----------------------------------------------------------------------
    def execute(
        self, req: ActionRequest, freezes: dict[str, freeze_mod.Freeze], now: float | None = None
    ) -> ActionResult:
        now = time.time() if now is None else now
        svc = self.cfg.service(req.service)
        refused = self.check(req, svc, freezes, now)
        if not refused and self.cfg.mode == "dry-run":
            refused = "dry-run 模式"
        if refused:
            self._record(now, req, "refused", refused)
            log.info("action refused %s on %s: %s", req.name, req.service, refused)
            return ActionResult(req.name, req.service, False, False, "", refused)
        assert svc is not None
        try:
            ok, detail = getattr(self, f"_do_{req.name}")(svc, req)
        except Exception as e:  # an action bug must not take the loop down
            log.exception("action %s on %s crashed", req.name, req.service)
            ok, detail = False, f"{type(e).__name__}: {e}"
        self._record(now, req, "ok" if ok else "failed", detail)
        log.info("action %s on %s -> %s (%s)", req.name, req.service, ok, detail)
        return ActionResult(req.name, req.service, True, ok, detail)

    def _record(self, now: float, req: ActionRequest, outcome: str, detail: str) -> None:
        self.history.append(
            {
                "ts": now,
                "action": req.name,
                "target": req.service,
                "outcome": outcome,
                "detail": detail[:200],
            }
        )
        del self.history[:-200]

    def _do_kickstart(self, svc: ServiceConfig, req: ActionRequest) -> tuple[bool, str]:
        assert svc.launchd
        res = self.runner(
            ["/bin/launchctl", "kickstart", "-k", svc.launchd], timeout=self.cfg.limits.timeout_s
        )
        if not res.ok:
            return False, f"launchctl rc={res.rc}: {(res.err or res.out).strip()[:120]}"
        self.sleep(4)
        job = read_job(svc.launchd, self.runner)
        return job.running, f"launchctl ok; 之后 state={job.state or '未加载'} pid={job.pid}"

    def _do_soft_restart_kaorou(self, svc: ServiceConfig, req: ActionRequest) -> tuple[bool, str]:
        script = self.cfg.kaorou.env_script
        res = self.runner([str(script), "soft-restart"], timeout=self.cfg.limits.timeout_s)
        return res.ok, f"soft-restart rc={res.rc} {(res.out or res.err).strip()[:120]}"

    def _do_clean_tmp(self, svc: ServiceConfig, req: ActionRequest) -> tuple[bool, str]:
        cutoff = time.time() - self.cfg.clean_tmp.min_age_hours * 3600
        removed = freed = 0
        for root in self.cfg.clean_tmp.dirs:
            if (
                not root.is_absolute()
                or len(root.parts) < 4
                or root.is_symlink()
                or not root.is_dir()
            ):
                continue  # refuse "/", "/tmp", "~" ... : the directory must be a specific path
            for child in root.iterdir():
                try:
                    st = child.lstat()
                    if st.st_mtime > cutoff:
                        continue
                    if child.is_symlink() or child.is_file():
                        child.unlink()
                        freed += st.st_size
                    elif child.is_dir():
                        freed += sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
                        shutil.rmtree(child)
                    removed += 1
                except OSError as e:
                    log.warning("clean_tmp: cannot remove %s: %s", child, e)
        return True, f"删除 {removed} 项，释放 {freed // 1024} KiB"

    def _do_advertise_tailscale(self, svc: ServiceConfig, req: ActionRequest) -> tuple[bool, str]:
        want = list(self.cfg.tailscale.services)
        missing = missing_services(self.cfg, self.runner)
        if missing is None:
            return False, "读不到 tailscale prefs"
        done, bad = [], []
        for name in (n for n in want if n in missing):
            res = self.runner(
                [str(self.cfg.tailscale.binary), "serve", "advertise", name],
                timeout=self.cfg.limits.timeout_s,
            )
            (done if res.ok else bad).append(name)
        return not bad, f"advertise {done or '-'}; 失败 {bad or '-'}"


def missing_services(cfg: Config, runner: Runner = default_run) -> list[str] | None:
    """Configured Tailscale Services that the local node does not currently advertise."""
    import json

    res = runner([str(cfg.tailscale.binary), "debug", "prefs"], timeout=15)
    if not res.ok:
        return None
    try:
        adv = json.loads(res.out).get("AdvertiseServices") or []
    except ValueError:
        return None
    return [s for s in cfg.tailscale.services if s not in adv]
