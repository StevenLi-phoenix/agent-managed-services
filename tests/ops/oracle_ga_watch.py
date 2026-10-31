# Frozen copy of the operator's ga-watch (the DSH Monitor on the AMS escalation journal) as it ran
# until 2026-10-31. It is the oracle of tests/ops/test_ops_journal.py: `ams watch journal` with the
# global-awareness rules must emit exactly what this emitted. Do not edit; not collected by pytest.
"""Watch global-awareness AMS escalations and print only lines worth an agent's judgement.

Rules (tuned against the existing 34-line baseline in escalations.jsonl):
  * routine/normal noise is suppressed (item-filter stats, stream reconnects,
    admin-open config warning, missing-key sources the owner already knows about)
  * a genuinely new or long-absent problem is always printed once
  * a flapping problem is printed at most once per 6h with a repeat count
  * service health is polled; going down / restart storms print immediately,
    and keep printing once every 30 min while it stays down
"""

import json
import os
import subprocess
import sys
import threading
import time

LOG = "/Users/dsh-runtime/.ams/logs/escalations.jsonl"
STATE = "/Users/dsh-runtime/work/ga-watch/state.json"

# owner-declared known-missing-key sources (match by prefix: openaq-latest, openaq-cities, ...)
KNOWN_MISSING_KEY = ("cloudflare-radar", "windy-webcams", "openaq")
IGNORE_WARN_MSGS = {
    "dropped invalid upstream items",
    "stream ended by upstream",
    # GDELT names a slot in lastupdate.txt before the zip is on the CDN; the
    # source reads older slots and next run picks it up (documented behaviour,
    # run still ended in "source ok"). A real outage surfaces as "source failed".
    "upstream http error",
    # same GDELT CDN lag, newer wording: walks back a slot and still ends in
    # "source ok"; a real outage shows up as "source failed", which is not ignored.
    "gdelt-geo: newest slots all unavailable, walking back",
}
IGNORE_WARN_PREFIXES = ("gdelt-geo: slot unavailable",)
IGNORE_RAW_PREFIXES = ("repeated ",)  # AMS's own "N times in window" rollup
IGNORE_WARN_SUBSTR = ("ADMIN_OPEN=1",)

NEW_WINDOW = 12 * 3600  # same signature silent this long => treat as new again
FLAP_WINDOW = 3600  # count occurrences inside this window
FLAP_COUNT = 5  # >= this many in FLAP_WINDOW => flapping
FLAP_COOLDOWN = 6 * 3600  # at most one flap report per this period

lock = threading.Lock()


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"sig": {}}


state = load_state()
state.setdefault("sig", {})
state.setdefault("bootstrapped", False)


def save_state():
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def now():
    return time.time()


def classify(obj, raw):
    """Return (signature, severity, summary) or (None, ...) when suppressed."""
    if raw.startswith(IGNORE_RAW_PREFIXES):
        return None, None, None
    if not raw.startswith("{"):
        return ("raw", raw[:120]), "unknown", raw[:300]
    level = obj.get("level")
    msg = obj.get("msg", "")
    src = obj.get("src") or ""
    if level == "warn":
        if msg in IGNORE_WARN_MSGS:
            return None, None, None
        if msg.startswith(IGNORE_WARN_PREFIXES):
            return None, None, None
        if any(s in msg for s in IGNORE_WARN_SUBSTR):
            return None, None, None
    if msg == "source failed" and src.startswith(KNOWN_MISSING_KEY):
        err = str(obj.get("error", ""))
        if "unset" in err or "KEY" in err or "TOKEN" in err:
            return None, None, None
    err = obj.get("error")
    if isinstance(err, dict):
        err = err.get("message", "")
    detail = f"{msg}" + (f" src={src}" if src else "") + (f" err={str(err)[:220]}" if err else "")
    if "cron" in obj:
        detail += f" cron={obj['cron']}"
    return (level, msg, src), level, detail


_QUIET = False


def emit(text):
    if _QUIET:
        return
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def handle(line):
    raw = line.strip()
    if not raw:
        return
    try:
        rec = json.loads(raw)
        text = rec.get("event", {}).get("text", "")
        reason = rec.get("reason", "")
    except Exception:
        return
    try:
        obj = json.loads(text) if text.lstrip().startswith("{") else {}
    except Exception:
        obj = {}
    sig, level, detail = classify(obj, text)
    if sig is None:
        return
    key = "|".join(map(str, sig))
    t = now()
    with lock:
        entry = state["sig"].get(key)
        first_sight = entry is None
        if entry is None:
            entry = {
                "first": t,
                "last_seen": t,
                "count": 1,
                "hist": [t],
                "last_emit": 0,
                "base": False,
            }
            state["sig"][key] = entry
            kind = "NEW"
        else:
            gap = t - entry.get("last_seen", t)
            hist = [h for h in entry["hist"] if t - h < FLAP_WINDOW]
            hist.append(t)
            entry["hist"] = hist
            entry["count"] = len(hist)
            entry["last_seen"] = t
            if gap > NEW_WINDOW:
                kind = "BACK"
            elif len(hist) >= FLAP_COUNT and t - entry["last_emit"] > FLAP_COOLDOWN:
                kind = "FLAP"
            else:
                save_state()
                return
        entry["last_emit"] = t
        count_now = len(entry["hist"])
        save_state()
    if first_sight and entry.get("base"):
        return
    tag = {"NEW": "新问题", "BACK": "旧问题重现", "FLAP": "反复出现"}[kind]
    extra = f" 近1h出现{count_now}次" if kind == "FLAP" else ""
    emit(f"[{tag}] level={level} {detail} (AMS: {reason}){extra}")


def bootstrap():
    """Seed signatures from what is already on disk, without emitting."""
    global _QUIET
    _QUIET = True
    try:
        with open(LOG, "rb") as f:
            data = f.read()
        for line in data.decode("utf-8", "replace").splitlines():
            handle(line)
    finally:
        _QUIET = False
    for entry in state["sig"].values():
        entry["last_emit"] = 0
        entry["base"] = True
        entry["hist"] = [entry.get("last_seen", now())]
        entry["count"] = 1
    save_state()
    return len(data)


def tail_loop():
    offset = bootstrap()
    buf = ""
    while True:
        time.sleep(1.0)
        try:
            size = os.path.getsize(LOG)
            if size < offset:  # rotated / truncated
                offset = 0
                buf = ""
            if size == offset:
                continue
            with open(LOG, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
                offset = f.tell()
            buf += chunk.decode("utf-8", "replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                handle(line)
        except FileNotFoundError:
            continue
        except Exception:
            continue


def health_loop():
    """Poll AMS service health; report down/restart-storm, not steady state."""
    down_since = None
    last_emit = 0.0
    seen_healthy_once = False
    env = dict(os.environ, AMS_STATE_DIR="/Users/dsh-runtime/.ams")
    while True:
        time.sleep(300)
        try:
            out = subprocess.run(
                ["/Users/dsh-runtime/ams/.venv/bin/ams", "ctl", "status", "global-awareness"],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            ).stdout
            st = json.loads(out)["services"]["global-awareness"]
        except Exception as e:
            nowt = now()
            with lock:
                if nowt - last_emit < 1800:
                    continue
                last_emit = nowt
            emit(f"[服务查询失败] ams ctl status 出错: {type(e).__name__}: {e}")
            continue

        nowt = now()
        running = st.get("status") == "running"
        healthy = bool(st.get("healthy"))
        uptime = st.get("uptime_s") or 0
        fails = st.get("consecutive_failures")
        if running and healthy:
            down_since = None
            if uptime < 300 and seen_healthy_once:
                with lock:
                    if nowt - last_emit < 1800:
                        continue
                    last_emit = nowt
                emit(
                    f"[服务刚重启] global-awareness 已恢复，uptime {int(uptime)}s，连续失败 {fails}"
                )
            seen_healthy_once = True
        else:
            if down_since is None:
                down_since = nowt
            with lock:
                if last_emit and nowt - last_emit < 1800:
                    continue
                last_emit = nowt
            emit(
                f"[服务异常] status={st.get('status')} healthy={healthy} "
                f"consecutive_failures={fails} 已持续 {int(nowt - down_since)}s"
            )


def main():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    threading.Thread(target=health_loop, daemon=True).start()
    tail_loop()


if __name__ == "__main__":
    main()
