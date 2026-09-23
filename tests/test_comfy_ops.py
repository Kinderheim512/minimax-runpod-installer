"""Tests for the pod-side ComfyUI operations (launcher.comfy_ops).

Fully offline: the SSH exec channel and the ComfyUI HTTP API are
replaced with fakes (monkeypatched instance-level methods), so no pod,
network, GPU, or SSH server is touched.
"""

import io
import json
import os
import subprocess

import pytest

from launcher import comfy_ops
from launcher.comfy_ops import ComfyOps, ComfyOpsError
from launcher.tunnel import SshEndpoint


def _ops(base_url="http://127.0.0.1:8188") -> ComfyOps:
    return ComfyOps(
        endpoint=SshEndpoint(host="1.2.3.4", port=22, username="root"),
        key_path=r"C:\keys\id_ed25519",
        base_url=base_url,
    )


def test_safe_name_accepts_plain_names() -> None:
    for name in ("video_minimax_h3_t2v.json", "C-MMH3-12.json", "a.b-c_D9.json"):
        assert ComfyOps._safe_name(name, "workflow") == name


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "a/b.json",
        "../etc/passwd",
        "a b.json",
        "a;rm -rf /",
        "a|b",
        "x" * 201,
    ],
)
def test_safe_name_rejects_path_traversal_and_metachars(bad) -> None:
    with pytest.raises(ComfyOpsError):
        ComfyOps._safe_name(bad, "workflow")


def test_list_workflows_filters_json(monkeypatch) -> None:
    ops = _ops()
    monkeypatch.setattr(
        ComfyOps, "_exec",
        lambda self, command, timeout: "video_minimax_h3_t2v.json\nREADME.md\nC-MMH3-12.json\n\n",
    )
    assert ops.list_workflows() == [
        "video_minimax_h3_t2v.json",
        "C-MMH3-12.json",
    ]


def test_list_outputs_root_only_by_default(monkeypatch) -> None:
    ops = _ops()
    commands = []

    def fake_exec(self, command, timeout):
        commands.append(command)
        return "flat.png\nvideo\n"

    monkeypatch.setattr(ComfyOps, "_exec", fake_exec)
    assert ops.list_outputs() == ["flat.png", "video"]
    assert commands[0].startswith("ls -1 /opt/ComfyUI/output")


def test_list_outputs_includes_subfolders(monkeypatch) -> None:
    ops = _ops()
    monkeypatch.setattr(
        ComfyOps, "_exec",
        lambda self, command, timeout: (
            "/opt/ComfyUI/output/flat.png\n"
            "/opt/ComfyUI/output/video/MiniMax_H3_00001_.mp4\n"
            "/elsewhere/ignore.mp4\n"
        ),
    )
    assert ops.list_outputs(subfolders=True) == [
        "flat.png",
        "video/MiniMax_H3_00001_.mp4",
    ]


class _FakeViewResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=65536):
        chunk, self._payload = self._payload[:n], self._payload[n:]
        return chunk


def test_download_output_with_subfolder(monkeypatch, tmp_path) -> None:
    ops = _ops()
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        return _FakeViewResp(b"mp4data")

    monkeypatch.setattr("launcher.comfy_ops.urllib.request.urlopen", fake_urlopen)
    target = ops.download_output(
        "MiniMax_H3_00001_.mp4", tmp_path, subfolder="video"
    )
    assert captured["url"].startswith("http://127.0.0.1:8188/view?filename=")
    assert "subfolder=video" in captured["url"]
    assert "type=output" in captured["url"]
    assert target == tmp_path / "video" / "MiniMax_H3_00001_.mp4"
    assert target.read_bytes() == b"mp4data"


def test_download_output_without_subfolder_is_root(monkeypatch, tmp_path) -> None:
    ops = _ops()
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        return _FakeViewResp(b"pngdata")

    monkeypatch.setattr("launcher.comfy_ops.urllib.request.urlopen", fake_urlopen)
    target = ops.download_output("flat.png", tmp_path)
    assert "subfolder=" not in captured["url"]
    assert target == tmp_path / "flat.png"
    assert target.read_bytes() == b"pngdata"


def test_download_output_can_skip_the_local_subfolder_mirror(monkeypatch, tmp_path) -> None:
    """``mirror_subfolder=False`` keeps the pod subfolder in the URL only."""
    ops = _ops()
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        return _FakeViewResp(b"mp4data")

    monkeypatch.setattr("launcher.comfy_ops.urllib.request.urlopen", fake_urlopen)
    target = ops.download_output(
        "MiniMax_H3_00042.mp4",
        tmp_path,
        subfolder="video/MiniMax_H3",
        mirror_subfolder=False,
    )
    assert "subfolder=video/MiniMax_H3" in captured["url"]
    assert target == tmp_path / "MiniMax_H3_00042.mp4"
    assert target.read_bytes() == b"mp4data"
    assert not (tmp_path / "video").exists()


def test_download_output_local_name_only_changes_the_local_file(monkeypatch, tmp_path) -> None:
    """The pod name drives ``/view``; the local name avoids a collision."""
    ops = _ops()
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        return _FakeViewResp(b"mp4data")

    monkeypatch.setattr("launcher.comfy_ops.urllib.request.urlopen", fake_urlopen)
    target = ops.download_output(
        "MiniMax_H3_00042.mp4",
        tmp_path,
        subfolder="video/MiniMax_H3",
        mirror_subfolder=False,
        local_name="MiniMax_H3_00042 (2).mp4",
    )
    assert "filename=MiniMax_H3_00042.mp4" in captured["url"]
    assert "subfolder=video/MiniMax_H3" in captured["url"]
    assert target == tmp_path / "MiniMax_H3_00042 (2).mp4"
    assert target.read_bytes() == b"mp4data"


def test_download_output_rejects_traversal_subfolder_for_local_path(monkeypatch, tmp_path) -> None:
    """A pod-reported subfolder with ``..`` must not steer the download
    outside the target directory: the local path falls back to the root
    while the pod still receives the original value in the /view URL."""
    ops = _ops()
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        return _FakeViewResp(b"data")

    monkeypatch.setattr("launcher.comfy_ops.urllib.request.urlopen", fake_urlopen)
    target = ops.download_output(
        "video.mp4", tmp_path, subfolder="video/../../etc"
    )
    assert target == tmp_path / "video.mp4"
    # urllib.parse.quote keeps "/" unescaped (safe="/") — still HTTP-safe.
    assert "subfolder=video/../../etc" in captured["url"]
    assert not (tmp_path / ".." / "etc").exists()
    assert not (tmp_path.parent / "etc" / "video.mp4").exists()


def test_download_output_rejects_absolute_subfolder(monkeypatch, tmp_path) -> None:
    ops = _ops()
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        return _FakeViewResp(b"data")

    monkeypatch.setattr("launcher.comfy_ops.urllib.request.urlopen", fake_urlopen)
    target = ops.download_output("x.png", tmp_path, subfolder="/etc/passwd")
    # Degraded to a plain relative subfolder — still inside the target dir.
    assert target == tmp_path / "etc" / "passwd" / "x.png"
    assert str(target.resolve()).startswith(str(tmp_path.resolve()) + os.sep)


def test_download_output_rejects_windows_reserved_subfolder(monkeypatch, tmp_path) -> None:
    ops = _ops()
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        return _FakeViewResp(b"data")

    monkeypatch.setattr("launcher.comfy_ops.urllib.request.urlopen", fake_urlopen)
    target = ops.download_output("x.png", tmp_path, subfolder="a|b")
    assert target == tmp_path / "x.png"  # falls back to the root


def test_download_output_accepts_spaces_in_subfolder(monkeypatch, tmp_path) -> None:
    ops = _ops()

    def fake_urlopen(url, timeout=None):
        return _FakeViewResp(b"data")

    monkeypatch.setattr("launcher.comfy_ops.urllib.request.urlopen", fake_urlopen)
    target = ops.download_output("clip.mp4", tmp_path, subfolder="video results")
    assert target == tmp_path / "video results" / "clip.mp4"


def test_read_workflow_parses_json(monkeypatch) -> None:
    ops = _ops()
    payload = json.dumps({"nodes": [], "id": "x"})
    monkeypatch.setattr(
        ComfyOps, "_exec",
        lambda self, command, timeout: payload,
    )
    assert ops.read_workflow("video_minimax_h3_t2v.json") == {"nodes": [], "id": "x"}


def test_read_workflow_rejects_bad_name(monkeypatch) -> None:
    ops = _ops()
    with pytest.raises(ComfyOpsError):
        ops.read_workflow("../../etc/passwd")


def test_read_workflow_invalid_json(monkeypatch) -> None:
    ops = _ops()
    monkeypatch.setattr(ComfyOps, "_exec", lambda self, c, timeout: "not json {")
    with pytest.raises(ComfyOpsError, match="not valid JSON"):
        ops.read_workflow("x.json")


def test_read_workflow_non_object(monkeypatch) -> None:
    ops = _ops()
    monkeypatch.setattr(ComfyOps, "_exec", lambda self, c, timeout: "[1,2]")
    with pytest.raises(ComfyOpsError, match="not a workflow object"):
        ops.read_workflow("x.json")


def _fake_http_json(monkeypatch, responder):
    monkeypatch.setattr(
        ComfyOps, "_http_json",
        lambda self, method, path, body=None, timeout=60.0: responder(method, path, body),
    )


def test_get_object_info(monkeypatch) -> None:
    ops = _ops()
    _fake_http_json(monkeypatch, lambda m, p, b: {"SaveVideo": {}})
    assert ops.get_object_info() == {"SaveVideo": {}}


def test_get_queue_counts(monkeypatch) -> None:
    ops = _ops()
    _fake_http_json(
        monkeypatch,
        lambda m, p, b: {
            "queue_running": [[1, 2]],
            "queue_pending": [[3], [4], [5]],
        },
    )
    assert ops.get_queue() == {"running": 1, "pending": 3}


def test_get_history_reads_the_api_route(monkeypatch) -> None:
    ops = _ops()
    seen: list[str] = []

    def responder(m, p, b):
        seen.append(p)
        return {"pid-1": {"status": {"completed": True}}}

    _fake_http_json(monkeypatch, responder)
    assert ops.get_history(max_items=5) == {"pid-1": {"status": {"completed": True}}}
    assert seen == ["/api/history?max_items=5"]


def test_get_history_falls_back_to_the_legacy_route(monkeypatch) -> None:
    ops = _ops()
    seen: list[str] = []

    def responder(m, p, b):
        seen.append(p)
        if p.startswith("/api/history"):
            raise ComfyOpsError("HTTP 404")
        return {"pid-2": {}}

    _fake_http_json(monkeypatch, responder)
    assert ops.get_history() == {"pid-2": {}}
    assert seen == ["/api/history?max_items=200", "/history?max_items=200"]


def test_get_history_raises_when_both_routes_fail(monkeypatch) -> None:
    ops = _ops()

    def responder(m, p, b):
        raise ComfyOpsError("tunnel down")

    _fake_http_json(monkeypatch, responder)
    with pytest.raises(ComfyOpsError):
        ops.get_history()


def test_get_queue_degrades_to_empty_on_error(monkeypatch) -> None:
    ops = _ops()

    def responder(m, p, b):
        raise ComfyOpsError("tunnel down")

    _fake_http_json(monkeypatch, responder)
    assert ops.get_queue() == {}


def test_get_system_stats_degrades_to_empty_on_error(monkeypatch) -> None:
    ops = _ops()

    def responder(m, p, b):
        raise ComfyOpsError("tunnel down")

    _fake_http_json(monkeypatch, responder)
    assert ops.get_system_stats() == {}


def test_submit_prompt_rejection_surfaces_node_errors(monkeypatch) -> None:
    ops = _ops()

    def responder(m, p, b):
        raise ComfyOpsError(
            "ComfyUI API POST /prompt -> HTTP 400: "
            '{"error":{"type":"PromptError","message":"invalid"}}'
        )

    _fake_http_json(monkeypatch, responder)
    with pytest.raises(ComfyOpsError, match="HTTP 400"):
        ops.submit_prompt({})


def test_submit_prompt_missing_id_is_an_error(monkeypatch) -> None:
    ops = _ops()
    _fake_http_json(monkeypatch, lambda m, p, b: {"node_errors": {"1": "missing class"}})
    with pytest.raises(ComfyOpsError, match="rejected the prompt"):
        ops.submit_prompt({})


def test_http_requires_base_url() -> None:
    ops = _ops(base_url="")
    with pytest.raises(ComfyOpsError, match="tunnel must be up"):
        ops.get_object_info()


# --- build_comfy_ops (shared CLI/GUI binder) --------------------------------


class _FakeSecrets:
    runpod_api_key = "key-123"


class _FakeRunpodCfg:
    pod_id = ""


class _FakeSsh:
    key_path = r"C:\keys\id_ed25519"
    connect_timeout = 11


class _FakeConfig:
    def __init__(self, pod_id: str = "") -> None:
        self.secrets = _FakeSecrets()
        self.runpod = _FakeRunpodCfg()
        self.runpod.pod_id = pod_id
        self.ssh = _FakeSsh()

    def comfy_base_url(self) -> str:
        return "http://127.0.0.1:8188"


class _FakePod:
    status = "RUNNING"

    def __init__(self, endpoint) -> None:
        self._endpoint = endpoint

    def ssh_tunnel_endpoint(self):
        return self._endpoint


class _FakeRegistry:
    def __init__(self, record) -> None:
        self._record = record
        self.seen = []

    def load(self, stack):
        self.seen.append(stack)
        return self._record


def test_build_comfy_ops_binds_registered_pod(monkeypatch) -> None:
    from launcher import runpod as runpod_module

    endpoint = SshEndpoint(host="9.9.9.9", port=22, username="root")
    pod = _FakePod(endpoint)
    calls = {}

    class _FakeClient:
        def __init__(self, api_key) -> None:
            calls["api_key"] = api_key

        def get_pod(self, pod_id):
            calls["pod_id"] = pod_id
            return pod

    monkeypatch.setattr(runpod_module, "RunPodClient", _FakeClient)
    from types import SimpleNamespace as _NS

    registry = _FakeRegistry(_NS(pod_id="registered-pod"))
    ops = comfy_ops.build_comfy_ops(_FakeConfig(), registry=registry)
    assert registry.seen == ["comfy"]
    assert calls == {"api_key": "key-123", "pod_id": "registered-pod"}
    assert ops.endpoint is endpoint
    assert ops.key_path == r"C:\keys\id_ed25519"
    assert ops.connect_timeout == 11
    assert ops.base_url == "http://127.0.0.1:8188"


def test_build_comfy_ops_falls_back_to_env_pod_id(monkeypatch) -> None:
    from launcher import runpod as runpod_module

    pod = _FakePod(SshEndpoint(host="1.1.1.1", port=22, username="root"))
    seen = {}

    class _FakeClient:
        def __init__(self, api_key) -> None:
            seen["api_key"] = api_key

        def get_pod(self, pod_id):
            seen["pod_id"] = pod_id
            return pod

    monkeypatch.setattr(runpod_module, "RunPodClient", _FakeClient)
    ops = comfy_ops.build_comfy_ops(_FakeConfig(pod_id="env-pod"), registry=_FakeRegistry(None))
    assert seen["pod_id"] == "env-pod"
    assert ops.endpoint.host == "1.1.1.1"


def test_build_comfy_ops_requires_api_key() -> None:
    config = _FakeConfig()
    config.secrets.runpod_api_key = ""
    with pytest.raises(ComfyOpsError, match="RUNPOD_API_KEY"):
        comfy_ops.build_comfy_ops(config, registry=_FakeRegistry(None))


def test_build_comfy_ops_requires_registered_pod() -> None:
    with pytest.raises(ComfyOpsError, match="No comfy pod is registered"):
        comfy_ops.build_comfy_ops(_FakeConfig(), registry=_FakeRegistry(None))


def test_build_comfy_ops_requires_ssh_endpoint(monkeypatch) -> None:
    from launcher import runpod as runpod_module

    pod = _FakePod(None)

    class _FakeClient:
        def __init__(self, api_key) -> None:
            pass

        def get_pod(self, pod_id):
            return pod

    monkeypatch.setattr(runpod_module, "RunPodClient", _FakeClient)
    with pytest.raises(ComfyOpsError, match="no usable SSH endpoint"):
        comfy_ops.build_comfy_ops(_FakeConfig(pod_id="p1"), registry=_FakeRegistry(None))


# --- annuaire immediate install (install_node / install_workflow) ----------


def test_install_node_clones_repo(monkeypatch) -> None:
    ops = _ops()
    commands = []

    def fake_exec(self, command, timeout):
        commands.append(command)
        return "node installed"

    monkeypatch.setattr(ComfyOps, "_exec", fake_exec)
    ops.install_node("https://github.com/u/my-node.git")
    assert "git clone" in commands[0]
    assert "my-node" in commands[0]
    assert "custom_nodes" in commands[0]


def test_install_node_rejects_bad_url() -> None:
    ops = _ops()
    with pytest.raises(ComfyOpsError):
        ops.install_node("")
    with pytest.raises(ComfyOpsError):
        ops.install_node("ftp://nope")


def test_install_workflow_derives_filename(monkeypatch) -> None:
    ops = _ops()
    commands = []

    def fake_exec(self, command, timeout):
        commands.append(command)
        return "workflow installed"

    monkeypatch.setattr(ComfyOps, "_exec", fake_exec)
    ops.install_workflow("https://example.com/wf.json")
    assert "wf.json" in commands[0]
    assert "user/default/workflows/personal" in commands[0]


def test_install_workflow_forces_json_extension(monkeypatch) -> None:
    ops = _ops()
    commands = []

    def fake_exec(self, command, timeout):
        commands.append(command)
        return "workflow installed"

    monkeypatch.setattr(ComfyOps, "_exec", fake_exec)
    ops.install_workflow("https://example.com/wf", filename="Mon Wf")
    assert "Mon Wf.json" in commands[0]


def test_install_workflow_rejects_bad_url() -> None:
    ops = _ops()
    with pytest.raises(ComfyOpsError):
        ops.install_workflow("")
    with pytest.raises(ComfyOpsError):
        ops.install_workflow("ftp://nope")


def test_install_workflow_quotes_a_name_with_spaces(monkeypatch) -> None:
    """``shlex.quote`` *inside* double quotes produced a literal ``'`` in the name.

    ``-o "$wf_dir/'Mon Wf.json'"`` created a file literally called
    ``'Mon Wf.json'`` on the pod, while the log announced ``Mon Wf.json``.
    The whole path must be quoted as one shell word.
    """
    ops = _ops()
    commands = []
    monkeypatch.setattr(
        ComfyOps, "_exec", lambda self, command, timeout: commands.append(command) or ""
    )
    ops.install_workflow("https://example.com/wf", filename="Mon Wf")
    command = commands[0]
    assert "-o '/opt/ComfyUI/user/default/workflows/personal/Mon Wf.json'" in command
    assert "'Mon Wf.json'" not in command


def test_install_workflow_sanitizes_or_rejects_a_traversal_name(monkeypatch) -> None:
    """A traversal must never reach the pod's filesystem.

    ``Path(name).name`` reduces ``../../evil.json`` to ``evil.json``; anything
    it cannot reduce (a Windows separator on POSIX, shell metacharacters) is
    rejected outright.
    """
    ops = _ops()
    commands = []
    monkeypatch.setattr(
        ComfyOps, "_exec", lambda self, command, timeout: commands.append(command) or ""
    )
    ops.install_workflow("https://example.com/wf", filename="../../evil.json")
    assert "personal/evil.json" in commands[0]
    assert ".." not in commands[0]

    for bad in ("..\\evil.json", "evil;rm -rf ~.json", "a/b/c.json"):
        try:
            ops.install_workflow("https://example.com/wf", filename=bad)
        except ComfyOpsError:
            continue
        # If it was accepted, it must have been reduced to a bare leaf name
        # under the personal workflows folder.
        quoted = commands[-1].split("-o ", 1)[1].split(" ", 1)[0].strip("'")
        leaf = quoted.rsplit("/", 1)[-1]
        assert leaf not in ("", ".", "..")
        assert "\\" not in leaf and ";" not in leaf


def test_install_node_rejects_a_command_substitution_name(monkeypatch) -> None:
    """The node name is derived from a URL and interpolated into a shell string."""
    ops = _ops()
    monkeypatch.setattr(ComfyOps, "_exec", lambda self, command, timeout: "")
    with pytest.raises(ComfyOpsError):
        ops.install_node("https://github.com/u/$(touch pwned)")


def test_install_lora_rejects_a_traversal_filename(monkeypatch) -> None:
    """``--filename`` becomes ``dest_file="${dir}/${filename}"`` on the pod."""
    ops = _ops()
    monkeypatch.setattr(
        ComfyOps, "run_script", lambda self, *args, **kwargs: " ".join(args)
    )
    with pytest.raises(ComfyOpsError):
        ops.install_lora(
            "https://example.com/x.safetensors", filename="../../evil.safetensors"
        )


def test_install_lora_rejects_a_non_http_url(monkeypatch) -> None:
    """``url`` is the script's last positional: ``--list`` would be an option.

    The script then printed its listing, exited 0, and the caller reported a
    successful install that never happened.
    """
    ops = _ops()
    monkeypatch.setattr(
        ComfyOps, "run_script", lambda self, *args, **kwargs: " ".join(args)
    )
    with pytest.raises(ComfyOpsError):
        ops.install_lora("--list")


def test_remove_lora_rejects_a_traversal_name(monkeypatch) -> None:
    ops = _ops()
    monkeypatch.setattr(
        ComfyOps, "run_script", lambda self, *args, **kwargs: " ".join(args)
    )
    with pytest.raises(ComfyOpsError):
        ops.remove_lora("../../evil.safetensors")


# --- API keys on the pod-side scripts (the CivitAI 401/403 bug) -------------


def _ops_with_secrets(**extra) -> ComfyOps:
    return ComfyOps(
        endpoint=SshEndpoint(host="1.2.3.4", port=22, username="root"),
        key_path=None,
        secret_env=extra,
    )


def test_install_lora_forwards_the_civitai_key_to_the_script(monkeypatch) -> None:
    # Docker ENV is not inherited by sshd sessions, so the key must travel on
    # the remote command itself — otherwise install_lora.sh downloads
    # anonymously and CivitAI answers 401/403.
    ops = _ops_with_secrets(CIVITAI_API_KEY="civ-123", HF_TOKEN="hf-456")
    commands: list = []

    def fake_exec(self, command, timeout):
        commands.append(command)
        return "installed"

    monkeypatch.setattr(ComfyOps, "_exec", fake_exec)
    ops.install_lora("https://civitai.com/api/download/models/1")
    prefix = commands[0].split("bash ", 1)[0]
    assert "CIVITAI_API_KEY=civ-123" in prefix
    assert "HF_TOKEN=hf-456" in prefix
    assert "/install_lora.sh" in commands[0]
    assert "--personal" not in commands[0]


def test_forwarded_secret_values_are_shell_quoted(monkeypatch) -> None:
    ops = _ops_with_secrets(CIVITAI_API_KEY="a b;rm -rf /")
    commands: list = []

    def fake_exec(self, command, timeout):
        commands.append(command)
        return "installed"

    monkeypatch.setattr(ComfyOps, "_exec", fake_exec)
    ops.install_lora("https://civitai.com/api/download/models/1")
    assert "CIVITAI_API_KEY='a b;rm -rf /'" in commands[0]


def test_blank_secrets_are_not_exported(monkeypatch) -> None:
    ops = _ops_with_secrets(CIVITAI_API_KEY="", HF_TOKEN="   ")
    commands: list = []

    def fake_exec(self, command, timeout):
        commands.append(command)
        return "installed"

    monkeypatch.setattr(ComfyOps, "_exec", fake_exec)
    ops.install_lora("https://civitai.com/api/download/models/1")
    assert commands[0].startswith("bash ")
    assert "CIVITAI_API_KEY" not in commands[0]
    assert "HF_TOKEN" not in commands[0]


def test_unknown_secret_keys_are_never_forwarded() -> None:
    ops = _ops_with_secrets(RUNPOD_API_KEY="rp-secret", CIVITAI_API_KEY="civ")
    prefix = ops._secret_prefix()
    assert "CIVITAI_API_KEY=civ" in prefix
    assert "rp-secret" not in prefix


def test_run_script_without_secrets_is_unchanged(monkeypatch) -> None:
    ops = _ops()
    commands: list = []

    def fake_exec(self, command, timeout):
        commands.append(command)
        return "ok"

    monkeypatch.setattr(ComfyOps, "_exec", fake_exec)
    ops.sync_vault()
    assert commands[0] == f"bash {comfy_ops.DEFAULT_SCRIPTS_DIR}/sync_push.sh"


def test_build_comfy_ops_forwards_the_stored_keys(monkeypatch) -> None:
    from launcher import runpod as runpod_module

    endpoint = SshEndpoint(host="9.9.9.9", port=22, username="root")
    pod = _FakePod(endpoint)

    class _FakeClient:
        def __init__(self, api_key) -> None:
            pass

        def get_pod(self, pod_id):
            return pod

    monkeypatch.setattr(runpod_module, "RunPodClient", _FakeClient)
    from types import SimpleNamespace as _NS

    config = _FakeConfig()
    config.secrets.extra = {"HF_TOKEN": "hf-1", "CIVITAI_API_KEY": "civ-1"}
    ops = comfy_ops.build_comfy_ops(
        config, registry=_FakeRegistry(_NS(pod_id="pod-1"))
    )
    assert ops.secret_env == {"HF_TOKEN": "hf-1", "CIVITAI_API_KEY": "civ-1"}


def test_missing_civitai_key_is_reported_before_the_download(
    monkeypatch, caplog
) -> None:
    import logging

    ops = _ops()
    monkeypatch.setattr(ComfyOps, "_exec", lambda self, c, t: "installed")
    with caplog.at_level(logging.WARNING):
        ops.install_lora("https://civitai.com/api/download/models/1")
    assert "No CivitAI API key" in caplog.text


def test_hugging_face_urls_do_not_warn_about_civitai(monkeypatch, caplog) -> None:
    import logging

    ops = _ops()
    monkeypatch.setattr(ComfyOps, "_exec", lambda self, c, t: "installed")
    with caplog.at_level(logging.WARNING):
        ops.install_lora("https://huggingface.co/u/r/resolve/main/x.safetensors")
    assert "CivitAI" not in caplog.text


def test_is_civitai_url() -> None:
    assert comfy_ops.is_civitai_url("https://civitai.com/api/download/models/1")
    assert comfy_ops.is_civitai_url("https://civitai.red/api/download/models/1")
    assert not comfy_ops.is_civitai_url("https://huggingface.co/u/r/x.safetensors")
    assert not comfy_ops.is_civitai_url("")


# --- local assets: uploaded by the launcher, not downloaded by the pod ------


def test_asset_destination_per_kind() -> None:
    ops = _ops()
    assert ops.asset_destination("lora") == "/opt/ComfyUI/models/loras/personal"
    assert (
        ops.asset_destination("workflow")
        == "/opt/ComfyUI/user/default/workflows/personal"
    )
    assert ops.asset_destination("node") == "/opt/ComfyUI/custom_nodes"
    with pytest.raises(ComfyOpsError):
        ops.asset_destination("bogus")


def test_upload_asset_streams_a_local_file(tmp_path, monkeypatch) -> None:
    local = tmp_path / "y.safetensors"
    local.write_bytes(b"x" * 2048)
    ops = _ops()
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen["args"] = list(args)
        seen["payload"] = kwargs["stdin"].read()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(comfy_ops.subprocess, "run", fake_run)
    sizes = iter([None, 2048])  # absent before, correct after
    monkeypatch.setattr(ComfyOps, "remote_file_size", lambda self, path: next(sizes))

    out = ops.upload_asset("lora", local, name="Mon LoRA")
    assert "Mon LoRA" in out and "2 Ko" in out
    assert " s)" in out  # the duration is reported
    assert seen["payload"] == b"x" * 2048
    command = seen["args"][-1]
    assert "/opt/ComfyUI/models/loras/personal/y.safetensors" in command
    assert "cat > " in command


def test_upload_asset_skips_a_file_already_present(tmp_path, monkeypatch) -> None:
    """A 2 GB LoRA must not be re-sent on every start."""
    local = tmp_path / "y.safetensors"
    local.write_bytes(b"x" * 2048)
    ops = _ops()
    monkeypatch.setattr(ComfyOps, "remote_file_size", lambda self, path: 2048)

    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("nothing should be transferred")

    monkeypatch.setattr(comfy_ops.subprocess, "run", boom)
    out = ops.upload_asset("lora", local)
    assert "already on the pod" in out


def test_upload_asset_force_resends(tmp_path, monkeypatch) -> None:
    local = tmp_path / "y.safetensors"
    local.write_bytes(b"x" * 10)
    ops = _ops()
    monkeypatch.setattr(ComfyOps, "remote_file_size", lambda self, path: 10)
    monkeypatch.setattr(
        comfy_ops.subprocess, "run",
        lambda args, **k: subprocess.CompletedProcess(args, 0, "", ""),
    )
    assert "uploaded" in ops.upload_asset("lora", local, force=True)


def test_upload_asset_rejects_a_missing_local_path(tmp_path) -> None:
    ops = _ops()
    with pytest.raises(ComfyOpsError) as exc:
        ops.upload_asset("lora", tmp_path / "absent.safetensors")
    assert "introuvable" in str(exc.value)


def test_upload_asset_detects_a_truncated_transfer(tmp_path, monkeypatch) -> None:
    local = tmp_path / "y.safetensors"
    local.write_bytes(b"x" * 2048)
    ops = _ops()
    monkeypatch.setattr(
        comfy_ops.subprocess, "run",
        lambda args, **k: subprocess.CompletedProcess(args, 0, "", ""),
    )
    sizes = iter([None, 10])
    monkeypatch.setattr(ComfyOps, "remote_file_size", lambda self, path: next(sizes))
    with pytest.raises(ComfyOpsError) as exc:
        ops.upload_asset("lora", local)
    assert "incomplet" in str(exc.value)


def test_upload_asset_reports_a_failed_transfer(tmp_path, monkeypatch) -> None:
    local = tmp_path / "y.safetensors"
    local.write_bytes(b"x" * 10)
    ops = _ops()
    monkeypatch.setattr(ComfyOps, "remote_file_size", lambda self, path: None)
    monkeypatch.setattr(
        comfy_ops.subprocess, "run",
        lambda args, **k: subprocess.CompletedProcess(args, 1, "", "disk full"),
    )
    with pytest.raises(ComfyOpsError) as exc:
        ops.upload_asset("lora", local)
    assert "disk full" in str(exc.value)


def test_upload_asset_rejects_a_file_where_a_folder_is_expected(
    tmp_path, monkeypatch
) -> None:
    local = tmp_path / "not-a-folder.safetensors"
    local.write_bytes(b"x")
    with pytest.raises(ComfyOpsError) as exc:
        _ops().upload_asset("node", local)
    assert "must be a folder" in str(exc.value)


class _FakePopen:
    """Stands in for the tar and ssh processes of a folder upload."""

    calls: list = []

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.returncode = 0
        self.stdout = io.BytesIO(b"tar-stream")
        self.stderr = io.BytesIO(b"")
        _FakePopen.calls.append((list(argv), kwargs))

    def communicate(self, timeout=None):
        return ("", "")

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.returncode = -9


def _node_ops(monkeypatch, *, remote_signature=None, pip_output="deps ok"):
    """ComfyOps whose exec channel fakes the remote probes."""
    ops = _ops()
    exec_calls: list = []

    def fake_exec(self, command, timeout):
        exec_calls.append(command)
        if "du -sb" in command:
            return "" if remote_signature is None else f"{remote_signature[0]} {remote_signature[1]}"
        if "requirements.txt" in command:
            return pip_output
        return ""

    monkeypatch.setattr(ComfyOps, "_exec", fake_exec)
    monkeypatch.setattr(comfy_ops.subprocess, "Popen", _FakePopen)
    return ops, exec_calls


def test_upload_asset_sends_a_node_folder_and_installs_its_deps(
    tmp_path, monkeypatch
) -> None:
    local = tmp_path / "my_node"
    local.mkdir()
    (local / "__init__.py").write_text("x", encoding="utf-8")
    (local / "requirements.txt").write_text("numpy", encoding="utf-8")
    ops, exec_calls = _node_ops(monkeypatch)

    out = ops.upload_asset("node", local)
    assert "my_node/" in out
    assert "dependencies installed" in out
    # One tar process piped into one ssh process — not one connection per file.
    assert _FakePopen.calls[0][0][0] == "tar"
    assert _FakePopen.calls[1][0][0] == "ssh"
    assert "custom_nodes/my_node" in _FakePopen.calls[1][0][-1]
    assert any("venv/bin/pip" in c for c in exec_calls)


def test_upload_asset_skips_a_node_already_present(tmp_path, monkeypatch) -> None:
    local = tmp_path / "my_node"
    local.mkdir()
    (local / "a.py").write_text("x" * 10, encoding="utf-8")
    signature = (1, 10)  # matches _local_tree_signature for this folder
    ops, _ = _node_ops(monkeypatch, remote_signature=signature)
    _FakePopen.calls.clear()

    out = ops.upload_asset("node", local)
    assert "already on the pod" in out
    assert _FakePopen.calls == []


def test_upload_asset_survives_a_failed_pip(tmp_path, monkeypatch) -> None:
    """The node is already on the pod: a pip failure must not raise."""
    local = tmp_path / "my_node"
    local.mkdir()
    (local / "requirements.txt").write_text("numpy", encoding="utf-8")
    ops, _ = _node_ops(monkeypatch, pip_output="deps KO")

    out = ops.upload_asset("node", local)
    assert "dependencies not installed" in out


def test_human_size_is_compact() -> None:
    assert comfy_ops._human_size(512) == "512 o"
    assert comfy_ops._human_size(2048) == "2 Ko"
    assert comfy_ops._human_size(2 * 1024**3) == "2 Go"


def test_human_duration_is_compact() -> None:
    assert comfy_ops._human_duration(12.4) == "12 s"
    assert comfy_ops._human_duration(200) == "3 min 20 s"
    assert comfy_ops._human_duration(3900) == "1 h 05"
