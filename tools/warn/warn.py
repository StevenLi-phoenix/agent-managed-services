#!/usr/bin/env python3
"""warn: hand one warning to the operator through `ams intake` (POST /escalate).

Single file, standard library only, Python 3.8+. The operator that reads the AMS escalation journal
(DSH's ga-watch and agent) decides whether the owner hears about it; this tool only delivers. A failed delivery is kept in a local spool and
replayed by the next run, and the exit code is 0 unless --strict is given, so a monitoring hook that
calls warn can never break the thing it watches.

    warn --source n8 --severity error "disk full on /data" --text - < details.txt
    warn --curl          # print the equivalent curl one-liner

Token: ~/.config/warn/token (mode 0600). Endpoint: $WARN_URL, else the first line of
~/.config/warn/url (there is no default: the address of your intake belongs in your config, not in
this file). Timeout: $WARN_TIMEOUT s.
Exit codes: 0 done (or failed without --strict), 1 not delivered (--strict), 2 usage or token problem (--strict).
Usage errors (bad arguments) always exit 2.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TIMEOUT = 5.0
SEVERITIES = ("info", "warn", "error", "crit")
LIMITS = {"key": 200, "subject": 200, "title": 300, "text": 8000, "url": 2000}
SPOOL_MAX = 100          # newest entries kept
SPOOL_TTL = 7 * 86400    # older entries are dropped, not replayed
FLUSH_MAX = 20           # spool entries replayed per run
LOG_MAX_BYTES = 256 * 1024
PERMANENT = {400, 401, 403, 404, 405, 413, 415}  # replaying cannot help; 429 and 5xx can


def clean_origin(value):
    """Lowercase dotted words of [a-z0-9_-], the shape the intake accepts inside a kind."""
    words = [re.sub(r"[^a-z0-9_-]+", "-", w.lower()).strip("-") for w in (value or "").split(".")]
    words = [w for w in words if w]
    return ".".join(words) if words else "unknown"


def default_key(origin, title, now):
    """`<origin>-<sha1(title)[:10]>-<UTC yyyymmddhh>`: one alert text dedupes within the hour."""
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]
    return "%s-%s-%s" % (origin, digest, time.strftime("%Y%m%d%H", time.gmtime(now)))


def curl_line(url=None):
    url = url or "<intake url>"
    return ("curl -fsS -m 5 -H \"Authorization: Bearer $(cat ~/.config/warn/token)\" "
            "-H 'Content-Type: application/json' "
            "-d '{\"kind\":\"warn.<origin>.warn\",\"key\":\"<origin>-<unique>\",\"subject\":\"<origin>\","
            "\"title\":\"<one line>\"}' %s" % url)


def _line(value, limit):
    return re.sub(r"\s+", " ", value).strip()[:limit]


def build_event(origin, severity, title, key, subject, text, url, now):
    event = {
        "kind": ("warn.%s.%s" % (origin, severity))[:100],
        "key": _line(key or default_key(origin, title, now), LIMITS["key"]),
        "subject": _line(subject or origin, LIMITS["subject"]),
        "title": _line(title, LIMITS["title"]),
        "occurredAt": int(now * 1000),
    }
    if text:
        event["text"] = text.strip()[:LIMITS["text"]]
    if url and re.match(r"^https?://\S+$", url) and len(url) <= LIMITS["url"]:
        event["url"] = url
    return event


class Env:
    """Paths and settings derived from the (injectable) environment."""

    def __init__(self, env):
        home = env.get("HOME") or env.get("USERPROFILE") or str(Path.home())
        self.home = Path(home)
        self.url = env.get("WARN_URL") or None
        try:
            self.timeout = float(env.get("WARN_TIMEOUT") or DEFAULT_TIMEOUT)
        except ValueError:
            self.timeout = DEFAULT_TIMEOUT
        self.token_file = Path(env.get("WARN_TOKEN_FILE") or self.home / ".config" / "warn" / "token")
        if self.url is None and "WARN_URL" not in env:
            try:
                first = (self.home / ".config" / "warn" / "url").read_text(encoding="utf-8").split()
                self.url = first[0] if first else None
            except OSError:
                self.url = None
        self.state = self.home / ".local" / "state" / "warn"
        self.spool = self.state / "spool"
        self.log_file = self.state / "warn.log"


def log(cfg, level, message):
    """Append to the state log (rotated at 256 KB). Never raises."""
    try:
        cfg.state.mkdir(parents=True, exist_ok=True)
        if cfg.log_file.exists() and cfg.log_file.stat().st_size > LOG_MAX_BYTES:
            cfg.log_file.replace(cfg.state / "warn.log.1")
        with open(str(cfg.log_file), "a", encoding="utf-8") as fh:
            fh.write("%s %s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), level, message))
    except OSError:
        pass


def read_token(cfg, stderr):
    try:
        token = cfg.token_file.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        return None
    if os.name == "posix":
        try:
            if cfg.token_file.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                stderr.write("warn: token file permissions are open; run chmod 600 %s\n" % cfg.token_file)
        except OSError:
            pass
    return token


def post(cfg, token, event):
    """Return (kind, detail): ok / permanent / transient, plus the outcome or a reason."""
    body = json.dumps(event, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(cfg.url, data=body, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": "warn/1",
    })
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            data = resp.read(4096)
            try:
                return "ok", json.loads(data.decode("utf-8")).get("outcome", "accepted")
            except ValueError:
                return "ok", "accepted"
    except urllib.error.HTTPError as err:
        err.close()
        kind = "permanent" if err.code in PERMANENT else "transient"
        return kind, "HTTP %d" % err.code
    except (urllib.error.URLError, OSError, ValueError) as err:
        return "transient", "%s" % getattr(err, "reason", err)


def spool_save(cfg, event):
    cfg.spool.mkdir(parents=True, exist_ok=True)
    name = "%013d-%s.json" % (event["occurredAt"], hashlib.sha1(event["key"].encode("utf-8")).hexdigest()[:8])
    path = cfg.spool / name
    path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    entries = sorted(cfg.spool.glob("*.json"))
    for old in entries[:-SPOOL_MAX]:
        old.unlink()
        log(cfg, "WARN", "spool full, dropped %s" % old.name)


def spool_flush(cfg, token, now):
    """Replay older events. Returns False when delivery still fails (the endpoint looks down)."""
    if not cfg.spool.exists():
        return True
    sent = 0
    for path in sorted(cfg.spool.glob("*.json")):
        try:
            if now - path.stat().st_mtime > SPOOL_TTL:
                path.unlink()
                log(cfg, "WARN", "spool entry expired, dropped %s" % path.name)
                continue
            event = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(event, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            try:
                path.unlink()
            except OSError:
                pass
            log(cfg, "WARN", "spool entry unreadable, removed %s" % path.name)
            continue
        if sent >= FLUSH_MAX:
            return True
        kind, detail = post(cfg, token, event)
        if kind == "transient":
            log(cfg, "WARN", "spool replay failed (%s), endpoint still down" % detail)
            return False
        path.unlink()
        sent += 1
        log(cfg, "INFO" if kind == "ok" else "WARN", "spool replay %s key=%s: %s" % (kind, event.get("key"), detail))
    return True


def main(argv=None, env=None, stdin=None, stdout=None, stderr=None, now=None):
    env = os.environ if env is None else env
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    now = time.time() if now is None else now
    cfg = Env(env)

    parser = argparse.ArgumentParser(prog="warn", description="Hand one warning to the operator through ams intake.")
    parser.add_argument("title", nargs="?", help="one-line description of what is wrong")
    parser.add_argument("--source", default=None, help="who is warning (host or program), e.g. n8, phm.nginx")
    parser.add_argument("--severity", default="warn", help="info|warn|error|crit (default warn)")
    parser.add_argument("--key", help="dedupe key (default: <source>-<title hash>-<UTC hour>)")
    parser.add_argument("--subject", help="what it is about (default: the source)")
    parser.add_argument("--url", help="link with more detail")
    parser.add_argument("--text", help="longer detail; '-' reads stdin (untrusted data to the operator)")
    parser.add_argument("--strict", action="store_true", help="exit non-zero when the warning was not delivered")
    parser.add_argument("--print", dest="print_only", action="store_true", help="print the event JSON and do not send")
    parser.add_argument("--curl", action="store_true", help="print the equivalent curl one-liner and exit")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)

    if args.curl:
        stdout.write(curl_line(cfg.url) + "\n")
        return 0
    if args.severity not in SEVERITIES:
        stderr.write("warn: severity must be one of %s\n" % "|".join(SEVERITIES))
        return 2
    title = _line(args.title or "", LIMITS["title"])
    if not title:
        stderr.write("warn: a title is required\n")
        return 2
    origin = clean_origin(args.source or env.get("WARN_SOURCE") or "unknown")
    text = stdin.read() if args.text == "-" else args.text
    event = build_event(origin, args.severity, title, args.key, args.subject, text, args.url, now)

    if args.print_only:
        stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        return 0

    if cfg.url is None:
        stderr.write("warn: no endpoint: set $WARN_URL or write it to ~/.config/warn/url\n")
        log(cfg, "ERROR", "no endpoint configured; event key=%s not sent" % event["key"])
        return 2 if args.strict else 0
    token = read_token(cfg, stderr)
    if token is None:
        stderr.write("warn: no token in %s\n" % cfg.token_file)
        log(cfg, "ERROR", "no token in %s; event key=%s not sent" % (cfg.token_file, event["key"]))
        return 2 if args.strict else 0

    endpoint_up = spool_flush(cfg, token, now)
    if endpoint_up:
        kind, detail = post(cfg, token, event)
    else:
        kind, detail = "transient", "endpoint down (spool replay failed)"

    if kind == "ok":
        log(cfg, "INFO", "sent kind=%s key=%s: %s" % (event["kind"], event["key"], detail))
        stdout.write("warn: %s %s\n" % (detail, event["key"]))
        return 0
    if kind == "transient":
        spool_save(cfg, event)
        log(cfg, "WARN", "spooled kind=%s key=%s: %s" % (event["kind"], event["key"], detail))
        stderr.write("warn: not delivered (%s); spooled for the next run\n" % detail)
    else:
        log(cfg, "ERROR", "rejected kind=%s key=%s: %s" % (event["kind"], event["key"], detail))
        stderr.write("warn: rejected (%s); not spooled\n" % detail)
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
