"""A restricted YAML parser, big enough for an api ``service.yaml`` and no bigger.

ams is stdlib-only (D1) and the platform layer has to read manifests written for
PyYAML. Rather than vendor a YAML implementation, this module parses the subset
the 21 manifests actually use and **raises on everything else**. Guessing at a
construct we did not implement is how a manifest silently deploys the wrong
thing (PLAN-allin risk 1), so every unsupported construct raises
``YamlSubsetError`` naming the line and the construct.

Supported
    block mappings, block sequences (``- item`` and ``- {k: v}``), flow
    sequences ``[a, b]`` and flow mappings ``{k: v}`` (nestable), ``#``
    comments, plain scalars, single- and double-quoted scalars (the only way to
    span lines), ``true``/``false``, ``null``/``~``, decimal ints and floats.

Rejected, by design
    anchors ``&a`` / aliases ``*a``, tags ``!t``, merge keys ``<<``, multiple
    documents (``---`` / ``...``), block scalars (``|`` / ``>``), tab
    indentation, duplicate mapping keys, and the YAML 1.1 boolean words
    ``yes``/``no``/``on``/``off``/``y``/``n``.

That last one deserves a note: PyYAML resolves a bare ``no`` to ``False``, so
``restart: no`` -- legal per the deployer's own JSON schema -- already reaches
the deployer as a boolean and fails its enum check. Rejecting the word and
demanding ``"no"`` is both safer and strictly more correct than either
behaviour, so the parser refuses it rather than picking a side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["YamlSubsetError", "parse"]


class YamlSubsetError(ValueError):
    """A construct outside the supported YAML subset. Message names line + construct."""


def _err(line_no: int, msg: str) -> YamlSubsetError:
    return YamlSubsetError(f"line {line_no}: {msg}")


# Plain scalars that PyYAML (YAML 1.1) resolves to something other than a string.
_TRUE = frozenset({"true", "True", "TRUE"})
_FALSE = frozenset({"false", "False", "FALSE"})
_NULL = frozenset({"null", "Null", "NULL", "~", ""})
_AMBIGUOUS_BOOL = frozenset(
    {"yes", "Yes", "YES", "no", "No", "NO", "on", "On", "ON"}
    | {"off", "Off", "OFF", "y", "Y", "n", "N"}
)
_INT_RE = re.compile(r"^[-+]?(0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^[-+]?(0|[1-9][0-9]*)\.[0-9]+$")
# Numeric-looking forms YAML 1.1 resolves in ways this parser deliberately does
# not implement (octal, underscores, sexagesimal, exponents, .inf/.nan).
_SUSPECT_NUMERIC_RE = re.compile(
    r"^[-+]?(0[0-9_]+|0[xXoObB][0-9a-fA-F_]+|[0-9][0-9_]*[:.eE][0-9a-zA-Z_:.+-]*|\.(inf|Inf|INF|nan|NaN|NAN))$"
)
_INDICATORS = "&*!|>%@`"


@dataclass(frozen=True)
class _Line:
    no: int  # 1-based line number in the original text
    indent: int
    text: str  # comment-stripped, right-stripped content; never empty


# --------------------------------------------------------------------------- lexing


def _split_comment(raw: str, start: int = 0) -> tuple[str, str | None]:
    """Return ``(content, open_quote)``.

    ``content`` is ``raw`` with any trailing ``#`` comment removed. ``open_quote``
    is the quote character still open at end of line (the value continues on the
    next physical line), or ``None``.
    """
    quote: str | None = None
    i = start
    while i < len(raw):
        ch = raw[i]
        if quote == '"':
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                quote = None
        elif quote == "'":
            if ch == "'":
                if i + 1 < len(raw) and raw[i + 1] == "'":
                    i += 2
                    continue
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or raw[i - 1] in " \t"):
            return raw[:i], None
        i += 1
    return raw, quote


def _lex(text: str) -> list[_Line]:
    raw_lines = text.split("\n")
    out: list[_Line] = []
    i = 0
    while i < len(raw_lines):
        raw = raw_lines[i].rstrip("\r")
        line_no = i + 1
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise _err(line_no, "tab in indentation; the subset requires spaces")
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if stripped.rstrip() in ("---", "..."):
            raise _err(line_no, "multi-document stream (--- / ...) is not supported")
        content, open_quote = _split_comment(raw, indent)
        # A quoted scalar may span lines; nothing else may.
        while open_quote is not None:
            i += 1
            if i >= len(raw_lines):
                raise _err(line_no, f"unterminated {open_quote} quoted scalar")
            cont = raw_lines[i].rstrip("\r")
            # YAML folds a line break inside a flow scalar into one space.
            joined = content.rstrip() + " " + cont.strip()
            content, open_quote = _split_comment(joined, indent)
        content = content[indent:].rstrip()
        if content:
            out.append(_Line(no=line_no, indent=indent, text=content))
        i += 1
    return out


# --------------------------------------------------------------------------- scalars


def _reject_indicators(line_no: int, raw: str) -> None:
    head = raw.lstrip()
    if not head:
        return
    ch = head[0]
    if ch in _INDICATORS:
        names = {
            "&": "anchor",
            "*": "alias",
            "!": "tag",
            "|": "literal block scalar",
            ">": "folded block scalar",
            "%": "directive",
            "@": "reserved indicator",
            "`": "reserved indicator",
        }
        raise _err(line_no, f"{names[ch]} ({ch!r}) is not supported")


def _plain_scalar(line_no: int, raw: str) -> Any:
    text = raw.strip()
    if text in _NULL:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if text in _AMBIGUOUS_BOOL:
        raise _err(
            line_no,
            f'bare {text!r} is a YAML 1.1 boolean; quote it ("{text}") to mean the string',
        )
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    if _SUSPECT_NUMERIC_RE.match(text):
        raise _err(line_no, f"ambiguous numeric scalar {text!r}; quote it to mean the string")
    _reject_indicators(line_no, text)
    return text


_DQ_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
}


def _unquote(line_no: int, raw: str) -> str:
    quote, body = raw[0], raw[1:-1]
    if quote == "'":
        return body.replace("''", "'")
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(body):
            raise _err(line_no, "trailing backslash in double-quoted scalar")
        esc = body[i]
        if esc in _DQ_ESCAPES:
            out.append(_DQ_ESCAPES[esc])
            i += 1
        elif esc in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[esc]
            digits = body[i + 1 : i + 1 + width]
            if len(digits) != width or any(c not in "0123456789abcdefABCDEF" for c in digits):
                raise _err(line_no, rf"bad \{esc} escape in double-quoted scalar")
            out.append(chr(int(digits, 16)))
            i += 1 + width
        else:
            raise _err(line_no, rf"unsupported escape \{esc} in double-quoted scalar")
    return "".join(out)


def _scalar(line_no: int, raw: str) -> Any:
    text = raw.strip()
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        # Guard against `'a' 'b'`: a quoted scalar must be the whole value.
        inner_end = _find_quote_end(line_no, text, 0)
        if inner_end != len(text) - 1:
            raise _err(line_no, f"trailing content after a quoted scalar: {text!r}")
        return _unquote(line_no, text)
    if text and text[0] in "\"'":
        raise _err(line_no, f"unterminated quoted scalar: {text!r}")
    if text.startswith("{") or text.startswith("["):
        value, end = _parse_flow(line_no, text, 0)
        if text[end:].strip():
            raise _err(line_no, f"trailing content after a flow collection: {text!r}")
        return value
    if "<<" == text:
        raise _err(line_no, "merge key (<<) is not supported")
    return _plain_scalar(line_no, text)


def _find_quote_end(line_no: int, s: str, start: int) -> int:
    quote = s[start]
    i = start + 1
    while i < len(s):
        ch = s[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if quote == "'" and i + 1 < len(s) and s[i + 1] == "'":
                i += 2
                continue
            return i
        i += 1
    raise _err(line_no, f"unterminated {quote} quoted scalar")


# --------------------------------------------------------------------------- flow


def _parse_flow(line_no: int, s: str, i: int) -> tuple[Any, int]:
    if s[i] == "[":
        return _parse_flow_seq(line_no, s, i)
    if s[i] == "{":
        return _parse_flow_map(line_no, s, i)
    raise _err(line_no, "expected a flow collection")


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


def _parse_flow_scalar(line_no: int, s: str, i: int, stop: str) -> tuple[Any, int]:
    i = _skip_ws(s, i)
    if i >= len(s):
        raise _err(line_no, "unterminated flow collection")
    if s[i] in "\"'":
        end = _find_quote_end(line_no, s, i)
        return _unquote(line_no, s[i : end + 1]), end + 1
    if s[i] in "[{":
        return _parse_flow(line_no, s, i)
    j = i
    while j < len(s) and s[j] not in stop:
        j += 1
    return _plain_scalar(line_no, s[i:j]), j


def _parse_flow_seq(line_no: int, s: str, i: int) -> tuple[list[Any], int]:
    i += 1  # past '['
    out: list[Any] = []
    while True:
        i = _skip_ws(s, i)
        if i >= len(s):
            raise _err(line_no, "unterminated flow sequence")
        if s[i] == "]":
            return out, i + 1
        value, i = _parse_flow_scalar(line_no, s, i, ",]")
        out.append(value)
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1
        elif i < len(s) and s[i] == "]":
            return out, i + 1
        else:
            raise _err(line_no, "expected ',' or ']' in flow sequence")


def _parse_flow_map(line_no: int, s: str, i: int) -> tuple[dict[str, Any], int]:
    i += 1  # past '{'
    out: dict[str, Any] = {}
    while True:
        i = _skip_ws(s, i)
        if i >= len(s):
            raise _err(line_no, "unterminated flow mapping")
        if s[i] == "}":
            return out, i + 1
        key, i = _parse_flow_scalar(line_no, s, i, ":,}")
        if not isinstance(key, str):
            raise _err(line_no, f"flow mapping key {key!r} must be a string")
        i = _skip_ws(s, i)
        if i >= len(s) or s[i] != ":":
            raise _err(line_no, f"expected ':' after flow mapping key {key!r}")
        value, i = _parse_flow_scalar(line_no, s, i + 1, ",}")
        if key in out:
            raise _err(line_no, f"duplicate key {key!r} in flow mapping")
        out[key] = value
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1
        elif i < len(s) and s[i] == "}":
            return out, i + 1
        else:
            raise _err(line_no, "expected ',' or '}' in flow mapping")


# --------------------------------------------------------------------------- block


def _split_key(line: _Line) -> tuple[str, str] | None:
    """Split ``key: rest``; ``None`` when the line is not a mapping entry."""
    s = line.text
    i = 0
    if s[0] in "\"'":
        end = _find_quote_end(line.no, s, 0)
        key = _unquote(line.no, s[: end + 1])
        i = end + 1
        if i >= len(s) or s[i] != ":":
            raise _err(line.no, "expected ':' after a quoted mapping key")
        return key, s[i + 1 :].strip()
    while i < len(s):
        ch = s[i]
        if ch in "\"'":
            i = _find_quote_end(line.no, s, i) + 1
            continue
        if ch in "[{":
            return None  # a flow collection, not a mapping entry
        if ch == ":" and (i + 1 == len(s) or s[i + 1] in " \t"):
            return s[:i].strip(), s[i + 1 :].strip()
        i += 1
    return None


class _Parser:
    def __init__(self, lines: list[_Line]) -> None:
        self.lines = lines
        self.i = 0

    def at_end(self) -> bool:
        return self.i >= len(self.lines)

    def peek(self) -> _Line:
        return self.lines[self.i]

    def parse_node(self, indent: int) -> Any:
        line = self.peek()
        if line.text == "-" or line.text.startswith("- "):
            return self.parse_seq(indent)
        return self.parse_map(indent)

    def parse_seq(self, indent: int) -> list[Any]:
        out: list[Any] = []
        while not self.at_end():
            line = self.peek()
            if line.indent != indent:
                if line.indent < indent:
                    break
                raise _err(line.no, "unexpected indentation inside a block sequence")
            if not (line.text == "-" or line.text.startswith("- ")):
                break
            rest = line.text[1:].strip()
            self.i += 1
            if not rest:
                out.append(self.parse_child(indent, line.no))
                continue
            # `- key: value` starts a nested mapping whose remaining keys sit
            # at the column `rest` starts in, not at a fixed offset.
            dash_gap = len(line.text) - 1 - len(line.text[1:].lstrip(" "))
            rest_col = line.indent + 1 + dash_gap
            item_line = _Line(no=line.no, indent=rest_col, text=rest)
            if _split_key(item_line) is not None:
                sub = _Parser([item_line, *self.lines[self.i :]])
                out.append(sub.parse_map(rest_col))
                self.i += sub.i - 1
            else:
                out.append(_scalar(line.no, rest))
        return out

    def parse_map(self, indent: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while not self.at_end():
            line = self.peek()
            if line.indent < indent:
                break
            if line.indent > indent:
                raise _err(line.no, "unexpected indentation inside a block mapping")
            if line.text == "-" or line.text.startswith("- "):
                break
            kv = _split_key(line)
            if kv is None:
                raise _err(line.no, f"expected 'key: value', got {line.text!r}")
            key, rest = kv
            if key.startswith("<<"):
                raise _err(line.no, "merge key (<<) is not supported")
            _reject_indicators(line.no, key)
            if key in out:
                raise _err(line.no, f"duplicate mapping key {key!r}")
            self.i += 1
            if rest:
                _reject_indicators(line.no, rest)
                out[key] = _scalar(line.no, rest)
            else:
                out[key] = self.parse_child(indent, line.no)
        return out

    def parse_child(self, indent: int, parent_line: int) -> Any:
        """The value of a ``key:`` (or ``-``) whose content is on later lines."""
        if self.at_end():
            return None
        nxt = self.peek()
        if nxt.indent > indent:
            return self.parse_node(nxt.indent)
        # A block sequence may sit at the parent key's own indentation.
        if nxt.indent == indent and (nxt.text == "-" or nxt.text.startswith("- ")):
            return self.parse_seq(indent)
        if nxt.indent < indent:
            return None
        return None


def parse(text: str) -> Any:
    """Parse ``text`` as the supported YAML subset. Raises ``YamlSubsetError``."""
    lines = _lex(text)
    if not lines:
        return None
    parser = _Parser(lines)
    base = lines[0].indent
    value = parser.parse_node(base)
    if not parser.at_end():
        raise _err(parser.peek().no, "unexpected content after the top-level node")
    return value
