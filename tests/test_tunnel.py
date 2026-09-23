"""Tests for the SSH tunnel manager (credential-independent)."""

import itertools
import socket

import pytest

from launcher.tunnel import (
    SshEndpoint,
    TunnelError,
    TunnelManager,
    TunnelTarget,
    build_ssh_base_args,
    build_tunnel_args,
    build_tunnel_args_multi,
    from_runpod_endpoint,
    is_port_free,
)


def test_from_runpod_endpoint_converts_shape() -> None:
    # Simulate a RunPod SshEndpoint (same host/port/username shape).
    class FakeRunPodEndpoint:
        host = "1.2.3.4"
        port = 2222
        username = "root"

    endpoint = from_runpod_endpoint(FakeRunPodEndpoint())
    assert endpoint == SshEndpoint("1.2.3.4", 2222, "root")


def test_from_runpod_endpoint_defaults() -> None:
    class FakeRunPodEndpoint:
        host = "h.example"

    endpoint = from_runpod_endpoint(FakeRunPodEndpoint())
    assert endpoint.port == 22
    assert endpoint.username == "root"


def test_from_runpod_endpoint_requires_host() -> None:
    class Empty:
        pass

    with pytest.raises(TunnelError):
        from_runpod_endpoint(Empty())


def test_runpod_and_tunnel_endpoints_are_structurally_compatible() -> None:
    # The whole point of the adapter: launcher.runpod.SshEndpoint and
    # tunnel.SshEndpoint have identical field names and semantics.
    from launcher.runpod import SshEndpoint as RunPodSshEndpoint

    rp = RunPodSshEndpoint(host="h", port=22, username="root")
    converted = from_runpod_endpoint(rp)
    assert (converted.host, converted.port, converted.username) == ("h", 22, "root")


def test_build_ssh_base_args_includes_reliability_flags() -> None:
    args = build_ssh_base_args(None)
    assert args[0] == "ssh"
    assert "BatchMode=yes" in args
    assert "StrictHostKeyChecking=accept-new" in args
    assert "ServerAliveInterval=15" in args
    assert "ServerAliveCountMax=3" in args
    assert "ExitOnForwardFailure=yes" in args


def test_build_ssh_base_args_adds_key_path() -> None:
    args = build_ssh_base_args(r"C:\Users\me\.ssh\id_ed25519")
    idx = args.index("-i")
    assert args[idx + 1] == r"C:\Users\me\.ssh\id_ed25519"


def test_build_ssh_base_args_omits_key_when_none() -> None:
    args = build_ssh_base_args(None)
    assert "-i" not in args


def test_key_path_with_spaces_is_single_argv_element() -> None:
    # A Windows key path with spaces must be passed as ONE argv element so the
    # ssh.exe process receives it intact (no shell is involved).
    key_path = r"C:\Users\My Name\.ssh\id_ed25519"
    args = build_ssh_base_args(key_path)
    idx = args.index("-i")
    assert args[idx + 1] == key_path
    # The path must appear verbatim and not be split on whitespace.
    assert args.count(key_path) == 1


def test_build_tunnel_args_default_port() -> None:
    target = TunnelTarget(local_port=8000, remote_host="127.0.0.1", remote_port=8000)
    endpoint = SshEndpoint(host="1.2.3.4", username="root")
    args = build_tunnel_args(target, endpoint, None)

    assert "-N" in args
    assert "-L" in args
    assert "8000:127.0.0.1:8000" in args
    assert "root@1.2.3.4" in args
    # Default SSH port 22 => no explicit -p flag.
    assert "-p" not in args


def test_build_tunnel_args_non_default_port() -> None:
    target = TunnelTarget(local_port=8000, remote_host="127.0.0.1", remote_port=8000)
    endpoint = SshEndpoint(host="1.2.3.4", port=2222, username="root")
    args = build_tunnel_args(target, endpoint, None)

    idx = args.index("-p")
    assert args[idx + 1] == "2222"


def test_tunnel_target_forwarding_spec() -> None:
    target = TunnelTarget(8000, "127.0.0.1", 8080)
    assert target.forwarding_spec == "8000:127.0.0.1:8080"


def test_is_port_free_true_when_unbound() -> None:
    # Bind an ephemeral port, get its number, close it, then check it is free.
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert is_port_free(port) is True


def test_is_port_free_false_when_bound() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        assert is_port_free(port) is False


_fake_pid_counter = itertools.count(100000)


class _FakeProc:
    def __init__(self, returncode=None, pid=None):
        self._returncode = returncode
        self.pid = pid if pid is not None else next(_fake_pid_counter)
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def wait(self, timeout=None):
        return self._returncode

    def kill(self):
        self.killed = True


def _spawner(argv, cwd, env):
    return _FakeProc()


def _free_port() -> int:
    """Return an OS-assigned TCP port that is currently free."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_tunnel_start_builds_and_records_process() -> None:
    mgr = TunnelManager(spawner=_spawner)
    target = TunnelTarget(_free_port(), "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")

    mgr.start("llm", target, endpoint, None)

    assert "llm" in mgr._processes


def test_tunnel_start_raises_when_port_in_use() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

        mgr = TunnelManager(spawner=_spawner)
        target = TunnelTarget(port, "127.0.0.1", 8000)
        endpoint = SshEndpoint("1.2.3.4", 22, "root")

        with pytest.raises(TunnelError):
            mgr.start("llm", target, endpoint, None)


def test_tunnel_stop_terminates_and_removes() -> None:
    mgr = TunnelManager(spawner=_spawner)
    target = TunnelTarget(_free_port(), "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")
    mgr.start("llm", target, endpoint, None)

    proc = mgr._processes["llm"]
    mgr.stop("llm")

    assert proc.terminated is True
    assert "llm" not in mgr._processes


def test_tunnel_stop_missing_is_noop() -> None:
    mgr = TunnelManager(spawner=_spawner)
    mgr.stop("nope")  # should not raise


def test_tunnel_stop_kills_when_terminate_does_not_reap() -> None:
    """A half-dead ssh that ignores terminate() is killed so its port is freed."""
    import subprocess

    class StubbornProc:
        def __init__(self):
            self.pid = 123456
            self._alive = True
            self.terminated = False
            self.killed = False

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self.terminated = True
            # does NOT die: poll() still returns None afterward

        def wait(self, timeout=None):
            self._alive = False
            raise subprocess.TimeoutExpired("ssh", timeout)

        def kill(self):
            self.killed = True
            self._alive = False

    proc = StubbornProc()
    mgr = TunnelManager(spawner=lambda argv, cwd, env: proc)
    target = TunnelTarget(_free_port(), "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")
    mgr.start("llm", target, endpoint, None)

    mgr.stop("llm")

    assert proc.terminated is True
    assert proc.killed is True
    assert "llm" not in mgr._processes


def test_tunnel_stderr_text_returns_captured_stderr() -> None:
    """The real spawner captures ssh stderr so a failed attempt is diagnosable."""
    import io

    class ProcWithStderr:
        def __init__(self):
            self.pid = 999
            self.stderr = io.BytesIO(b"ssh: connect to host 1.2.3.4 port 22: Connection refused\r\n")

        def poll(self):
            return 1

    mgr = TunnelManager(spawner=lambda argv, cwd, env: ProcWithStderr())
    target = TunnelTarget(_free_port(), "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")
    mgr.start("llm", target, endpoint, None)

    text = mgr.stderr_text("llm")

    assert "Connection refused" in text


def test_tunnel_stderr_text_empty_when_no_stderr_handle() -> None:
    """Fake/injected spawners without a stderr handle yield an empty string."""
    mgr = TunnelManager(spawner=_spawner)
    target = TunnelTarget(_free_port(), "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")
    mgr.start("llm", target, endpoint, None)

    assert mgr.stderr_text("llm") == ""


def test_tunnel_stop_all() -> None:
    mgr = TunnelManager(spawner=_spawner)
    target = TunnelTarget(_free_port(), "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")
    mgr.start("a", target, endpoint, None)
    mgr.start("b", target, endpoint, None)

    mgr.stop_all()

    assert mgr._processes == {}


def test_is_alive_false_when_no_process() -> None:
    mgr = TunnelManager(spawner=_spawner)
    target = TunnelTarget(8000, "127.0.0.1", 8000)
    assert mgr.is_alive("missing", target) is False


def test_is_alive_false_when_process_exited() -> None:
    mgr = TunnelManager(spawner=lambda argv, cwd, env: _FakeProc(returncode=1))
    target = TunnelTarget(_free_port(), "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")
    mgr.start("llm", target, endpoint, None)

    assert mgr.is_alive("llm", target) is False


def test_wait_alive_returns_true_when_port_binds_late(monkeypatch) -> None:
    """SSH binds the local -L port after a delay; wait_alive must retry."""
    connect_calls = {"n": 0}

    def fake_can_connect(port, host="127.0.0.1", timeout=1.0):
        connect_calls["n"] += 1
        # Fail the first probe (port not yet bound), succeed after.
        return connect_calls["n"] >= 2

    monkeypatch.setattr("launcher.tunnel.can_connect", fake_can_connect)
    monkeypatch.setattr("launcher.tunnel.time.sleep", lambda s: None)

    mgr = TunnelManager(spawner=_spawner)
    target = TunnelTarget(_free_port(), "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")
    mgr.start("llm", target, endpoint, None)

    assert mgr.wait_alive("llm", target, timeout=5, interval=0.1) is True


def test_wait_alive_returns_false_when_process_exited(monkeypatch) -> None:
    monkeypatch.setattr("launcher.tunnel.time.sleep", lambda s: None)
    mgr = TunnelManager(spawner=lambda argv, cwd, env: _FakeProc(returncode=1))
    target = TunnelTarget(_free_port(), "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")
    mgr.start("llm", target, endpoint, None)

    assert mgr.wait_alive("llm", target, timeout=5, interval=0.1) is False


def test_wait_alive_returns_false_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr("launcher.tunnel.can_connect", lambda port, host="127.0.0.1", timeout=1.0: False)
    monkeypatch.setattr("launcher.tunnel.time.sleep", lambda s: None)
    mgr = TunnelManager(spawner=_spawner)
    target = TunnelTarget(_free_port(), "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")
    mgr.start("llm", target, endpoint, None)

    assert mgr.wait_alive("llm", target, timeout=0.01, interval=0.1) is False


def test_tunnel_start_replaces_prior_invocation_tunnel_holding_port(monkeypatch, tmp_path) -> None:
    """A live tunnel recorded by a previous CLI invocation is replaced, not a conflict."""
    import socket

    from launcher import runtime_state
    from launcher.runtime_state import ProcessEntry, RuntimeState

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    try:
        state = RuntimeState(tmp_path / "runtime.json")
        state.save(
            {
                "tunnels:llm": ProcessEntry(
                    pid=424242,
                    label="ssh tunnel llm",
                    port=port,
                    marker="ssh-tunnel",
                    created_at=1.0,
                )
            }
        )
        terminated = []

        def fake_terminate(pid):
            terminated.append(pid)
            holder.close()  # the OS releases the port when the old tunnel exits
            return True

        monkeypatch.setattr(runtime_state, "pid_is_alive", lambda pid: pid == 424242)
        monkeypatch.setattr(runtime_state, "terminate_pid", fake_terminate)

        spawned = []

        def spawner(argv, cwd, env):
            proc = _FakeProc()
            spawned.append(proc)
            return proc

        mgr = TunnelManager(spawner=spawner, state=state)
        target = TunnelTarget(port, "127.0.0.1", 8000)
        endpoint = SshEndpoint("1.2.3.4", 22, "root")

        mgr.start("llm", target, endpoint, None)  # must not raise "already in use"

        assert terminated == [424242]
        assert len(spawned) == 1
        assert mgr._processes["llm"] is spawned[0]
        assert state.load()["tunnels:llm"].pid == spawned[0].pid
    finally:
        holder.close()


def test_tunnel_start_external_port_occupant_still_conflict(monkeypatch, tmp_path) -> None:
    """An unrecorded external occupant is never adopted or terminated."""
    import socket

    from launcher import runtime_state
    from launcher.runtime_state import RuntimeState

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    try:
        state = RuntimeState(tmp_path / "runtime.json")  # empty: nothing recorded
        terminated = []
        monkeypatch.setattr(runtime_state, "pid_is_alive", lambda pid: True)
        monkeypatch.setattr(
            runtime_state, "terminate_pid", lambda pid: terminated.append(pid) or True
        )

        mgr = TunnelManager(spawner=_spawner, state=state)
        target = TunnelTarget(port, "127.0.0.1", 8000)
        endpoint = SshEndpoint("1.2.3.4", 22, "root")

        with pytest.raises(TunnelError, match="already in use"):
            mgr.start("llm", target, endpoint, None)

        assert terminated == []
    finally:
        holder.close()


def test_tunnel_start_stale_recorded_pid_does_not_block(monkeypatch, tmp_path) -> None:
    """A dead recorded PID must not make a free port appear occupied."""
    import socket

    from launcher import runtime_state
    from launcher.runtime_state import ProcessEntry, RuntimeState

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # free again once the context closes

    state = RuntimeState(tmp_path / "runtime.json")
    state.save(
        {
            "tunnels:llm": ProcessEntry(
                pid=999999, label="ssh tunnel llm", port=port, marker="ssh-tunnel", created_at=1.0
            )
        }
    )
    terminated = []
    monkeypatch.setattr(runtime_state, "pid_is_alive", lambda pid: False)
    monkeypatch.setattr(
        runtime_state, "terminate_pid", lambda pid: terminated.append(pid) or True
    )

    mgr = TunnelManager(spawner=_spawner, state=state)
    target = TunnelTarget(port, "127.0.0.1", 8000)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")

    mgr.start("llm", target, endpoint, None)  # proceeds: stale entry is ignored

    assert terminated == []
    assert "llm" in mgr._processes
    assert state.load()["tunnels:llm"].pid == mgr._processes["llm"].pid


# ---------------------------------------------------------------------------
# Multi-port tunnels (the `train` stack forwards two ports on one connection)
# ---------------------------------------------------------------------------


def test_build_tunnel_args_multi_emits_one_L_per_target() -> None:
    targets = [
        TunnelTarget(6080, "127.0.0.1", 6080),
        TunnelTarget(8080, "127.0.0.1", 8080),
    ]
    args = build_tunnel_args_multi(targets, SshEndpoint("1.2.3.4", 22, "root"), None)

    # One -N, then -L immediately followed by its spec, in the order given.
    assert args.count("-N") == 1
    assert args.count("-L") == 2
    first = args.index("-L")
    assert args[first + 1] == "6080:127.0.0.1:6080"
    second = args.index("-L", first + 1)
    assert args[second + 1] == "8080:127.0.0.1:8080"
    assert args[-1] == "root@1.2.3.4"


def test_build_tunnel_args_single_matches_multi_with_one_target() -> None:
    """The singular spelling must not drift from the plural one."""
    target = TunnelTarget(8188, "127.0.0.1", 8188)
    endpoint = SshEndpoint("1.2.3.4", 22, "root")
    assert build_tunnel_args(target, endpoint, None) == build_tunnel_args_multi(
        [target], endpoint, None
    )


def test_build_tunnel_args_multi_includes_the_port_when_not_22() -> None:
    targets = [TunnelTarget(6080, "127.0.0.1", 6080)]
    args = build_tunnel_args_multi(targets, SshEndpoint("1.2.3.4", 2222, "root"), None)
    assert args[-2:] == ["-p", "2222"]


def test_build_tunnel_args_multi_rejects_no_targets() -> None:
    with pytest.raises(TunnelError):
        build_tunnel_args_multi([], SshEndpoint("1.2.3.4", 22, "root"), None)


def test_start_many_spawns_one_process_for_every_port() -> None:
    argv_seen: list[list[str]] = []

    def spawner(argv, cwd, env):
        argv_seen.append(list(argv))
        return _FakeProc()

    mgr = TunnelManager(spawner=spawner)
    targets = [
        TunnelTarget(_free_port(), "127.0.0.1", 6080),
        TunnelTarget(_free_port(), "127.0.0.1", 8080),
    ]
    mgr.start_many("train", targets, SshEndpoint("1.2.3.4", 22, "root"), None)

    assert len(argv_seen) == 1, "two ports must not become two ssh processes"
    assert argv_seen[0].count("-L") == 2
    assert mgr.targets("train") == tuple(targets)


def test_start_many_checks_every_port_not_just_the_first() -> None:
    """A conflict on the SECOND port must fail, and before ssh is spawned."""
    spawned = []

    def spawner(argv, cwd, env):
        spawned.append(argv)
        return _FakeProc()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        busy = s.getsockname()[1]

        mgr = TunnelManager(spawner=spawner)
        targets = [
            TunnelTarget(_free_port(), "127.0.0.1", 6080),
            TunnelTarget(busy, "127.0.0.1", 8080),
        ]
        with pytest.raises(TunnelError) as excinfo:
            mgr.start_many("train", targets, SshEndpoint("1.2.3.4", 22, "root"), None)

    assert str(busy) in str(excinfo.value)
    assert spawned == [], "ssh must not be spawned when a port is already taken"


def test_start_many_rejects_no_targets() -> None:
    mgr = TunnelManager(spawner=_spawner)
    with pytest.raises(TunnelError):
        mgr.start_many("train", [], SshEndpoint("1.2.3.4", 22, "root"), None)


def test_is_alive_without_target_requires_every_port(monkeypatch) -> None:
    from launcher import tunnel as tunnel_module

    mgr = TunnelManager(spawner=_spawner)
    targets = [
        TunnelTarget(_free_port(), "127.0.0.1", 6080),
        TunnelTarget(_free_port(), "127.0.0.1", 8080),
    ]
    mgr.start_many("train", targets, SshEndpoint("1.2.3.4", 22, "root"), None)

    reachable: set[int] = {t.local_port for t in targets}
    monkeypatch.setattr(
        tunnel_module, "can_connect", lambda port, host="127.0.0.1", timeout=1.0: port in reachable
    )
    assert mgr.is_alive("train") is True

    # One port down is enough to make the whole tunnel useless.
    reachable.discard(targets[1].local_port)
    assert mgr.is_alive("train") is False

    # The single-target form still probes only what it was asked about.
    assert mgr.is_alive("train", targets[0]) is True


def test_is_alive_without_a_known_target_is_false() -> None:
    """An unknown name must not be reported alive just because no port answered."""
    mgr = TunnelManager(spawner=_spawner)
    assert mgr.is_alive("nope") is False


def test_stop_forgets_the_targets() -> None:
    mgr = TunnelManager(spawner=_spawner)
    targets = [
        TunnelTarget(_free_port(), "127.0.0.1", 6080),
        TunnelTarget(_free_port(), "127.0.0.1", 8080),
    ]
    mgr.start_many("train", targets, SshEndpoint("1.2.3.4", 22, "root"), None)
    mgr.stop("train")
    assert mgr.targets("train") == ()
    assert mgr.is_alive("train") is False


def test_wait_alive_without_target_waits_for_all_ports(monkeypatch) -> None:
    from launcher import tunnel as tunnel_module

    mgr = TunnelManager(spawner=_spawner)
    targets = [
        TunnelTarget(_free_port(), "127.0.0.1", 6080),
        TunnelTarget(_free_port(), "127.0.0.1", 8080),
    ]
    mgr.start_many("train", targets, SshEndpoint("1.2.3.4", 22, "root"), None)
    monkeypatch.setattr(
        tunnel_module, "can_connect", lambda port, host="127.0.0.1", timeout=1.0: True
    )
    assert mgr.wait_alive("train", timeout=1.0, interval=0.01) is True


# ---------------------------------------------------------------------------
# Stale SSH host keys (RunPod recycles public IP:port across pods)
# ---------------------------------------------------------------------------


def test_is_host_key_changed_matches_openssh_wording() -> None:
    """The real OpenSSH banner for a recycled RunPod endpoint is recognised."""
    from launcher.tunnel import is_host_key_changed

    stderr = (
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\r\n"
        "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\r\n"
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\r\n"
        "IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!\r\n"
        "Offending ECDSA key in C:\\Users\\me\\.ssh\\known_hosts:69\r\n"
        "Host key for [194.68.245.68]:22122 has changed and you have requested"
        " strict checking.\r\n"
        "Host key verification failed.\r\n"
    )
    assert is_host_key_changed(stderr) is True


def test_is_host_key_changed_is_false_for_booting_pod() -> None:
    """A still-booting pod (connection refused/timeout) must NOT purge anything."""
    from launcher.tunnel import is_host_key_changed

    assert is_host_key_changed(
        "ssh: connect to host 1.2.3.4 port 22: Connection refused"
    ) is False
    assert is_host_key_changed("Connection timed out during banner exchange") is False
    assert is_host_key_changed("") is False


def test_host_key_spec_uses_the_bracket_form_off_port_22() -> None:
    from launcher.tunnel import host_key_spec

    assert host_key_spec(SshEndpoint("1.2.3.4", 22122, "root")) == "[1.2.3.4]:22122"
    assert host_key_spec(SshEndpoint("1.2.3.4", 22, "root")) == "1.2.3.4"


def test_purge_known_host_entry_removes_only_that_endpoint(tmp_path) -> None:
    """Only the offending ``[host]:port`` lines go; everything else is kept."""
    from launcher.tunnel import purge_known_host_entry

    path = tmp_path / "known_hosts"
    kept = [
        "[194.68.245.68]:22104 ssh-ed25519 AAAAoldkey",
        "github.com ssh-ed25519 AAAAgithub",
        "[194.68.245.70]:22162 ssh-rsa AAAAother",
    ]
    stale = [
        "[194.68.245.68]:22122 ssh-ed25519 AAAAstale1",
        "[194.68.245.68]:22122 ssh-rsa AAAAstale2",
        "[194.68.245.68]:22122 ecdsa-sha2-nistp256 AAAAstale3",
    ]
    path.write_text(
        "\n".join([kept[0], stale[0], kept[1], stale[1], stale[2], kept[2]]) + "\n",
        encoding="utf-8",
    )

    assert purge_known_host_entry("[194.68.245.68]:22122", path) is True
    assert path.read_text(encoding="utf-8") == "\n".join(kept) + "\n"


def test_purge_known_host_entry_keeps_multi_host_lines_of_other_hosts(tmp_path) -> None:
    """A comma-separated host field is only dropped when it contains the spec."""
    from launcher.tunnel import purge_known_host_entry

    path = tmp_path / "known_hosts"
    path.write_text(
        "1.2.3.4,5.6.7.8 ssh-ed25519 AAAAother\n"
        "9.9.9.9,[1.2.3.4]:22122 ssh-ed25519 AAAAstale\n",
        encoding="utf-8",
    )

    assert purge_known_host_entry("[1.2.3.4]:22122", path) is True
    assert path.read_text(encoding="utf-8") == "1.2.3.4,5.6.7.8 ssh-ed25519 AAAAother\n"


def test_purge_known_host_entry_returns_false_when_absent(tmp_path) -> None:
    from launcher.tunnel import purge_known_host_entry

    path = tmp_path / "known_hosts"
    original = "[194.68.245.68]:22104 ssh-ed25519 AAAAoldkey\n"
    path.write_text(original, encoding="utf-8")

    assert purge_known_host_entry("[194.68.245.68]:22122", path) is False
    assert path.read_text(encoding="utf-8") == original


def test_purge_known_host_entry_missing_file_is_a_noop(tmp_path) -> None:
    from launcher.tunnel import purge_known_host_entry

    path = tmp_path / "absent" / "known_hosts"

    assert purge_known_host_entry("[1.2.3.4]:22122", path) is False
    assert not path.exists()


def test_purge_known_host_entry_keeps_comments_and_blank_lines(tmp_path) -> None:
    from launcher.tunnel import purge_known_host_entry

    path = tmp_path / "known_hosts"
    path.write_text(
        "# a comment\n\n[1.2.3.4]:22122 ssh-ed25519 AAAAstale\n",
        encoding="utf-8",
    )

    assert purge_known_host_entry("[1.2.3.4]:22122", path) is True
    assert path.read_text(encoding="utf-8") == "# a comment\n\n"
