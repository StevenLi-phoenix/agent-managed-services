"""Layer-0 bootstrap: the RS256 keypair, the platform secrets, the two
declarations that make ``registry`` and ``auth`` ams services.

This replaces three shell scripts from the api repo's ``bootstrap/`` directory
(PLAN-allin Q4):

===============================  ==================================================
``03-jwt-keys.sh``               :func:`ensure_keypair` + :func:`place_jwt_key`
``08-auth-init.sh``              :func:`auth_declaration` + :func:`ensure_secrets`
``09-registry-init.sh``          :func:`registry_declaration` + :func:`ensure_secrets`
===============================  ==================================================

Three rules shape everything here.

**Nothing is overwritten.** The keypair is generated once and skipped forever
after; a secret that already has a value is left alone. Re-running is how the
agent loop repairs a partial bootstrap, so a second run must be able to say
"nothing was created" and mean it. Rotation is a deliberate, separate act (
``ams secret set`` / removing the key files), never a side effect of a sync tick.

**No generated value is ever printed, logged, or passed in argv.** Values go
from :func:`secrets.token_hex` straight into the :class:`~ams.secrets.SecretStore`,
which itself logs names and byte counts only. ``openssl`` writes the private key
to a file we name; the key never travels through a pipe we read.

**A mapped uid cannot read a harness file** (D4), so the JWT keys cannot live in
one shared directory the way production's ``/etc/auth/`` does. Each service that
needs a key gets its own copy inside its own service-owned ``<root>/etc/``,
placed through the admin namespace by :func:`place_jwt_key`. That helper is
Layer-0-agnostic on purpose: the sync loop (T3.1) calls it for every Layer-1
service to materialise ``SVC_M2M_PUBLIC_KEY_PATH``.

Wiring ``ams platform bootstrap`` into ``ams.cli`` belongs to T3.1, which owns
the ``platform`` subparser. Until then this module runs standalone::

    python -m ams.platform.bootstrap [--state DIR] [--store DIR] [--examples DIR]
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets as _secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ams.platform.translate import emit_toml
from ams.runtime import RuntimeStore
from ams.schema import (
    HealthSpec,
    LimitsSpec,
    LoggingSpec,
    RestartSpec,
    RuntimeSpec,
    ServiceDecl,
    StartSpec,
    StopSpec,
)
from ams.secrets import SecretStore, store_for
from ams.spawn import DATA_DIRNAME, INNER_GID, INNER_UID
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import run_admin

log = logging.getLogger("ams.platform.bootstrap")

__all__ = [
    "BootstrapError",
    "BootstrapResult",
    "REGISTRY_ID",
    "AUTH_ID",
    "LAYER0_PORTS",
    "REGISTRY_SECRETS",
    "AUTH_SECRETS",
    "ETC_DIRNAME",
    "JWT_PRIVATE_NAME",
    "JWT_PUBLIC_NAME",
    "platform_dir",
    "jwt_private_path",
    "jwt_public_path",
    "loopback_url",
    "ensure_keypair",
    "ensure_secrets",
    "ensure_service_dirs",
    "place_jwt_key",
    "registry_declaration",
    "auth_declaration",
    "layer0_declarations",
    "write_declaration",
    "bootstrap",
    "main",
]


class BootstrapError(RuntimeError):
    """Bootstrap could not complete. Never carries a secret value."""


# --------------------------------------------------------------------------- constants

REGISTRY_ID = "registry"
AUTH_ID = "auth"

#: Layer-0 ports are **fixed**, not allocated. A declaration can only expand its
#: own ``${PORT_<name>}``, so ``registry`` cannot write "auth's port" and auth
#: cannot write "registry's". Every cross reference in the replica -- registry's
#: ``REGISTRY_AUTH_URL``, auth's ``AUTH_REGISTRY_URL``, the JWT issuer, and the
#: ``REGISTRY_URL``/``AUTH_URL`` that ``TranslateContext`` injects into all 20
#: Layer-1 services -- is a literal URL, and a literal needs a number that is
#: known before anything starts. See DECISIONS D22 (T2.1).
LAYER0_PORTS: Mapping[str, int] = {REGISTRY_ID: 20100, AUTH_ID: 20101}

#: Generated once by :func:`ensure_secrets`, stored per service id.
REGISTRY_SECRETS: tuple[str, ...] = ("REGISTRY_ADMIN_TOKEN",)
AUTH_SECRETS: tuple[str, ...] = (
    "AUTH_SESSION_SECRET",
    "AUTH_PAT_VERIFY_TOKEN",
    # Auth's own M2M caller identity in the registry. The sync loop passes this
    # same value to ``registryclient.create_identity("auth", ...)``; it is not
    # shared with any other service id.
    "AUTH_M2M_SECRET",
)

#: ``token_hex(32)`` -> 64 hex characters, matching ``openssl rand -hex 32`` in
#: the bootstrap scripts this module replaces.
SECRET_BYTES = 32

PLATFORM_DIRNAME = "platform"
ETC_DIRNAME = "etc"
JWT_PRIVATE_NAME = "jwt-rs256.pem"
JWT_PUBLIC_NAME = "jwt-rs256.pub"

#: Harness-owned, harness-only. Nothing traverses it but this module.
PLATFORM_DIR_MODE = 0o700
KEY_PRIVATE_MODE = 0o600  # in the store, harness-owned
KEY_PUBLIC_MODE = 0o644
#: Inside a service root the copies are service-owned: the private key is
#: readable by that one service and nothing else, the public key by anyone who
#: can traverse in (which, from another namespace, is nobody).
SERVICE_KEY_PRIVATE_MODE = 0o400
SERVICE_KEY_PUBLIC_MODE = 0o444
ETC_DIR_MODE = 0o755
DATA_DIR_MODE = 0o750

#: Path used when rendering the checked-in examples, so ``examples/platform/
#: layer0/*/service.toml`` shows the declarations exactly as they land on the
#: target host (CLAUDE.md: ``AMS_STATE_DIR`` = ``/home/harness/store/state``).
EXAMPLE_STATE_ROOT = Path("/home/harness/store/state")

_OPENSSL = "openssl"
_KEYGEN_TIMEOUT_S = 60.0

_UV_PYTHON = "3.12"
_MEMORY_MAX = "200M"
_PIDS_MAX = 64
_START_PERIOD_S = 120.0

_HEADER = """\
# Generated by ams.platform.bootstrap -- do not hand-edit.
#
# Layer 0 of the racknerd replica:
#   {what}
#
# Regenerating is idempotent, so a local edit is silently reverted on the next
# `ams platform bootstrap`; change the generator instead.
#
# The port is FIXED, not allocated (DECISIONS D22, T2.1): every cross reference in the
# replica -- the other Layer-0 service's URL, the JWT issuer, and the
# REGISTRY_URL/AUTH_URL injected into all 20 Layer-1 declarations -- is a
# literal, and a declaration can only expand its own ${{PORT_<name>}}.
#
# Secret VALUES are never here. `secrets = [...]` lists names; the harness reads
# the values out of <state>/secrets/<id>/ at spawn (D16).
"""


# --------------------------------------------------------------------------- paths


def platform_dir(store: RuntimeStore) -> Path:
    """``<store>/platform`` -- the harness-only home of the replica keypair."""
    return store.root / PLATFORM_DIRNAME


def jwt_private_path(store: RuntimeStore) -> Path:
    return platform_dir(store) / JWT_PRIVATE_NAME


def jwt_public_path(store: RuntimeStore) -> Path:
    return platform_dir(store) / JWT_PUBLIC_NAME


def service_etc_dir(state: StateDir, service_id: str) -> Path:
    return state.service_root(service_id) / ETC_DIRNAME


def loopback_url(port: int) -> str:
    """The replica's only permitted URL shape.

    ``TranslateContext`` rejects a non-loopback ``registry_url``/``auth_url``
    outright (PLAN-allin risk 5: a replica must never be able to write into
    production), and Layer 0 uses the same shape so the two agree by
    construction.
    """
    if not isinstance(port, int) or isinstance(port, bool):
        raise BootstrapError(f"port must be an int, got {port!r}")
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------- result


@dataclass(frozen=True)
class BootstrapResult:
    """What one :func:`bootstrap` run did.

    Entries are stable, greppable labels (``"key:private"``,
    ``"secret:auth/AUTH_M2M_SECRET"``, ``"decl:registry"``) -- never values, and
    never a path that might be interesting to leak. A second run over an intact
    state dir has empty ``created`` and ``updated``, which is the property the
    idempotence test asserts.
    """

    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    existing: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated)

    def summary(self) -> str:
        return (
            f"{len(self.created)} created, {len(self.updated)} updated, "
            f"{len(self.existing)} already present"
        )


# --------------------------------------------------------------------------- keypair


def _run_openssl(argv: Sequence[str], *, timeout_s: float = _KEYGEN_TIMEOUT_S) -> None:
    """Run one ``openssl`` invocation as an argv list.

    A module-level function rather than an inline ``subprocess.run`` so the
    portable tests can record the argv without an ``openssl`` on the host, and
    so there is exactly one place that could ever grow a shell (it will not:
    D1 forbids shell strings, and these argv carry file paths, not key material).
    """
    exe = shutil.which(argv[0])
    if exe is None:
        raise BootstrapError(f"{argv[0]!r} not found on PATH; cannot generate the JWT keypair")
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell string
            [exe, *argv[1:]],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise BootstrapError(f"{' '.join(argv)} timed out after {timeout_s}s") from e
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-5:]
        raise BootstrapError(f"{' '.join(argv)} exited {proc.returncode}: {' / '.join(tail)}")


def ensure_keypair(store: RuntimeStore) -> list[str]:
    """Generate ``<store>/platform/jwt-rs256.{pem,pub}`` once. Returns what was created.

    Both files present -> nothing happens, ever. Only one present is treated as
    a half-finished previous run and both are regenerated, because a public key
    that does not match the private key silently breaks every M2M verification
    in the replica and there is no way to tell them apart after the fact.

    The pair is written into a temp directory and moved into place, so an
    interrupted run cannot leave a truncated private key that the next run would
    accept as "already exists".
    """
    priv, pub = jwt_private_path(store), jwt_public_path(store)
    if priv.is_file() and pub.is_file():
        log.info("jwt keypair already present at %s; not regenerating", priv)
        return []
    if priv.exists() or pub.exists():
        log.warning(
            "incomplete jwt keypair in %s (pem=%s pub=%s); regenerating both",
            platform_dir(store),
            priv.exists(),
            pub.exists(),
        )

    directory = platform_dir(store)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, PLATFORM_DIR_MODE)  # mkdir's mode is masked by the umask

    with tempfile.TemporaryDirectory(dir=directory, prefix=".keygen") as td:
        tmp_priv = Path(td) / JWT_PRIVATE_NAME
        tmp_pub = Path(td) / JWT_PUBLIC_NAME
        _run_openssl(
            [
                _OPENSSL,
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(tmp_priv),
            ]
        )
        _run_openssl([_OPENSSL, "rsa", "-in", str(tmp_priv), "-pubout", "-out", str(tmp_pub)])
        # openssl honours the umask; force the modes before the files are
        # visible under their final names.
        for path, mode in ((tmp_priv, KEY_PRIVATE_MODE), (tmp_pub, KEY_PUBLIC_MODE)):
            if not path.is_file():
                raise BootstrapError(f"openssl reported success but {path.name} was not written")
            os.chmod(path, mode)
        os.replace(tmp_priv, priv)
        os.replace(tmp_pub, pub)

    log.info("generated RS256 keypair in %s", directory)
    return ["key:private", "key:public"]


# --------------------------------------------------------------------------- secrets


def ensure_secrets(store: SecretStore, wanted: Mapping[str, Sequence[str]]) -> list[str]:
    """Fill in every missing secret in ``{service_id: [NAME, ...]}``.

    A name that already has a value is never touched -- rotating
    ``AUTH_SESSION_SECRET`` logs every user out, and rotating
    ``REGISTRY_ADMIN_TOKEN`` locks the sync loop out of its own registry, so
    neither may happen as a side effect of a repair run.

    The value goes ``token_hex -> encode -> SecretStore.set`` with nothing in
    between: no local variable outlives the call, nothing is returned, and the
    return value here is a list of *names*.
    """
    created: list[str] = []
    for service_id in sorted(wanted):
        for name in store.missing(service_id, wanted[service_id]):
            store.set(service_id, name, _secrets.token_hex(SECRET_BYTES).encode("ascii"))
            created.append(f"secret:{service_id}/{name}")
    return created


# --------------------------------------------------------------------------- key placement


def ensure_service_dirs(
    state: StateDir,
    service_id: str,
    block: UidBlock,
    *,
    harness_uid: int | None = None,
    harness_gid: int | None = None,
) -> bool:
    """Make sure ``<root>/data`` and ``<root>/etc`` exist and belong to ``block``.

    ``ams.userns.ensure_service_root`` already guarantees ``data`` before every
    spawn; this adds ``etc`` (the per-service key directory) and is safe to call
    before the root has ever been staged. Returns ``True`` when it did work.

    Warm path forks nothing: if both directories exist and the root is already
    owned by the block, this is three ``stat`` calls.
    """
    root = state.service_root(service_id)
    data, etc = root / DATA_DIRNAME, root / ETC_DIRNAME
    if data.is_dir() and etc.is_dir() and root.exists() and os.stat(root).st_uid == block.uid_start:
        return False

    admin = {"harness_uid": harness_uid, "harness_gid": harness_gid}
    if not root.exists():
        root.mkdir(parents=True, mode=0o755)
        os.chmod(root, 0o755)
    if not data.is_dir():
        run_admin(["mkdir", "-m", oct(DATA_DIR_MODE)[2:], "-p", str(data)], block, **admin).check()
    if not etc.is_dir():
        run_admin(["mkdir", "-m", oct(ETC_DIR_MODE)[2:], "-p", str(etc)], block, **admin).check()
    run_admin(["chown", "-R", f"{INNER_UID}:{INNER_GID}", str(root)], block, **admin).check()
    log.info("prepared %s: data=%s etc=%s", service_id, data, etc)
    return True


def place_jwt_key(
    state: StateDir,
    service_id: str,
    block: UidBlock,
    *,
    store: RuntimeStore,
    private: bool = False,
    harness_uid: int | None = None,
    harness_gid: int | None = None,
) -> bool:
    """Copy a JWT key from the store into ``<root>/etc/``, owned by the service.

    Production keeps one ``/etc/auth/jwt-rs256.pub`` that every service reads
    through a shared unix group. That is unreachable here: the harness uid is
    deliberately not mapped into a service's namespace (D4), so a harness-owned
    file is ``nobody``-owned inside it and a harness-owned *directory* outside
    the service root is not something the service can be given access to without
    inventing a group. Each service therefore gets its own copy.

    ``private=True`` additionally places the 0400 private key. Only ``registry``
    and ``auth`` may ask for it -- they are the two signers (registry mints M2M
    tokens, auth mints user tokens), mirroring production's "registry is in the
    auth group" arrangement but without the shared group.

    Returns ``True`` when a copy was made. The warm path is one ``stat``: the
    key files are immutable once :func:`ensure_keypair` has run, so a
    destination with the right owner and mode is by construction the right
    content. (If the store keypair is ever deleted and regenerated, delete the
    per-service ``<root>/etc/jwt-rs256.*`` too -- nothing here notices.)
    """
    src = jwt_private_path(store) if private else jwt_public_path(store)
    if not src.is_file():
        raise BootstrapError(f"{src} does not exist; run ensure_keypair first")
    etc = service_etc_dir(state, service_id)
    dst = etc / src.name
    mode = SERVICE_KEY_PRIVATE_MODE if private else SERVICE_KEY_PUBLIC_MODE

    try:
        st = os.stat(dst)
    except FileNotFoundError:
        pass
    else:
        if st.st_uid == block.uid_start and (st.st_mode & 0o7777) == mode:
            log.debug("%s: %s already in place", service_id, dst)
            return False

    admin = {"harness_uid": harness_uid, "harness_gid": harness_gid}
    # Four argv lists, no shell (D1). `mkdir -m` sets the mode on creation so a
    # key is never briefly visible through a 0777 directory; the recursive chown
    # covers both the directory and every key already in it.
    run_admin(["mkdir", "-m", oct(ETC_DIR_MODE)[2:], "-p", str(etc)], block, **admin).check()
    run_admin(["cp", str(src), str(dst)], block, **admin).check()
    run_admin(["chown", "-R", f"{INNER_UID}:{INNER_GID}", str(etc)], block, **admin).check()
    run_admin(["chmod", oct(mode)[2:].zfill(4), str(dst)], block, **admin).check()
    log.info("placed %s for %s (mode %o)", src.name, service_id, mode)
    return True


# --------------------------------------------------------------------------- declarations


def _root_str(state: StateDir, service_id: str) -> str:
    return str(state.service_root(service_id))


def registry_declaration(
    state: StateDir,
    *,
    port_name: str = "main",
    port: int | None = None,
    auth_port: int | None = None,
) -> ServiceDecl:
    """The ``registry`` declaration: service catalog, heartbeat, ACL, M2M minting.

    Verified against ``api/components/registry/src/registry/main.py``: the
    module exposes a lifespan-driven module-level ``app``, **not** a
    ``build_app`` factory, so the argv has no ``--factory``.
    """
    port = LAYER0_PORTS[REGISTRY_ID] if port is None else port
    auth_port = LAYER0_PORTS[AUTH_ID] if auth_port is None else auth_port
    root = _root_str(state, REGISTRY_ID)
    issuer = loopback_url(auth_port)
    return ServiceDecl(
        id=REGISTRY_ID,
        name="Registry",
        start=StartSpec(
            argv=(
                "uvicorn",
                "registry.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                f"${{PORT_{port_name}}}",
            ),
            workdir="repo/components/registry",
        ),
        env={
            "REGISTRY_DB_PATH": f"{root}/{DATA_DIRNAME}/registry.db",
            # Heartbeat churn, deliberately excluded from the daily backup by
            # api's own design; a separate file keeps that property here too.
            "REGISTRY_RUNTIME_DB_PATH": f"{root}/{DATA_DIRNAME}/registry-runtime.db",
            "REGISTRY_MIGRATIONS_DIR": f"{root}/repo/components/registry/migrations",
            "REGISTRY_JWT_PRIVATE_KEY_PATH": f"{root}/{ETC_DIRNAME}/{JWT_PRIVATE_NAME}",
            # Signer and verifier must agree. Every Layer-1 service builds its
            # M2MVerifier with ``issuer=config.auth_url``, and translate.py
            # injects AUTH_URL as this same loopback URL -- so the issuer is the
            # replica's auth address, not production's https://auth.lishuyu.app.
            "REGISTRY_JWT_ISSUER": issuer,
            "REGISTRY_AUTH_URL": issuer,
        },
        ports={port_name: port},
        runtime=RuntimeSpec(kind="uv", python=_UV_PYTHON, sync=True),
        health=HealthSpec(
            kind="http", port=port_name, path="/health", start_period_s=_START_PERIOD_S
        ),
        stop=StopSpec(signal="SIGTERM", timeout_s=10.0),
        limits=LimitsSpec(memory_max=_MEMORY_MAX, pids_max=_PIDS_MAX),
        restart=RestartSpec(policy="always"),
        # The api SDK's setup_sdk installs "LEVELNAME name: message" on the root
        # logger (PLAN-allin Q5a/T1.4), so the harness reads the level the
        # process printed instead of guessing from the text.
        logging=LoggingSpec(format="level-prefix"),
        secrets=REGISTRY_SECRETS,
    )


def auth_declaration(
    state: StateDir,
    *,
    port_name: str = "main",
    port: int | None = None,
    registry_port: int | None = None,
) -> ServiceDecl:
    """The ``auth`` declaration: user JWT, PAT, JWKS.

    Two differences from production worth reading twice:

    ``[health] kind = "tcp"``. Auth has **no** ``/health`` route -- it registers
    ``users``, ``pats``, ``tokens``, ``jwks``, ``oauth``, ``email_login``,
    ``password_login``, ``webauthn_login``, ``emergency`` and ``audit``, and
    nothing else. A TCP connect is therefore the strongest honest liveness
    signal; an HTTP probe of any real route would need a credential and would
    turn an auth outage into a probe-shaped 401.

    **No GitHub OAuth placeholders.** ``_build_oauth_providers`` skips GitHub
    unless *both* ``AUTH_GITHUB_CLIENT_ID`` and ``AUTH_GITHUB_CLIENT_SECRET``
    are set, so the app starts fine without them -- and setting placeholders
    would build a provider that renders a login button leading to a GitHub error
    page. Unset is both simpler and more honest. OAuth is out of replica scope
    (PLAN-allin Q4).
    """
    port = LAYER0_PORTS[AUTH_ID] if port is None else port
    registry_port = LAYER0_PORTS[REGISTRY_ID] if registry_port is None else registry_port
    root = _root_str(state, AUTH_ID)
    return ServiceDecl(
        id=AUTH_ID,
        name="Auth",
        start=StartSpec(
            argv=(
                "uvicorn",
                "auth.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                f"${{PORT_{port_name}}}",
            ),
            workdir="repo/components/auth",
        ),
        env={
            "AUTH_DB_PATH": f"{root}/{DATA_DIRNAME}/auth.db",
            "AUTH_MIGRATIONS_DIR": f"{root}/repo/components/auth/migrations",
            "AUTH_JWT_PRIVATE_KEY_PATH": f"{root}/{ETC_DIRNAME}/{JWT_PRIVATE_NAME}",
            "AUTH_JWT_PUBLIC_KEY_PATH": f"{root}/{ETC_DIRNAME}/{JWT_PUBLIC_NAME}",
            "AUTH_JWT_ISSUER": loopback_url(port),
            "AUTH_REGISTRY_URL": loopback_url(registry_port),
            # Plain HTTP on loopback: a Secure cookie would never be sent back.
            "AUTH_INSECURE_COOKIES": "1",
            # Empty -> host-only cookies. Production's ".lishuyu.app" would make
            # every cookie undeliverable to 127.0.0.1; the empty string is read
            # by main.py as "unset" and is written out to say so deliberately.
            "AUTH_COOKIE_DOMAIN": "",
            # No OAuth provider is configured, so nothing can create the first
            # account anyway; closed is the safer of two inert settings.
            "AUTH_OPEN_REGISTRATION": "0",
            "AUTH_ALLOWED_RETURN_TO_HOSTS": "127.0.0.1",
        },
        ports={port_name: port},
        runtime=RuntimeSpec(kind="uv", python=_UV_PYTHON, sync=True),
        health=HealthSpec(kind="tcp", port=port_name, start_period_s=_START_PERIOD_S),
        stop=StopSpec(signal="SIGTERM", timeout_s=10.0),
        limits=LimitsSpec(memory_max=_MEMORY_MAX, pids_max=_PIDS_MAX),
        restart=RestartSpec(policy="always"),
        logging=LoggingSpec(format="level-prefix"),
        secrets=AUTH_SECRETS,
    )


def layer0_declarations(state: StateDir, *, port_name: str = "main") -> dict[str, ServiceDecl]:
    """Both Layer-0 declarations, keyed by service id."""
    return {
        REGISTRY_ID: registry_declaration(state, port_name=port_name),
        AUTH_ID: auth_declaration(state, port_name=port_name),
    }


def render_declaration(decl: ServiceDecl, *, what: str) -> str:
    """``service.toml`` text: a header comment, then :func:`emit_toml`'s output
    with the ``[logging]`` table spliced in ahead of ``[stop]``.

    ``emit_toml`` does not render ``[logging]`` (it is shared with the manifest
    translator, whose 21 golden files predate the field). Rather than change a
    module another task owns, the two lines are inserted here, in the same slot
    ``examples/platform/caddy/service.toml`` puts them. The round trip through
    ``ams.schema.loads`` is asserted by the tests, so a drift in either half
    fails loudly.
    """
    body = emit_toml(decl)
    if decl.logging.format != "auto":
        lines = body.split("\n")
        try:
            at = lines.index("[stop]")
        except ValueError:  # pragma: no cover - emit_toml always writes [stop]
            raise BootstrapError("emit_toml produced no [stop] table") from None
        lines[at:at] = ["[logging]", f'format = "{decl.logging.format}"', ""]
        body = "\n".join(lines)
    return _HEADER.format(what=what) + "\n" + body


_WHAT = {
    REGISTRY_ID: "the service catalog, heartbeat collector, ACL store and M2M token minter",
    AUTH_ID: "user JWTs, personal access tokens and the JWKS endpoint",
}


def write_declaration(path: Path, decl: ServiceDecl) -> str:
    """Write one declaration, reporting ``"created"``/``"updated"``/``"existing"``.

    A declaration is generated data, so drift is repaired rather than preserved
    -- but the common case is byte-identical, and rewriting an identical file
    would churn its mtime and make "did anything change?" unanswerable.
    """
    text = render_declaration(decl, what=_WHAT.get(decl.id, f"the {decl.id} service"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.read_text(encoding="utf-8") == text:
            return "existing"
        outcome = "updated"
        log.warning("%s drifted from the generator; rewriting", path)
    else:
        outcome = "created"
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    log.info("%s declaration %s", outcome, path)
    return outcome


# --------------------------------------------------------------------------- entry point


def bootstrap(
    state: StateDir,
    store: RuntimeStore,
    *,
    registry_port_name: str = "main",
    examples_dir: Path | None = None,
) -> BootstrapResult:
    """Run the whole Layer-0 bootstrap. Idempotent.

    Deliberately does **not** touch service roots: at bootstrap time no uid
    block has been allocated and no source has been staged, so there is nothing
    to place a key into. :func:`place_jwt_key` and :func:`ensure_service_dirs`
    are the pieces the sync loop (T3.1) and the live bring-up (T3.2) call once
    those exist.

    ``examples_dir`` additionally renders the two declarations against
    :data:`EXAMPLE_STATE_ROOT` so the checked-in examples show the real thing.
    """
    state.ensure()
    created: list[str] = []
    updated: list[str] = []
    existing: list[str] = []

    made = ensure_keypair(store)
    created += made
    if not made:
        existing += ["key:private", "key:public"]

    secret_store = store_for(state)
    wanted = {REGISTRY_ID: REGISTRY_SECRETS, AUTH_ID: AUTH_SECRETS}
    made_secrets = ensure_secrets(secret_store, wanted)
    created += made_secrets
    existing += [
        f"secret:{sid}/{name}"
        for sid in sorted(wanted)
        for name in wanted[sid]
        if f"secret:{sid}/{name}" not in made_secrets
    ]

    declarations = layer0_declarations(state, port_name=registry_port_name)
    for service_id, decl in declarations.items():
        outcome = write_declaration(state.service_decl_path(service_id), decl)
        {"created": created, "updated": updated, "existing": existing}[outcome].append(
            f"decl:{service_id}"
        )

    if examples_dir is not None:
        example_state = StateDir(EXAMPLE_STATE_ROOT)
        for service_id, decl in layer0_declarations(
            example_state, port_name=registry_port_name
        ).items():
            outcome = write_declaration(Path(examples_dir) / service_id / "service.toml", decl)
            {"created": created, "updated": updated, "existing": existing}[outcome].append(
                f"example:{service_id}"
            )

    result = BootstrapResult(tuple(created), tuple(updated), tuple(sorted(existing)))
    log.info("layer-0 bootstrap: %s", result.summary())
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m ams.platform.bootstrap``.

    A temporary front door: T3.1 owns the ``platform`` subparser in ``ams.cli``
    and will call :func:`bootstrap` from there. Nothing printed here is derived
    from a secret -- the labels are names.
    """
    parser = argparse.ArgumentParser(
        prog="python -m ams.platform.bootstrap",
        description="Generate the Layer-0 keypair, secrets and declarations (idempotent).",
    )
    parser.add_argument("--state", metavar="DIR", help="state dir (default: $AMS_STATE_DIR)")
    parser.add_argument("--store", metavar="DIR", help="store dir (default: $AMS_STORE_DIR)")
    parser.add_argument(
        "--examples",
        metavar="DIR",
        help="also render the declarations for this examples directory",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    state = StateDir(Path(args.state)) if args.state else StateDir.from_env()
    store = RuntimeStore(Path(args.store)) if args.store else RuntimeStore.from_env()
    try:
        result = bootstrap(
            state,
            store,
            examples_dir=Path(args.examples) if args.examples else None,
        )
    except BootstrapError as e:
        log.error("bootstrap failed: %s", e)
        return 1
    for label in result.created:
        log.info("created %s", label)
    for label in result.updated:
        log.info("updated %s", label)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
