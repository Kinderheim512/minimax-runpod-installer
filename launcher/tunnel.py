"""Windows SSH tunnel manager for the MiniMax H3 Launcher.

Establishes and manages local SSH port-forwarding tunnels from the Windows host
to services running inside a RunPod pod. A tunnel is a long-lived ``ssh -N -L``
child process.

The module is split into:

* pure, testable functions (``build_ssh_args``, ``build_tunnel_args``,
  ``is_port_free``) that construct the ``ssh`` command line;
* a :class:`TunnelManager` that spawns and supervises the ``ssh`` subprocess,
  with an injectable spawner so it can be unit-tested without a real SSH server.

No tunnel is actually established at import time, and nothing here requires an
SSH server, GPU, or RunPod account to test.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import winproc

# Console flags live in one place (``launcher.winproc``); re-exported here so
# the historical ``tunnel.CREATE_NO_WINDOW`` name keeps working.
CREATE_NO_WINDOW = winproc.CREATE_NO_WINDOW

# Keepalive / reliability flags applied to every tunnel.
_SERVER_ALIVE_INTERVAL = "15"
_SERVER_ALIVE_COUNT_MAX = "3"
_CONNECT_TIMEOUT = "10"


class TunnelError(RuntimeError):
    """Raised when a tunnel cannot be established or supervised."""


def from_runpod_endpoint(endpoint: object) -> "SshEndpoint":
    """Convert a RunPod SSH endpoint object into a tunnel :class:`SshEndpoint`.

    ``launcher.runpod.SshEndpoint`` and :class:`SshEndpoint` are intentionally
    kept as separate dataclasses (per the architectural rule that the RunPod
    client and the SSH/tunnel manager remain decoupled). They share the same
    ``host`` / ``port`` / ``username`` shape, so this minimal adapter converts
    between them without importing ``launcher.runpod`` here.

    The expected flow is::

        RunPodClient.get_pod(...).direct_endpoint  ->  from_runpod_endpoint(...)
            ->  TunnelManager.start(...)

    Any object exposing ``host``, ``port``, and ``username`` attributes is
    accepted (duck-typed), which keeps the tunnel manager independent of the
    RunPod client's concrete type.
    """
    host = getattr(endpoint, "host", None)
    port = getattr(endpoint, "port", 22)
    username = getattr(endpoint, "username", "root")
    if not host:
        raise TunnelError("RunPod endpoint is missing a host")
    return SshEndpoint(host=str(host), port=int(port), username=str(username))


@dataclass(frozen=True)
class TunnelTarget:
    """What a tunnel forwards: a local port to a remote host:port."""

    local_port: int
    remote_host: str
    remote_port: int

    @property
    def forwarding_spec(self) -> str:
        """The ``-L`` argument: ``local:remote_host:remote_port``."""
        return f"{self.local_port}:{self.remote_host}:{self.remote_port}"


@dataclass(frozen=True)
class SshEndpoint:
    """Where to connect for the tunnel (mirrors ``launcher.runpod.SshEndpoint``).

    Kept local to avoid a hard coupling between the tunnel manager and the
    RunPod client; callers pass a plain host/port/username triple.
    """

    host: str
    port: int = 22
    username: str = "root"


#: Substrings of ssh stderr that mean "the endpoint's host key is not the one
#: recorded for it". RunPod recycles public ``host:port`` pairs across pods, so
#: a pod created on an endpoint an earlier pod already used presents a brand
#: new host key: with ``StrictHostKeyChecking=accept-new`` that is a hard
#: failure ("Host key verification failed"), not a boot delay. The stale entry
#: is ours to drop — the next attempt re-adds the new key.
HOST_KEY_CHANGED_MARKERS = (
    "remote host identification has changed",
    # "Host key for [ip]:port has changed and you have requested strict checking."
    "host key for",
    "host key verification failed",
)


def is_host_key_changed(stderr: str) -> bool:
    """Return True if *stderr* reports a changed/unknown host key.

    "Connection refused" / "Connection timed out" mean the pod's sshd is still
    coming up (docker image pulling) and must NOT be mistaken for this.
    """
    lowered = stderr.lower()
    return any(marker in lowered for marker in HOST_KEY_CHANGED_MARKERS)


def host_key_spec(endpoint: SshEndpoint) -> str:
    """Return the ``known_hosts`` host field for *endpoint*.

    OpenSSH brackets the host and appends the port whenever the port is not the
    default one: ``[194.68.245.68]:22122``.
    """
    if endpoint.port == 22:
        return endpoint.host
    return f"[{endpoint.host}]:{endpoint.port}"


def default_known_hosts_path() -> Path:
    """Path of the ``known_hosts`` file the spawned ``ssh`` reads by default."""
    return Path(os.path.expanduser("~")) / ".ssh" / "known_hosts"


def purge_known_host_entry(spec: str, path: Optional[Path] = None) -> bool:
    """Drop every ``known_hosts`` line whose host field matches *spec*.

    The file is rewritten atomically and every unrelated line (comments, blank
    lines, other hosts) is kept byte for byte. Returns True when at least one
    line was removed; a missing file, a file without a match and an unreadable
    or unwritable file all report False, so the caller aborts instead of
    retrying blind.

    Hashed entries (``HashKnownHosts``) cannot be matched in Python, so a file
    that holds hashed lines falls back to ``ssh-keygen -R``, which hashes the
    spec the same way ssh does.
    """
    path = Path(path) if path is not None else default_known_hosts_path()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return False

    kept: list[str] = []
    removed = 0
    for line in lines:
        fields = line.split()
        if len(fields) < 2 or fields[0].startswith("#"):
            kept.append(line)
            continue
        if spec in fields[0].split(","):
            removed += 1
            continue
        kept.append(line)

    if removed:
        if not _rewrite_known_hosts(path, kept):
            return False
        return True

    if any(line.startswith("|1|") for line in lines):
        return _purge_hashed_known_host_entry(spec, path)
    return False


def _rewrite_known_hosts(path: Path, lines: Sequence[str]) -> bool:
    """Replace *path* with *lines*, keeping the original file's permissions."""
    tmp = path.with_name(path.name + ".minimax-tmp")
    try:
        tmp.write_text("".join(lines), encoding="utf-8", newline="")
        try:
            os.chmod(tmp, os.stat(path).st_mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def _purge_hashed_known_host_entry(spec: str, path: Path) -> bool:
    """Remove a hashed *spec* entry with ``ssh-keygen -R`` (best effort)."""
    try:
        completed = winproc.run(
            ["ssh-keygen", "-R", spec, "-f", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = f"{completed.stdout or ''}{completed.stderr or ''}".lower()
    if "not found" in output:
        return False
    return completed.returncode == 0


def build_ssh_base_args(
    key_path: Optional[str],
    connect_timeout: int = int(_CONNECT_TIMEOUT),
) -> list[str]:
    """Return the common ``ssh`` reliability/security flags.

    These flags make the connection non-interactive and self-healing:

    * ``BatchMode=yes`` — never prompt for a password/key (fail instead);
    * ``StrictHostKeyChecking=accept-new`` — accept new host keys without prompt;
    * ``ServerAliveInterval`` / ``ServerAliveCountMax`` — detect dead connections;
    * ``ConnectTimeout`` — fail fast on unreachable hosts.
    """
    args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ServerAliveInterval={_SERVER_ALIVE_INTERVAL}",
        "-o", f"ServerAliveCountMax={_SERVER_ALIVE_COUNT_MAX}",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ExitOnForwardFailure=yes",
    ]
    if key_path:
        args.extend(["-i", key_path])
    return args


def build_tunnel_args(
    target: TunnelTarget,
    endpoint: SshEndpoint,
    key_path: Optional[str],
    connect_timeout: int = int(_CONNECT_TIMEOUT),
) -> list[str]:
    """Build the full ``ssh -N -L ...`` command line for a single-forward tunnel.

    ``-N`` runs ssh without a remote command (forwarding only). The result is a
    list suitable for ``subprocess.Popen``.

    Kept as the single-target spelling of :func:`build_tunnel_args_multi`; the
    two produce byte-identical argument lists for one target.
    """
    return build_tunnel_args_multi([target], endpoint, key_path, connect_timeout)


def build_tunnel_args_multi(
    targets: Sequence[TunnelTarget],
    endpoint: SshEndpoint,
    key_path: Optional[str],
    connect_timeout: int = int(_CONNECT_TIMEOUT),
) -> list[str]:
    """Build one ``ssh`` command line forwarding several local ports at once.

    One ``ssh -L`` per target, all in a single connection. That matters for the
    ``train`` stack, which needs two ports (the KasmVNC desktop and the file
    manager): two separate ``ssh`` processes would mean two authentications,
    two failure modes and two things to reap, for one pod.

    ``ExitOnForwardFailure=yes`` (set by :func:`build_ssh_base_args`) makes ssh
    abort if ANY of the forwards cannot be bound, so a partially-working tunnel
    is impossible — either every port is forwarded or the process exits and the
    caller sees a failure rather than a half-open stack.
    """
    if not targets:
        raise TunnelError("a tunnel needs at least one forwarding target")
    args = build_ssh_base_args(key_path, connect_timeout)
    args.append("-N")
    for target in targets:
        args.extend(["-L", target.forwarding_spec])
    args.append(f"{endpoint.username}@{endpoint.host}")
    if endpoint.port != 22:
        args.extend(["-p", str(endpoint.port)])
    return args


def build_ssh_exec_args(
    endpoint: SshEndpoint,
    key_path: Optional[str],
    command: str,
    connect_timeout: int = int(_CONNECT_TIMEOUT),
) -> list[str]:
    """Build an ``ssh ... <command>`` command line to run on the pod.

    Same reliability flags as the tunnels (BatchMode, accept-new host keys,
    keepalives, connect timeout). The command is passed as a single argument
    so the remote shell is the only parser — no local shell interpolation.
    """
    args = build_ssh_base_args(key_path, connect_timeout)
    args.append(f"{endpoint.username}@{endpoint.host}")
    if endpoint.port != 22:
        args.extend(["-p", str(endpoint.port)])
    args.append(command)
    return args


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if *port* on *host* is not currently bound.

    Used to detect a local port conflict before starting a tunnel.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True


def can_connect(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Return True if a TCP connection to *host*:*port* can be established.

    Used as a liveness probe to detect a "half-dead" tunnel whose process is
    still alive but whose forwarding is broken.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        except OSError:
            return False
        return True


# Signature of a process spawner: (argv, cwd, env) -> subprocess.Popen-like.
Spawner = Callable[[Sequence[str], Optional[str], Optional[dict]], subprocess.Popen]


def _default_spawner(
    argv: Sequence[str],
    cwd: Optional[str],
    env: Optional[dict],
) -> subprocess.Popen:
    # stderr is captured to a temp file (not DEVNULL) so callers can surface
    # the real ssh diagnostic (connection refused, key rejected, routing
    # failure) after a failed attempt instead of diagnosing blind.
    stderr = tempfile.TemporaryFile(mode="w+b")
    return winproc.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        stdin=subprocess.DEVNULL,
    )


class TunnelManager:
    """Supervise one or more local SSH forwarding tunnels.

    Parameters
    ----------
    spawner:
        Optional process spawner. Defaults to :func:`_default_spawner`, which
        launches ``ssh.exe`` hidden. Inject a fake in tests.
    state:
        Optional :class:`~launcher.runtime_state.RuntimeState` for recording
        spawned PIDs so a later CLI invocation can stop them. When None, PIDs
        are tracked in-memory only (used by unit tests).
    """

    def __init__(self, spawner: Spawner = _default_spawner, state=None) -> None:
        self._spawner = spawner
        self._processes: dict[str, subprocess.Popen] = {}
        #: The targets each named tunnel forwards. Stored so liveness can be
        #: checked against every port without the caller having to remember
        #: which ones it asked for — the multi-port case would otherwise be
        #: easy to get wrong by checking only the first.
        self._targets: dict[str, tuple[TunnelTarget, ...]] = {}
        self._state = state

    def start(
        self,
        name: str,
        target: TunnelTarget,
        endpoint: SshEndpoint,
        key_path: Optional[str],
        connect_timeout: int = int(_CONNECT_TIMEOUT),
    ) -> None:
        """Start a single-forward tunnel named *name*.

        The one-target spelling of :meth:`start_many`, kept because most stacks
        need exactly one port and ``start(name, target, ...)`` reads better
        there than a one-element sequence.
        """
        self.start_many(name, (target,), endpoint, key_path, connect_timeout)

    def start_many(
        self,
        name: str,
        targets: Sequence[TunnelTarget],
        endpoint: SshEndpoint,
        key_path: Optional[str],
        connect_timeout: int = int(_CONNECT_TIMEOUT),
    ) -> None:
        """Start a tunnel named *name* forwarding every target in *targets*.

        Replaces any existing tunnel of that name. A launcher-owned tunnel of
        the same name from a previous CLI invocation (recorded in RuntimeState)
        is terminated first so its local ports can be re-bound. External
        processes are never touched: if a port is still occupied afterwards, a
        conflict is raised.

        Every local port is checked BEFORE ssh is spawned, so a conflict is
        reported against the port that caused it rather than as a generic
        "forward failed" from a process that has already exited.
        """
        targets = tuple(targets)
        if not targets:
            raise TunnelError(f"tunnel {name!r} needs at least one target")
        self._release_existing(name)
        for target in targets:
            if not is_port_free(target.local_port):
                raise TunnelError(
                    f"Local port {target.local_port} is already in use; "
                    "free it or choose a different port."
                )
        argv = build_tunnel_args_multi(targets, endpoint, key_path, connect_timeout)
        proc = self._spawner(argv, None, None)
        self._processes[name] = proc
        self._targets[name] = targets
        self._record(name, proc, targets[0].local_port)

    def _release_existing(self, name: str) -> None:
        """Release any existing launcher-owned tunnel *name* before re-binding.

        Covers both a tunnel spawned by this process and one recorded in
        RuntimeState by a previous CLI invocation that is still alive. Only
        recorded (launcher-owned) PIDs are ever terminated.
        """
        if self._state is not None:
            from . import runtime_state

            entry = self._state.load().get(f"tunnels:{name}")
            if entry is not None and runtime_state.pid_is_alive(entry.pid):
                try:
                    runtime_state.terminate_recorded_pid(entry)
                except OSError:
                    pass
        self.stop(name)

    def _record(self, name: str, proc: subprocess.Popen, port: int) -> None:
        if self._state is None:
            return
        from . import runtime_state

        entries = self._state.load()
        entries[f"tunnels:{name}"] = runtime_state.ProcessEntry(
            pid=proc.pid,
            label=f"ssh tunnel {name}",
            port=port,
            marker="ssh-tunnel",
            created_at=time.time(),
        )
        self._state.save(entries)

    def is_alive(self, name: str, target: Optional[TunnelTarget] = None) -> bool:
        """Return True if the tunnel *name* is running and forwarding.

        A tunnel is considered alive only if its process has not exited AND a
        TCP connection to its local port succeeds (guards against half-dead
        tunnels).

        With *target* given, that port alone is probed. With *target* omitted,
        every port the tunnel was started with is probed and ALL must answer —
        which is what a multi-port stack needs, since a tunnel forwarding two
        ports is only useful when both work.
        """
        proc = self._processes.get(name)
        if proc is None:
            return False
        if proc.poll() is not None:
            return False
        targets = (target,) if target is not None else self._targets.get(name, ())
        if not targets:
            return False
        return all(can_connect(t.local_port) for t in targets)

    def stderr_text(self, name: str) -> str:
        """Return the captured stderr of tunnel *name*, or an empty string.

        The real spawner redirects ``ssh`` stderr to a temp file; after a
        failed attempt this surfaces the actual diagnostic (e.g. "Connection
        refused", "Permission denied (publickey)", "Connection timed out").
        Fake/injected spawners that return a Popen without a ``stderr`` handle
        yield an empty string.
        """
        proc = self._processes.get(name)
        if proc is None:
            return ""
        stderr = getattr(proc, "stderr", None)
        if stderr is None or getattr(stderr, "closed", False):
            return ""
        try:
            stderr.seek(0)
            data = stderr.read()
        except (OSError, ValueError):
            return ""
        if isinstance(data, bytes):
            return data.decode("utf-8", "replace").strip()
        return str(data).strip()

    def wait_alive(
        self,
        name: str,
        target: Optional[TunnelTarget] = None,
        timeout: float = 15.0,
        interval: float = 0.5,
    ) -> bool:
        """Poll until the tunnel *name* is alive or *timeout* elapses.

        SSH needs a moment to authenticate and bind the local ``-L`` port after
        spawn, so a single immediate ``is_alive`` probe is unreliable. This
        retries for a bounded grace period and returns the final liveness.

        *target* is forwarded to :meth:`is_alive`; omitting it waits for every
        port the tunnel was started with.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.is_alive(name, target):
                return True
            proc = self._processes.get(name)
            if proc is not None and proc.poll() is not None:
                # Process exited (e.g. auth failure) — not merely slow to bind.
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def targets(self, name: str) -> tuple[TunnelTarget, ...]:
        """Return the targets tunnel *name* was started with (empty if none)."""
        return self._targets.get(name, ())

    def stop(self, name: str) -> None:
        """Stop the tunnel *name* if it exists, freeing its local ports."""
        proc = self._processes.pop(name, None)
        self._targets.pop(name, None)
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._close_stderr(proc)
        self._unrecord(name)

    @staticmethod
    def _close_stderr(proc: subprocess.Popen) -> None:
        """Close a captured stderr temp file, if any, without raising."""
        stderr = getattr(proc, "stderr", None)
        if stderr is not None and not getattr(stderr, "closed", False):
            try:
                stderr.close()
            except OSError:
                pass

    def _unrecord(self, name: str) -> None:
        if self._state is None:
            return
        entries = self._state.load()
        entries.pop(f"tunnels:{name}", None)
        self._state.save(entries)

    def stop_all(self) -> None:
        """Stop every managed tunnel."""
        for name in list(self._processes):
            self.stop(name)
        self._targets.clear()
        if self._state is not None:
            entries = self._state.load()
            for key in [k for k in entries if k.startswith("tunnels:")]:
                entries.pop(key, None)
            self._state.save(entries)
