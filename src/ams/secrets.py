"""Write-only secret store (DECISIONS D16).

A declaration lists secret *names* only (``secrets = ["SVC_SECRET"]``); the
values live here, in a harness-private tree under the state dir::

    <state>/secrets/            0700  harness
    <state>/secrets/<id>/       0700  harness
    <state>/secrets/<id>/NAME   0600  harness, contents = the raw value

Why the filesystem and not encryption at rest: the harness uid is deliberately
*not* mapped into a service's user namespace (D4), so a 0600 harness-owned file
is already unreadable from inside every service — verified by
``tests/linux/test_isolated.py``. An encryption key would have to sit on the same
host next to the ciphertext and would only defend against same-uid processes, so
it buys nothing today. The future path is ``systemd-creds`` /
``LoadCredentialEncrypted=`` on the unit; do not delete that option.

Write-only means exactly that: values enter through ``set`` (stdin or a file,
never argv, never a declaration) and leave only into a spawned service's
environment via :func:`make_extra_env_for`. Nothing in this module logs, prints
or reprs a value, and ``ams secret list`` shows names.

A service must not start with a declared-but-unset secret: ``load`` raises
:class:`MissingSecret`, the supervisor turns that into a spawn failure, and the
agent gets an escalation rather than a service running with a silently empty
credential.
"""

from __future__ import annotations

import argparse
import logging
import os
import stat
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ams.schema import ENV_NAME_RE, RESERVED_ENV, RESERVED_ENV_PREFIXES, SERVICE_ID_RE
from ams.state import StateDir

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ams.schema import ServiceDecl

log = logging.getLogger("ams.secrets")

EXIT_OK = 0
EXIT_ERROR = 1

DIR_MODE = 0o700
FILE_MODE = 0o600
# Values are handed to ``execve``, so a NUL can never survive; reject it at the
# door instead of silently truncating a credential.
_NUL = b"\0"


class SecretError(ValueError):
    """Invalid secret name, service id, or value. Message never contains the value."""


class MissingSecret(RuntimeError):
    """A declared secret has no stored value. Names the service and the secret."""


@dataclass(frozen=True)
class SecretStore:
    """Values for one harness instance, rooted at ``<root>/secrets``.

    ``root`` is the state dir root (see :func:`store_for`); the ``secrets``
    component is computed here rather than in ``ams.state`` so the layout of
    the store stays owned by this module.
    """

    root: Path

    @property
    def dir(self) -> Path:
        return self.root / "secrets"

    def __repr__(self) -> str:  # never render values; the dir listing is names only
        return f"SecretStore(dir={self.dir})"

    __str__ = __repr__

    # ----------------------------------------------------------------- paths

    def service_dir(self, service_id: str) -> Path:
        return self.dir / _check_id(service_id)

    def path(self, service_id: str, name: str) -> Path:
        return self.service_dir(service_id) / _check_name(name)

    # ----------------------------------------------------------------- write

    def set(self, service_id: str, name: str, value: bytes) -> Path:
        """Store ``value`` for ``<service_id>.<name>``; returns the file path.

        ``value`` is bytes because it comes from stdin or a file and must not
        round-trip through a lossy decode. Two rules apply to it:

        - a NUL byte is rejected (it cannot survive ``execve``);
        - exactly one trailing newline is stripped (``\\r\\n`` counts as one),
          because ``echo secret | ams secret set ...`` is the common case and
          nobody means to include that byte. A value that genuinely ends in a
          newline needs two: the second one survives. An empty result is
          rejected — it is always a mistake (a mistyped pipe, an empty file),
          and a service that declared the secret would start with an empty
          credential instead of failing loudly.

        The write is atomic: a 0600 temp file in the same directory, then
        ``os.replace``. A concurrent reader therefore sees either the old value
        or the new one, never a truncated file, and no temp file survives a
        failure.
        """
        value = _check_value(value)
        target = self.path(service_id, name)
        self._ensure_dir(target.parent)
        tmp = target.with_name(f".{target.name}.tmp{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(value)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, FILE_MODE)  # O_CREAT's mode is subject to the umask
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        log.info("stored secret %s/%s (%d bytes)", service_id, name, len(value))
        return target

    def remove(self, service_id: str, name: str) -> bool:
        """Delete one value. ``False`` if there was nothing to delete."""
        path = self.path(service_id, name)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        log.info("removed secret %s/%s", service_id, name)
        return True

    def remove_all(self, service_id: str) -> int:
        """Delete every value for a service (and its directory). Returns the count."""
        removed = 0
        for name in self.names(service_id):
            if self.remove(service_id, name):
                removed += 1
        try:
            self.service_dir(service_id).rmdir()
        except OSError as e:  # non-empty (a stray temp file) or already gone
            log.debug("keeping %s: %s", self.service_dir(service_id), e)
        return removed

    # ------------------------------------------------------------------ read

    def names(self, service_id: str) -> list[str]:
        """Names that currently have a value, sorted. Never reads a value."""
        try:
            entries = list(self.service_dir(service_id).iterdir())
        except FileNotFoundError:
            return []
        return sorted(e.name for e in entries if e.is_file() and ENV_NAME_RE.match(e.name))

    def load(self, service_id: str, names: Iterable[str]) -> dict[str, str]:
        """Values for ``names``, ready to merge into a service environment.

        Raises :class:`MissingSecret` naming the service and the first missing
        secret. Decoding uses ``surrogateescape``, the same convention
        ``os.environ`` uses, so a non-UTF-8 value round-trips into the child
        rather than raising here.
        """
        out: dict[str, str] = {}
        for name in names:
            path = self.path(service_id, name)
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                raise MissingSecret(
                    f"{service_id}: secret {name!r} has no value; "
                    f"set it with: ams secret set {service_id} {name}"
                ) from None
            except OSError as e:
                raise MissingSecret(f"{service_id}: secret {name!r} is unreadable: {e}") from None
            out[name] = raw.decode("utf-8", "surrogateescape")
        return out

    def missing(self, service_id: str, names: Iterable[str]) -> list[str]:
        """Declared names with no stored value. Cheap: stats, never reads."""
        have = set(self.names(service_id))
        return [n for n in names if n not in have]

    # --------------------------------------------------------------- private

    def _ensure_dir(self, path: Path) -> None:
        for d in (self.dir, path):
            d.mkdir(parents=True, exist_ok=True)
            # mkdir's mode= is subject to the umask; force it. 0700 keeps the
            # tree unreadable and unlistable by everyone but the harness.
            if stat.S_IMODE(d.stat().st_mode) != DIR_MODE:
                os.chmod(d, DIR_MODE)


def store_for(state: StateDir) -> SecretStore:
    """The secret store belonging to a state dir."""
    return SecretStore(state.root)


# --------------------------------------------------------------------- checks


def _check_id(service_id: str) -> str:
    if not isinstance(service_id, str) or not SERVICE_ID_RE.match(service_id):
        raise SecretError(f"invalid service id {service_id!r}; must match {SERVICE_ID_RE.pattern}")
    return service_id


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not ENV_NAME_RE.match(name):
        raise SecretError(f"invalid secret name {name!r}; must match {ENV_NAME_RE.pattern}")
    if name in RESERVED_ENV or name.startswith(RESERVED_ENV_PREFIXES):
        raise SecretError(f"secret name {name!r} is reserved: set by the harness")
    return name


def _check_value(value: bytes) -> bytes:
    if not isinstance(value, (bytes, bytearray)):
        raise SecretError("secret value must be bytes")
    value = bytes(value)
    if _NUL in value:
        raise SecretError("secret value contains a NUL byte; it cannot be an environment value")
    if value.endswith(b"\r\n"):
        value = value[:-2]
    elif value.endswith(b"\n"):
        value = value[:-1]
    if not value:
        raise SecretError("secret value is empty (after stripping one trailing newline)")
    return value


# ------------------------------------------------------------------ injection

ExtraEnv = Callable[["ServiceDecl"], tuple[dict[str, str], tuple[str, ...]]]


def make_extra_env_for(state: StateDir, runtime_env_for: ExtraEnv | None = None) -> ExtraEnv:
    """``Supervisor(extra_env_for=...)`` that adds the declared secrets.

    Composed with (not replacing) the runtime activation lookup: the returned
    dict is ``runtime_env | secrets``. Secrets win over runtime extras because a
    runtime activation variable is harness-generated and a secret is the
    operator's explicit intent; neither can shadow a reserved name, because
    ``SpawnRequest.env`` sets those after ``extra_env`` and the schema rejects a
    declaration that names one.

    Pure lookup like the runtime one, with one difference: it raises
    :class:`MissingSecret` when a declared secret has no value. That is
    deliberate — the start fails, the supervisor escalates, and no service ever
    runs with a credential the operator only *thinks* is set.
    """
    store = store_for(state)

    def lookup(decl: ServiceDecl) -> tuple[dict[str, str], tuple[str, ...]]:
        env, path_prepend = runtime_env_for(decl) if runtime_env_for is not None else ({}, ())
        if decl.secrets:
            env = {**env, **store.load(decl.id, decl.secrets)}
        return env, path_prepend

    return lookup


def warn_missing_secrets(state: StateDir, declarations: Mapping[str, ServiceDecl]) -> list[str]:
    """Log one WARNING per service whose declared secrets are not all set.

    Startup does not refuse: the harness may legitimately come up before an
    operator has filled the store, and every *other* service must still run. The
    affected service fails at start with a clear escalation (see
    :func:`make_extra_env_for`), so this is a heads-up, not the enforcement.
    Returns the ids warned about, for tests and callers.
    """
    store = store_for(state)
    warned: list[str] = []
    for service_id, decl in declarations.items():
        if not decl.secrets:
            continue
        missing = store.missing(service_id, decl.secrets)
        if missing:
            log.warning(
                "%s declares secrets with no stored value: %s (set them with "
                "'ams secret set %s <NAME>'; the service will fail to start)",
                service_id,
                ", ".join(missing),
                service_id,
            )
            warned.append(service_id)
    return warned


# ------------------------------------------------------------------------ cli


def add_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``ams secret {set,rm,list,check}``.

    The wiring lives here rather than in ``ams.cli`` so that everything that can
    touch a value — parsing, reading stdin, the handlers — is in one auditable
    module.
    """
    parser = subparsers.add_parser(
        "secret",
        help="manage the write-only secret store (values never appear in argv)",
        description=(
            "Store values for the secret names a declaration lists. Values are "
            "read from stdin or a file, never from the command line, and are "
            "never printed back."
        ),
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")
    common.add_argument("--log-level", default="WARNING")
    ops = parser.add_subparsers(dest="secret_command", required=True)

    p_set = ops.add_parser(
        "set", parents=[common], help="store a value read from stdin (or --from-file)"
    )
    p_set.add_argument("service_id")
    p_set.add_argument("name", metavar="NAME")
    p_set.add_argument(
        "--from-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="read the value from this file instead of stdin",
    )

    p_rm = ops.add_parser("rm", parents=[common], help="delete one stored value")
    p_rm.add_argument("service_id")
    p_rm.add_argument("name", metavar="NAME")

    p_list = ops.add_parser("list", parents=[common], help="names that have a value, one per line")
    p_list.add_argument("service_id")

    p_check = ops.add_parser(
        "check", parents=[common], help="exit 1 if a declared secret has no value"
    )
    p_check.add_argument("service_id")
    return parser


def cmd_secret(args: argparse.Namespace) -> int:
    # No force=True: this command is also called in-process (tests, an embedding
    # agent), and tearing down whatever root handlers exist would swallow the
    # caller's logging. basicConfig is a no-op when the root is already set up,
    # so the level goes on the package logger explicitly.
    level = getattr(logging, str(args.log_level).upper(), logging.WARNING)
    logging.basicConfig(
        level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    logging.getLogger("ams").setLevel(level)
    # Local import: ams.cli imports this module to build its parser, so the
    # dependency has to be one-way at import time. _state_dir is the single
    # place that decides --state-dir beats $AMS_STATE_DIR (D11).
    from ams.cli import _state_dir

    state = _state_dir(args.state_dir)
    store = store_for(state)
    handlers: dict[str, Callable[[argparse.Namespace, StateDir, SecretStore], int]] = {
        "set": _cmd_set,
        "rm": _cmd_rm,
        "list": _cmd_list,
        "check": _cmd_check,
    }
    try:
        return handlers[args.secret_command](args, state, store)
    except (SecretError, MissingSecret) as e:
        print(f"ERROR {e}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as e:
        print(f"ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR


def _read_value(args: argparse.Namespace) -> bytes:
    if args.from_file is not None:
        return Path(args.from_file).read_bytes()
    return _read_stdin_bytes()


def _read_stdin_bytes() -> bytes:
    """Whole of stdin as bytes.

    ``sys.stdin.buffer`` is the real path; the text fallback exists because a
    test (or an embedding agent) may replace ``sys.stdin`` with a StringIO that
    has no buffer. A TTY gets a hint on stderr and is then read anyway, so an
    interactive ``ams secret set`` still works with Ctrl-D.
    """
    if getattr(sys.stdin, "isatty", lambda: False)():
        print(
            "reading the secret value from stdin; end with Ctrl-D "
            "(use --from-file to read a file instead)",
            file=sys.stderr,
        )
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return buffer.read()
    return str(sys.stdin.read()).encode("utf-8", "surrogateescape")


def _cmd_set(args: argparse.Namespace, _state: StateDir, store: SecretStore) -> int:
    # Nothing is printed on success: the shell's exit code is the whole answer,
    # and any output here is one careless `set -x` away from a leaked value.
    store.set(args.service_id, args.name, _read_value(args))
    return EXIT_OK


def _cmd_rm(args: argparse.Namespace, _state: StateDir, store: SecretStore) -> int:
    if not store.remove(args.service_id, args.name):
        print(f"no stored value for {args.service_id}/{args.name}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _cmd_list(args: argparse.Namespace, _state: StateDir, store: SecretStore) -> int:
    for name in store.names(args.service_id):
        print(name)
    return EXIT_OK


def _cmd_check(args: argparse.Namespace, state: StateDir, store: SecretStore) -> int:
    from ams.schema import DeclError

    try:
        decl = state.load_declaration(args.service_id)
    except (DeclError, OSError) as e:
        print(f"ERROR {args.service_id}: {e}", file=sys.stderr)
        return EXIT_ERROR
    missing = store.missing(args.service_id, decl.secrets)
    if missing:
        print(f"MISSING {args.service_id}: {' '.join(missing)}")
        return EXIT_ERROR
    print(f"OK {args.service_id} ({len(decl.secrets)} secret(s) set)")
    return EXIT_OK
