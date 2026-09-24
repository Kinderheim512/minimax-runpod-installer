"""Shared fixtures for the launcher test suite."""

import os
import subprocess

import pytest

#: Hosts a test is allowed to reach. Everything else is refused instantly by
#: :func:`_no_outbound_network`.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"})

#: Remote shells the suite must never launch. A real ``ssh`` aimed at a fake
#: pod endpoint (``1.2.3.4:2222`` in the orchestrator tests) costs the whole
#: ``ConnectTimeout`` per probe — 5 probes × ~12 s per comfy-start test before
#: the version probe was stubbed out of the tests.
_REMOTE_CLIENTS = frozenset(
    {"ssh", "scp", "sftp", "ssh.exe", "scp.exe", "sftp.exe"}
)


@pytest.fixture(autouse=True)
def _isolate_runtime_state(tmp_path, monkeypatch):
    """Point the default RuntimeState path at a per-test temp directory.

    The launcher records live process PIDs in %TEMP%\\openfox-forge\\runtime.json
    on the machine where it runs. Tests must never read, clear, or terminate
    through that real file, so any test that constructs a default RuntimeState()
    (by omitting the state argument) is redirected to an isolated path.
    """
    monkeypatch.setenv("MINIMAX_LAUNCHER_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _isolate_credentials_dir(tmp_path, monkeypatch):
    """Point the credential store at a per-test directory, on every platform.

    The store is per-user state whose location is platform-dependent
    (``%APPDATA%`` on Windows, ``~/Library/Application Support`` on macOS,
    ``$XDG_DATA_HOME`` on Linux), so all of them are redirected here:
    redirecting only APPDATA left the POSIX paths pointing at the operator's
    real home.

    On POSIX the backend is pinned to ``file`` too. Auto-detection would pick
    the login Keychain on macOS, whose ``security`` CLI **blocks** on a runner
    with no interactive session (waiting for an unlock prompt that never
    comes — a four-hour CI hang), and the Secret Service on Linux, which needs
    a session bus CI does not have. Windows keeps DPAPI, so the
    DPAPI-specific tests still exercise the real thing.
    """
    monkeypatch.setenv("MINIMAX_LAUNCHER_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    if os.name != "nt":
        monkeypatch.setenv("MINIMAX_LAUNCHER_CREDENTIAL_BACKEND", "file")


@pytest.fixture(autouse=True)
def _isolate_launcher_home(tmp_path, monkeypatch):
    """Keep the launcher home (pod registry / provisioning lock) per-test.

    Tests must never read or write the operator's real ``pod.json`` or
    ``provision.lock``. Any test that needs a custom home can monkeypatch
    ``MINIMAX_LAUNCHER_HOME`` after this fixture runs.
    """
    monkeypatch.delenv("MINIMAX_LAUNCHER_HOME", raising=False)


@pytest.fixture(autouse=True)
def _isolate_ambient_launcher_env(monkeypatch):
    """Drop the ambient launcher variables a real session exports.

    ``main()`` and ``load_config()`` fall back to ``LAUNCHER_STACK`` / ``LLM_PROFILE``
    when no explicit value is given, so a developer shell that has one of them
    set (e.g. an OpenFox session started with ``LAUNCHER_STACK=llamacpp``) changed
    what the CLI *defaults* to and broke a test that asserted the default. The
    suite must not depend on the machine it runs on: tests that need a value
    pass it explicitly.
    """
    for name in ("LAUNCHER_STACK", "LLM_PROFILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_pod_presence_sleep(monkeypatch):
    """Never wait out the pod-presence retry window during tests.

    A freshly created pod is re-checked a few seconds apart before the
    launcher concludes it is gone (RunPod indexes new pods with a delay).
    The tests exercise that retry, so the delay itself is zeroed.
    """
    from launcher import orchestrator

    monkeypatch.setattr(orchestrator, "_POD_PRESENCE_DELAY", 0.0)


@pytest.fixture(autouse=True)
def _offline_health_probes(monkeypatch):
    """The readiness probes answer instantly, without a socket.

    ``run_diagnostics`` / ``operational_status`` probe local ports (vLLM 8000,
    ComfyUI 8188, llama.cpp 8080, the training desktop). On a machine where a
    *refused* loopback connect does not return instantly — a local filter, a
    security agent holding the port open, a tunnel that accepts but never
    answers — every doctor test paid seconds for a status it never asserts,
    and the llama.cpp tests paid the full 5 s probe timeout per
    ``run_diagnostics`` call.

    The stubs reproduce exactly the unreachable outcome the tests already
    assert (``WARN`` / "not answering"); the probes themselves keep their own
    unit tests in ``tests/test_health.py``, which import them by name and
    inject their own transport. ``launcher.doctor`` and ``launcher.main`` bind
    the probes at import time, so both namespaces are patched — a test that
    wants another outcome patches them after this fixture and wins.
    """
    from launcher import doctor, main
    from launcher.health import HealthError

    def _unreachable(base_url, *args, **kwargs):
        raise HealthError(
            f"GET {base_url} failed: connection refused (offline test suite)"
        )

    def _not_ready(*args, **kwargs):
        return False

    for module in (doctor, main):
        for name, stub in (
            ("check_comfy", _not_ready),
            ("check_train", _not_ready),
        ):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, stub)


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch):
    """Keep the suite offline: loopback sockets only, and no remote shell.

    A test that silently reaches the real world is not a slow test, it is a
    flaky one: it passes on a laptop with no route and hangs for the whole
    connect timeout on a machine with a firewall that drops the packets. Both
    failure modes were observed here (SSH probes against a fake pod endpoint,
    health probes against ports held open by an unrelated service), so the
    guard is deliberate: an outbound connect fails immediately, and a
    ``ssh``/``scp``/``sftp`` spawn raises. Loopback stays available — the
    tunnel/port tests need real local sockets — and the failure is an
    ``OSError``, the shape every caller already handles.
    """
    import socket

    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        if (
            self.family in (socket.AF_INET, socket.AF_INET6)
            and isinstance(address, tuple)
            and address
            and address[0] not in _LOOPBACK_HOSTS
        ):
            raise OSError(
                10061,
                f"Connection refused (test suite: {address[0]} is out of "
                "reach — outbound network is disabled)",
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)

    real_popen = subprocess.Popen

    def guarded_popen(args, *call_args, **kwargs):
        argv = args.split() if isinstance(args, str) else list(args)
        if argv:
            name = os.path.basename(str(argv[0])).lower()
            if name in _REMOTE_CLIENTS:
                raise OSError(
                    f"test suite: {name} must not be spawned (inject a fake "
                    "spawner instead)"
                )
        return real_popen(args, *call_args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
