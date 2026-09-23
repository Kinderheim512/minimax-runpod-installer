"""Tests for the port ownership helpers (launcher.portctl)."""

from launcher import portctl


class _Proc:
    def __init__(self, stdout=b"", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


NETSTAT_SAMPLE = (
    "Proto  Local Address          Foreign Address        State           PID\n"
    "  TCP    0.0.0.0:135          0.0.0.0:0              LISTENING       100\n"
    "  TCP    0.0.0.0:10369        0.0.0.0:0              LISTENING       4321\n"
    "  TCP    127.0.0.1:10370      127.0.0.1:10369        ESTABLISHED     4321\n"
    "  TCP    1.2.3.4:443          10.0.0.1:51234         ESTABLISHED     200\n"
    "  UDP    0.0.0.0:5353         0.0.0.0:*                          300\n"
)


def _run_returning(stdout):
    def fake_run(cmd, **kwargs):
        data = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
        return _Proc(stdout=data)

    return fake_run


def test_find_listener_pid_parses_netstat(monkeypatch) -> None:
    monkeypatch.setattr(portctl.os, "name", "nt")
    monkeypatch.setattr(portctl, "_run", _run_returning(NETSTAT_SAMPLE))
    assert portctl.find_listener_pid(10369) == 4321


def test_find_listener_pid_no_match_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(portctl.os, "name", "nt")
    monkeypatch.setattr(portctl, "_run", _run_returning(NETSTAT_SAMPLE))
    assert portctl.find_listener_pid(9999) is None


def test_find_listener_pid_french_locale_state(monkeypatch) -> None:
    """French Windows: the state word is 'ÉCOUTE', foreign is 0.0.0.0:0."""
    sample = "  TCP    0.0.0.0:10369        0.0.0.0:0              ÉCOUTE      4321\n"
    monkeypatch.setattr(portctl.os, "name", "nt")
    monkeypatch.setattr(portctl, "_run", _run_returning(sample))
    assert portctl.find_listener_pid(10369) == 4321


def test_find_listener_pid_ignores_established_and_other_ports(monkeypatch) -> None:
    sample = (
        "  TCP    127.0.0.1:10369      127.0.0.1:51234        ESTABLISHED     4321\n"
    )
    monkeypatch.setattr(portctl.os, "name", "nt")
    monkeypatch.setattr(portctl, "_run", _run_returning(sample))
    assert portctl.find_listener_pid(10369) is None


def test_find_listener_pid_command_failure_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(portctl.os, "name", "nt")

    def boom(cmd, **kwargs):
        raise OSError("netstat not found")

    monkeypatch.setattr(portctl, "_run", boom)
    assert portctl.find_listener_pid(10369) is None


def test_find_listener_pid_non_windows_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(portctl.os, "name", "posix")
    assert portctl.find_listener_pid(10369) is None


def test_get_process_name_parses_tasklist_csv(monkeypatch) -> None:
    monkeypatch.setattr(portctl.os, "name", "nt")
    monkeypatch.setattr(
        portctl, "_run",
        _run_returning('"node.exe","4321","Console","1","12,345 K"\n'),
    )
    assert portctl.get_process_name(4321) == "node.exe"


def test_get_process_name_no_row_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(portctl.os, "name", "nt")
    monkeypatch.setattr(portctl, "_run", _run_returning(""))
    assert portctl.get_process_name(4321) is None


def test_get_process_name_none_pid_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(portctl.os, "name", "nt")
    called = []
    monkeypatch.setattr(portctl, "_run", lambda cmd, **kw: called.append(1))
    assert portctl.get_process_name(None) is None
    assert called == []


