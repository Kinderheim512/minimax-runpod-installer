"""The suite's offline guard: loopback only, and no remote shell.

`tests/conftest.py` refuses every outbound TCP connect and every
``ssh``/``scp``/``sftp`` spawn, so a test cannot silently depend on the
network (which is both a flakiness source and, on a machine that drops the
packets instead of refusing them, a multi-minute stall). These tests pin the
guard down: it must bite where it is meant to, and stay out of the way
everywhere else.
"""

import socket
import subprocess

import pytest


def test_outbound_connect_is_refused_instantly() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(5.0)
        with pytest.raises(OSError):
            sock.connect(("1.2.3.4", 2222))


def test_ssh_spawn_is_refused() -> None:
    with pytest.raises(OSError):
        subprocess.Popen(["ssh", "root@1.2.3.4", "true"])


def test_ssh_spawn_is_refused_for_a_shell_command() -> None:
    with pytest.raises(OSError):
        subprocess.Popen("ssh root@1.2.3.4 true", shell=True)


def test_loopback_connect_still_works() -> None:
    """The guard must not get in the way of a real local socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(5.0)
            client.connect(("127.0.0.1", port))
        conn, _ = listener.accept()
        conn.close()


def test_other_subprocesses_are_still_allowed() -> None:
    """Only the remote shells are blocked — a real process still starts."""
    proc = subprocess.run(
        ["cmd", "/c", "exit 0"] if __import__("sys").platform == "win32" else ["true"],
        capture_output=True,
    )
    assert proc.returncode == 0
