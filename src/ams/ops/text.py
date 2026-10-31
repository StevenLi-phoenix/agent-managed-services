"""Text hygiene for everything that crosses a trust boundary.

Log lines are written by the services (and their dependencies), so they are data: they are
redacted before they reach the LLM or the notification channel, stripped of control characters,
and reduced to a stable *signature* so that one cause is one cause however often it prints.
"""

from __future__ import annotations

import json
import re

from ams.platform.policy import cause_key

# C0/C1 controls (ESC, BEL, CR ...) and DEL; tabs and newlines become spaces.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SPACE_RE = re.compile(r"[\t\n]+")

# Order matters: the specific shapes first, the generic ones last.
_SECRET_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <redacted>"),
    (
        re.compile(r"\b(?:llmgw|pat|sk|ghp|gho|ghs|github_pat|xox[abp])[_-][A-Za-z0-9_-]{8,}"),
        "<redacted>",
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "<redacted-jwt>"),
    (
        re.compile(
            r"(?i)\b(token|key|secret|password|passwd|sessdata|bili_jct|sid|psid|psidts|auth|"
            r"authorization|cookie|api[_-]?key)(\s*[=:]\s*)(?!<redacted)[^\s&;,\"'}]{4,}"
        ),
        r"\1\2<redacted>",
    ),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<redacted-hex>"),
)

# Identifiers that vary per occurrence but cause_key() does not collapse.
_ID_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bUC[\w-]{22}\b"), "<yt-channel>"),
    (re.compile(r"(?<=\[youtube\] )[\w-]{11}(?=:)"), "<yt-id>"),
    (re.compile(r"(?<=\bv=)[\w-]{11}\b"), "<yt-id>"),
    (re.compile(r"\bUC\w{3,}\b"), "<yt-channel>"),
    # retry counters: "after 4 attempts", "(attempt 1/4)", "retrying in 0.3s"
    (re.compile(r"(?i)\b(attempts?|after|retrying in)\s+\d+(?:/\d+)?(?:\.\d+)?"), r"\1 N"),
)


def strip_controls(text: str) -> str:
    return _CONTROL_RE.sub("", _SPACE_RE.sub(" ", text))


def redact(text: str) -> str:
    out = text
    for pattern, replacement in _SECRET_RULES:
        out = pattern.sub(replacement, out)
    return out


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def sanitize(text: str, limit: int = 400) -> str:
    """What may be shown to a model or a human: no controls, no secrets, bounded."""
    return clip(redact(strip_controls(text)), limit)


def _json_signature(service: str, text: str) -> str | None:
    """Structured logs: the cause is (level, msg, source), never the numbers inside."""
    if not text.lstrip().startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("msg"), str):
        return None
    where = next((str(obj[k]) for k in ("src", "source", "component") if obj.get(k)), "")
    msg = _normalize_msg(obj["msg"])
    return f"{service}|json|{obj.get('level', '')}|{msg}|{where}"[:300]


def _normalize_msg(msg: str) -> str:
    return re.sub(r"\d+", "N", msg)


def signature(service: str, text: str) -> str:
    """Stable identity of a log line's cause (ids, numbers and timestamps collapsed)."""
    structured = _json_signature(service, text)
    if structured is not None:
        return structured
    pre = text
    for pattern, replacement in _ID_RULES:
        pre = pattern.sub(replacement, pre)
    return cause_key(service, "log", pre)
