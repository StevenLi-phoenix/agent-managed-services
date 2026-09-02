"""The control channel: a unix socket served from the supervision loop.

Why it exists (DECISIONS D15 gap 2, D17): before this, changing one declaration
meant restarting ``ams-harness.service``, and ``KillMode=control-group`` takes
*every* service down with it. That is a blast-radius downgrade against the
per-service restart the api deployer already does. ``ams ctl <op> [id]`` and
``systemctl reload ams-harness`` (SIGHUP) now touch exactly one service, or add
and remove services without disturbing the rest.

Design constraints this file lives under:

- **No threads.** The listening socket and every accepted connection are
  registered in the supervisor's own ``selectors`` loop through
  ``Supervisor.register_fd`` / ``unregister_fd``. Nothing here touches the
  supervisor's private selector, and nothing here blocks: reads and writes are
  non-blocking, and a connection that cannot be flushed in one go is
  re-registered for ``EVENT_WRITE`` instead of spinning on ``send``.
- **Never raise into the loop.** Every handler returns an error *response*; the
  supervisor guards the callback as well, but relying on that guard would leave
  the client hanging with no reply.
- **Authorisation is the filesystem.** The socket is 0600 and owned by the
  harness user, in a state dir that is 0750. There is no in-band auth: a peer
  that can open the socket already runs as the harness user and could signal the
  process directly. This is why the channel is a unix socket and not HTTP on
  localhost (D17), where any local uid could reach it.

Protocol: one JSON object per line in, one JSON object per line out, then the
server closes. Request ``{"op": "...", "id": "..."}``; response
``{"ok": true, ...}`` or ``{"ok": false, "error": "..."}``. Request lines are
capped at :data:`MAX_REQUEST_BYTES`; a longer line is answered with an error
rather than buffered, so an unbounded write cannot grow the harness's memory.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import selectors
import socket
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ams.state import StateDir
    from ams.supervisor import Supervisor

log = logging.getLogger("ams.control")

CONTROL_SOCKET_NAME = "control.sock"
SOCKET_MODE = 0o600
BACKLOG = 16
# One request line may not exceed this. 64 KiB is the same cap ams.events puts on
# a log line; no legitimate request comes close (the largest is ~20 bytes).
MAX_REQUEST_BYTES = 65536
READ_CHUNK = 8192

# Ops that name a single service. Everything else takes no id.
PER_SERVICE_OPS: tuple[str, ...] = ("start", "stop", "restart", "kill")
GLOBAL_OPS: tuple[str, ...] = ("status", "reload", "ping")
OPS: tuple[str, ...] = (*GLOBAL_OPS, *PER_SERVICE_OPS)

ReloadFn = Callable[[], dict[str, Any]]


def control_socket_path(state: StateDir) -> Path:
    """Where the control socket lives for a given state dir.

    Computed here rather than added to ``StateDir`` so the server and the ``ams
    ctl`` client cannot disagree about it while ``ams.state`` stays untouched.
    A unix socket path is limited to ~104 bytes on macOS / 108 on Linux, so a
    deeply nested ``AMS_STATE_DIR`` will fail to bind; the error names the path.
    """
    return Path(state.root) / CONTROL_SOCKET_NAME


class _Connection:
    """One accepted client: read a line, answer it, close.

    Requests are single-shot by protocol, so there is no read-after-write state
    to track -- only the outgoing buffer, which exists because a client that
    stops reading must not block the supervision loop inside ``send``.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.fd = sock.fileno()
        self.inbuf = bytearray()
        self.outbuf = b""
        self.overflow = False


class ControlServer:
    """Unix-socket control channel, served from a :class:`Supervisor`'s loop.

    Usage: construct, :meth:`open`, then drive the supervisor normally; the
    server's fds are serviced by ``run_once``. :meth:`close` unlinks the socket.
    """

    def __init__(
        self,
        path: Path,
        supervisor: Supervisor,
        *,
        reload_fn: ReloadFn | None = None,
    ) -> None:
        self.path = Path(path)
        self.supervisor = supervisor
        self.reload_fn = reload_fn
        self._listener: socket.socket | None = None
        self._conns: dict[int, _Connection] = {}

    # ------------------------------------------------------------------ setup

    def open(self) -> None:
        """Bind, chmod 0600, listen, and register with the supervisor's loop.

        A leftover socket file from a harness that was SIGKILLed is removed
        first: ``bind`` fails with EADDRINUSE on an existing path whether or not
        anything is listening, so refusing to unlink would make a hard crash
        permanently disable the control channel. Only a socket file is removed --
        if the path is a regular file or a directory, that is a misconfiguration
        (or someone else's data) and the error is raised.
        """
        if self._listener is not None:
            raise RuntimeError(f"control server on {self.path} is already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._unlink_stale()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.setblocking(False)
            # umask would otherwise mask bits off the bind-created node; chmod
            # after bind is the portable way to guarantee 0600. There is a short
            # window where the node exists with the umask's mode -- acceptable,
            # since the parent directory is 0750 and harness-owned.
            sock.bind(str(self.path))
            os.chmod(self.path, SOCKET_MODE)
            sock.listen(BACKLOG)
        except OSError as e:
            sock.close()
            if _path_too_long(e, self.path):
                raise OSError(
                    f"cannot bind the control socket: the path is too long for a unix "
                    f"socket ({len(str(self.path))} bytes, kernel limit ~104): {self.path}. "
                    "Point $AMS_STATE_DIR at a shorter path."
                ) from e
            raise
        except BaseException:
            sock.close()
            raise
        self._listener = sock
        self.supervisor.register_fd(sock.fileno(), self._on_accept, name="control-listener")
        log.info("control socket listening on %s (mode 0%o)", self.path, SOCKET_MODE)

    def _unlink_stale(self) -> None:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return
        except OSError as e:
            raise OSError(f"cannot stat control socket path {self.path}: {e}") from e
        if not stat.S_ISSOCK(st.st_mode):
            raise OSError(
                f"{self.path} exists and is not a socket; refusing to remove it. "
                "Move it aside, or point --state-dir somewhere else."
            )
        log.warning("removing stale control socket %s", self.path)
        os.unlink(self.path)

    def close(self) -> None:
        """Unregister and close everything, and unlink the socket file."""
        for conn in list(self._conns.values()):
            self._close_conn(conn)
        if self._listener is not None:
            self.supervisor.unregister_fd(self._listener.fileno())
            self._listener.close()
            self._listener = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not unlink %s: %s", self.path, e)
        log.info("control socket closed")

    def __enter__(self) -> ControlServer:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- accepting

    def _on_accept(self, _fd: int) -> None:
        listener = self._listener
        if listener is None:  # pragma: no cover - unregistered before this can fire
            return
        # Drain the backlog: the selector is level-triggered, but accepting every
        # ready connection in one pass keeps concurrent clients from queueing
        # behind a whole loop iteration each.
        while True:
            try:
                sock, _addr = listener.accept()
            except BlockingIOError:
                return
            except OSError as e:
                if e.errno not in (errno.EINTR, errno.ECONNABORTED, errno.EMFILE, errno.ENFILE):
                    log.warning("control accept failed: %s", e)
                return
            sock.setblocking(False)
            conn = _Connection(sock)
            self._conns[conn.fd] = conn
            self.supervisor.register_fd(conn.fd, self._on_readable, name="control-conn")
            log.debug("control client connected (fd=%d)", conn.fd)

    # ---------------------------------------------------------------- reading

    def _on_readable(self, fd: int) -> None:
        conn = self._conns.get(fd)
        if conn is None:  # pragma: no cover - unregistered on close
            return
        while True:
            try:
                chunk = conn.sock.recv(READ_CHUNK)
            except BlockingIOError:
                return
            except InterruptedError:  # pragma: no cover - retried by CPython
                continue
            except OSError as e:
                log.debug("control read error on fd %d: %s", fd, e)
                self._close_conn(conn)
                return
            if not chunk:  # client hung up without sending a full line
                self._close_conn(conn)
                return
            conn.inbuf.extend(chunk)
            newline = conn.inbuf.find(b"\n")
            if newline >= 0:
                line = bytes(conn.inbuf[:newline])
                self._handle_line(conn, line)
                return
            if len(conn.inbuf) > MAX_REQUEST_BYTES:
                # Answer and close rather than keep buffering: the cap exists so a
                # client that never sends a newline cannot grow the harness's RSS.
                conn.inbuf.clear()
                conn.overflow = True
                self._respond(
                    conn,
                    _error(f"request line exceeds {MAX_REQUEST_BYTES} bytes"),
                )
                return

    def _handle_line(self, conn: _Connection, line: bytes) -> None:
        self._respond(conn, self.handle_request_bytes(line))

    # -------------------------------------------------------------- dispatch

    def handle_request_bytes(self, line: bytes) -> dict[str, Any]:
        """Parse and dispatch one request line. Never raises."""
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as e:
            return _error(f"request is not valid utf-8: {e}")
        text = text.strip()
        if not text:
            return _error("empty request")
        try:
            request = json.loads(text)
        except json.JSONDecodeError as e:
            return _error(f"malformed JSON: {e}")
        if not isinstance(request, dict):
            return _error("request must be a JSON object")
        return self.handle_request(request)

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run one already-parsed request. Never raises."""
        op = request.get("op")
        if not isinstance(op, str):
            return _error("request needs a string 'op'")
        if op not in OPS:
            return _error(f"unknown op {op!r}; expected one of {', '.join(sorted(OPS))}")
        service_id = request.get("id")
        if op in PER_SERVICE_OPS:
            if not isinstance(service_id, str) or not service_id:
                return _error(f"op {op!r} needs a service 'id'")
        elif service_id is not None and not isinstance(service_id, str):
            return _error("'id' must be a string")
        try:
            return self._dispatch(op, service_id)
        except KeyError as e:
            # Supervisor._get raises KeyError("unknown service 'x'") for a bad id.
            return _error(str(e.args[0]) if e.args else "unknown service")
        except Exception as e:  # a bug here answers the client, it does not crash
            log.exception("control op %r failed: %s", op, e)
            return _error(f"{type(e).__name__}: {e}")

    def _dispatch(self, op: str, service_id: str | None) -> dict[str, Any]:
        sup = self.supervisor
        if op == "ping":
            return {"ok": True, "pong": True, "pid": os.getpid()}
        if op == "status":
            return {"ok": True, "services": sup.status()}
        if op == "reload":
            if self.reload_fn is None:
                return _error("reload is not configured on this control server")
            return {"ok": True, "reload": self.reload_fn()}
        assert service_id is not None  # guaranteed by handle_request
        {"start": sup.start, "stop": sup.stop, "restart": sup.restart, "kill": sup.kill}[op](
            service_id
        )
        log.info("control: %s %s", op, service_id)
        return {"ok": True, "op": op, "id": service_id, "status": sup.status().get(service_id)}

    # --------------------------------------------------------------- writing

    def _respond(self, conn: _Connection, payload: dict[str, Any]) -> None:
        try:
            data = json.dumps(payload, default=str).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as e:  # pragma: no cover - default=str covers it
            log.error("control response is not serialisable: %s", e)
            data = json.dumps(_error("response could not be serialised")).encode() + b"\n"
        conn.outbuf = data
        self._flush(conn)

    def _on_writable(self, fd: int) -> None:
        conn = self._conns.get(fd)
        if conn is None:  # pragma: no cover - unregistered on close
            return
        self._flush(conn)

    def _flush(self, conn: _Connection) -> None:
        while conn.outbuf:
            try:
                sent = conn.sock.send(conn.outbuf)
            except BlockingIOError:
                # Client is not reading. Wait for writability instead of
                # spinning; the supervision loop must not stall on a slow peer.
                self.supervisor.register_fd(
                    conn.fd,
                    self._on_writable,
                    events=selectors.EVENT_WRITE,
                    name="control-conn-w",
                )
                return
            except InterruptedError:  # pragma: no cover - retried by CPython
                continue
            except OSError as e:
                log.debug("control write error on fd %d: %s", conn.fd, e)
                self._close_conn(conn)
                return
            conn.outbuf = conn.outbuf[sent:]
        self._close_conn(conn)

    def _close_conn(self, conn: _Connection) -> None:
        self._conns.pop(conn.fd, None)
        self.supervisor.unregister_fd(conn.fd)
        try:
            conn.sock.close()
        except OSError:  # pragma: no cover
            pass
        log.debug("control client closed (fd=%d)", conn.fd)


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _path_too_long(exc: OSError, path: Path) -> bool:
    """``sun_path`` overflow, which surfaces differently per platform.

    Linux raises ``OSError(ENAMETOOLONG)``; macOS raises a bare
    ``OSError("AF_UNIX path too long")`` with no errno at all, so the length has
    to be checked directly. Worth naming explicitly: the limit is 104 bytes on
    macOS and 108 on Linux, a deep ``AMS_STATE_DIR`` silently disables the whole
    control channel, and "cannot connect" sends the reader hunting for a
    stopped harness that is in fact running.
    """
    return exc.errno == errno.ENAMETOOLONG or len(str(path).encode()) >= 100


def _connect_error(path: Path, exc: OSError) -> str:
    if _path_too_long(exc, path):
        return (
            f"the control socket path is too long for a unix socket ({len(str(path))} bytes): "
            f"{path}\nThe kernel limit is ~104 bytes. Point --state-dir/$AMS_STATE_DIR at a "
            "shorter path."
        )
    return f"cannot connect to {path}: {exc}"


# ------------------------------------------------------------------- client


class ControlError(RuntimeError):
    """The control socket could not be reached (absent, stale, or unreadable)."""


def request(
    path: Path,
    op: str,
    service_id: str | None = None,
    *,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Send one request to the control socket and return the parsed response.

    Raises :class:`ControlError` when the socket is absent or nothing is
    listening -- the harness is not running -- which the CLI turns into exit 2,
    distinct from "the harness answered and said no" (exit 1).
    """
    payload: dict[str, Any] = {"op": op}
    if service_id is not None:
        payload["id"] = service_id
    line = json.dumps(payload).encode("utf-8") + b"\n"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        try:
            sock.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise ControlError(
                f"no harness listening on {path}: {e}\n"
                "Start it with 'systemctl start ams-harness' (or 'ams run'), and "
                "check that --state-dir/$AMS_STATE_DIR names the same state dir."
            ) from e
        except OSError as e:
            raise ControlError(_connect_error(path, e)) from e
        sock.sendall(line)
        buf = bytearray()
        while b"\n" not in buf:
            try:
                chunk = sock.recv(READ_CHUNK)
            except TimeoutError as e:
                raise ControlError(f"timed out after {timeout_s}s waiting for {path}") from e
            except OSError as e:
                raise ControlError(f"read from {path} failed: {e}") from e
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_REQUEST_BYTES * 16:  # a runaway server, not a response
                raise ControlError(f"response from {path} is implausibly large")
    finally:
        sock.close()
    if not buf:
        raise ControlError(f"{path} closed the connection without answering")
    text = bytes(buf).split(b"\n", 1)[0].decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ControlError(f"unparseable response from {path}: {e}: {text!r}") from e
    if not isinstance(parsed, dict):
        raise ControlError(f"response from {path} is not a JSON object: {text!r}")
    return parsed
