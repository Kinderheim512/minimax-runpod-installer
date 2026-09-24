"""Tests for the secure RunPod credential store (launcher.credentials).

Covers the DPAPI-backed store core with an injected fake cipher (offline and
platform-independent), the ``credentials`` CLI subcommands, config precedence
is covered in ``tests/test_config_loading.py``. On Windows only, a round trip
against the real DPAPI implementation runs using generated fake credentials.

No real RunPod API key, pod, or infrastructure is ever used.
"""

import json
import os
import uuid
from pathlib import Path

import pytest

from launcher import credentials
from launcher.credentials import (
    CREDENTIALS_VERSION,
    CredentialStore,
    CredentialStoreCorrupt,
    CredentialStoreError,
    CredentialStoreMalformed,
    CredentialStoreMissing,
    CredentialStoreUnsupported,
    CredentialStoreVersionError,
    RunPodCredentials,
    default_credentials_path,
)

FAKE_KEY = "rp_fake_test_key_0123456789abcdef"
FAKE_TEMPLATE = "tmpl_fake_test_9876543210fedcba"
FAKE_KEY_2 = "rp_fake_test_key_9999999999zzzz"
FAKE_TEMPLATE_2 = "tmpl_fake_test_0000000000yyyy"
FAKE_HF = "hf_fake_test_token_0123456789abcdef"
FAKE_CIVITAI = "civ_fake_test_key_0123456789abcdef"


def _fake_cipher():
    """A reversible fake cipher: ciphertext is obviously not plaintext."""

    def protect(data: bytes) -> bytes:
        return b"CT:" + data[::-1]

    def unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"CT:"):
            raise ValueError("bad ciphertext")
        return blob[3:][::-1]

    return protect, unprotect


def _store(tmp_path: Path, **kwargs) -> CredentialStore:
    protect, unprotect = _fake_cipher()
    kwargs.setdefault("protect", protect)
    kwargs.setdefault("unprotect", unprotect)
    kwargs.setdefault("apply_user_only_acl", False)
    return CredentialStore(
        path=tmp_path / "OpenFoxForge" / "credentials.dpapi", **kwargs
    )


def _write_raw_ciphertext(store: CredentialStore, plaintext: bytes) -> None:
    protect, _ = _fake_cipher()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(protect(plaintext))


# ---------------------------------------------------------------------------
# set / get
# ---------------------------------------------------------------------------


def test_set_then_get_roundtrip(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    assert store.path.is_file()

    loaded = store.get()
    assert isinstance(loaded, RunPodCredentials)
    assert loaded.runpod_api_key == FAKE_KEY
    assert loaded.runpod_template_id == FAKE_TEMPLATE

    on_disk = store.path.read_bytes()
    assert FAKE_KEY.encode("utf-8") not in on_disk
    assert FAKE_TEMPLATE.encode("utf-8") not in on_disk


def test_payload_shape_v2_with_empty_extras(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    _, unprotect = _fake_cipher()
    payload = json.loads(unprotect(store.path.read_bytes()).decode("utf-8"))
    assert payload == {
        "version": CREDENTIALS_VERSION,
        "runpod_api_key": FAKE_KEY,
        "runpod_template_id": FAKE_TEMPLATE,
        "extra_secrets": {},
    }
    assert "runpod_pod_id" not in payload
    assert "pod_id" not in payload


# ---------------------------------------------------------------------------
# extra secrets (v2: HF_TOKEN / CIVITAI_API_KEY / ...)
# ---------------------------------------------------------------------------


def test_set_extra_roundtrip(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(
        FAKE_KEY,
        FAKE_TEMPLATE,
        extra={"HF_TOKEN": FAKE_HF, "civitai_api_key": FAKE_CIVITAI},
    )
    loaded = store.get()
    assert loaded.extra == {
        "HF_TOKEN": FAKE_HF,
        "CIVITAI_API_KEY": FAKE_CIVITAI,
    }
    on_disk = store.path.read_bytes()
    assert FAKE_HF.encode("utf-8") not in on_disk
    assert FAKE_CIVITAI.encode("utf-8") not in on_disk


def test_set_without_extra_preserves_existing_extras(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE, extra={"HF_TOKEN": FAKE_HF})
    store.set(FAKE_KEY_2, FAKE_TEMPLATE_2)
    loaded = store.get()
    assert loaded.runpod_api_key == FAKE_KEY_2
    assert loaded.runpod_template_id == FAKE_TEMPLATE_2
    assert loaded.extra == {"HF_TOKEN": FAKE_HF}


def test_set_explicit_extra_fully_replaces(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE, extra={"HF_TOKEN": FAKE_HF})
    store.set(FAKE_KEY, FAKE_TEMPLATE, extra={"CIVITAI_API_KEY": FAKE_CIVITAI})
    assert store.get().extra == {"CIVITAI_API_KEY": FAKE_CIVITAI}


def test_blank_extra_value_drops_key(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(
        FAKE_KEY,
        FAKE_TEMPLATE,
        extra={"HF_TOKEN": FAKE_HF, "CIVITAI_API_KEY": "   "},
    )
    assert store.get().extra == {"HF_TOKEN": FAKE_HF}


def test_v1_payload_loads_with_empty_extras(tmp_path) -> None:
    store = _store(tmp_path)
    _write_raw_ciphertext(
        store,
        json.dumps(
            {
                "version": 1,
                "runpod_api_key": FAKE_KEY,
                "runpod_template_id": FAKE_TEMPLATE,
            }
        ).encode("utf-8"),
    )
    loaded = store.get()
    assert loaded.runpod_api_key == FAKE_KEY
    assert loaded.runpod_template_id == FAKE_TEMPLATE
    assert loaded.extra == {}


def test_malformed_extra_secrets_raise(tmp_path) -> None:
    for bad in (
        ["HF_TOKEN"],
        {"HF_TOKEN": ""},
        {"": FAKE_HF},
        {"HF_TOKEN": 12345},
        "HF_TOKEN",
    ):
        store = _store(tmp_path)
        _write_raw_ciphertext(
            store,
            json.dumps(
                {
                    "version": CREDENTIALS_VERSION,
                    "runpod_api_key": FAKE_KEY,
                    "runpod_template_id": FAKE_TEMPLATE,
                    "extra_secrets": bad,
                }
            ).encode("utf-8"),
        )
        with pytest.raises(CredentialStoreMalformed):
            store.get()


def test_status_reports_extra_keys_without_values(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE, extra={"HF_TOKEN": FAKE_HF})
    st = store.status()
    assert st.extra_keys == ("HF_TOKEN",)
    rendered = repr(st) + json.dumps(st.__dict__)
    assert FAKE_HF not in rendered


def test_extra_secrets_never_in_errors_or_logs(tmp_path, caplog) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE, extra={"HF_TOKEN": FAKE_HF})
    store.path.write_bytes(b"garbage")
    try:
        store.get()
    except CredentialStoreError as exc:
        assert FAKE_HF not in str(exc)
    with caplog.at_level("DEBUG"):
        store.status()
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE_HF not in joined


def test_credentials_repr_redacts_extra(tmp_path) -> None:
    creds = RunPodCredentials(
        runpod_api_key=FAKE_KEY,
        runpod_template_id=FAKE_TEMPLATE,
        extra={"HF_TOKEN": FAKE_HF},
    )
    assert FAKE_HF not in repr(creds)
    assert FAKE_HF not in str(creds)


def test_set_overwrites_existing(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    store.set(FAKE_KEY_2, FAKE_TEMPLATE_2)
    loaded = store.get()
    assert loaded.runpod_api_key == FAKE_KEY_2
    assert loaded.runpod_template_id == FAKE_TEMPLATE_2


def test_set_twice_rotates_values(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    store.set(FAKE_KEY_2, FAKE_TEMPLATE)
    assert store.get().runpod_api_key == FAKE_KEY_2
    store.set(FAKE_KEY, FAKE_TEMPLATE_2)
    assert store.get().runpod_api_key == FAKE_KEY
    assert store.get().runpod_template_id == FAKE_TEMPLATE_2


def test_get_missing_file_raises_missing(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CredentialStoreMissing):
        store.get()


def test_empty_values_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CredentialStoreError):
        store.set("", FAKE_TEMPLATE)
    with pytest.raises(CredentialStoreError):
        store.set(FAKE_KEY, "")
    with pytest.raises(CredentialStoreError):
        store.set("   ", FAKE_TEMPLATE)
    with pytest.raises(CredentialStoreError):
        store.set(FAKE_KEY, "  ")
    assert not store.path.exists()


def test_non_string_values_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CredentialStoreError):
        store.set(None, FAKE_TEMPLATE)  # type: ignore[arg-type]
    with pytest.raises(CredentialStoreError):
        store.set(FAKE_KEY, 12345)  # type: ignore[arg-type]


def test_set_verifies_by_decrypting_written_file(tmp_path) -> None:
    written: list[bytes] = []
    protect, _ = _fake_cipher()

    def tracking_unprotect(blob: bytes) -> bytes:
        written.append(blob)
        return _fake_cipher()[1](blob)

    store = CredentialStore(
        path=tmp_path / "c.dpapi",
        protect=protect,
        unprotect=tracking_unprotect,
        apply_user_only_acl=False,
    )
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    assert written, "set() must decrypt the file after writing it"
    assert store.path.read_bytes() in written


def test_set_verification_mismatch_raises_safe_error(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(
        store,
        "_read_payload",
        lambda: {
            "version": CREDENTIALS_VERSION,
            "runpod_api_key": "something_else",
            "runpod_template_id": "something_else",
        },
    )
    with pytest.raises(CredentialStoreError, match="verification"):
        store.set(FAKE_KEY, FAKE_TEMPLATE)


# ---------------------------------------------------------------------------
# load failures — classified, safe errors
# ---------------------------------------------------------------------------


def test_corrupted_ciphertext_raises_corrupt(tmp_path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b"\x00\xff garbage that is not a cipher blob")
    with pytest.raises(CredentialStoreCorrupt):
        store.get()


def test_unprotect_exception_maps_to_corrupt(tmp_path) -> None:
    protect, _ = _fake_cipher()

    def broken_unprotect(blob: bytes) -> bytes:
        raise RuntimeError("dpapi blew up")

    store = CredentialStore(
        path=tmp_path / "c.dpapi",
        protect=protect,
        unprotect=broken_unprotect,
        apply_user_only_acl=False,
    )
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(protect(b"{}"))
    with pytest.raises(CredentialStoreCorrupt):
        store.get()


def test_unreadable_file_maps_to_corrupt(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)

    def boom(self) -> bytes:
        raise OSError("access denied")

    monkeypatch.setattr(Path, "read_bytes", boom)
    with pytest.raises(CredentialStoreCorrupt):
        store.get()


def test_malformed_decrypted_payload_raises(tmp_path) -> None:
    store = _store(tmp_path)
    _write_raw_ciphertext(store, b"this is not json at all")
    with pytest.raises(CredentialStoreMalformed):
        store.get()


def test_wrong_payload_version_raises(tmp_path) -> None:
    store = _store(tmp_path)
    _write_raw_ciphertext(
        store,
        json.dumps(
            {
                "version": CREDENTIALS_VERSION + 1,
                "runpod_api_key": FAKE_KEY,
                "runpod_template_id": FAKE_TEMPLATE,
            }
        ).encode("utf-8"),
    )
    with pytest.raises(CredentialStoreVersionError):
        store.get()


def test_missing_api_key_field_raises(tmp_path) -> None:
    store = _store(tmp_path)
    _write_raw_ciphertext(
        store,
        json.dumps(
            {"version": CREDENTIALS_VERSION, "runpod_template_id": FAKE_TEMPLATE}
        ).encode("utf-8"),
    )
    with pytest.raises(CredentialStoreMalformed):
        store.get()


def test_missing_template_field_raises(tmp_path) -> None:
    store = _store(tmp_path)
    _write_raw_ciphertext(
        store,
        json.dumps(
            {"version": CREDENTIALS_VERSION, "runpod_api_key": FAKE_KEY}
        ).encode("utf-8"),
    )
    with pytest.raises(CredentialStoreMalformed):
        store.get()


def test_non_dict_payload_raises(tmp_path) -> None:
    store = _store(tmp_path)
    _write_raw_ciphertext(store, json.dumps([1, 2, 3]).encode("utf-8"))
    with pytest.raises(CredentialStoreMalformed):
        store.get()


def test_exceptions_never_expose_secrets(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    store.path.write_bytes(b"garbage")

    for fn in (store.get, lambda: store.status()):
        try:
            fn()
        except CredentialStoreError as exc:
            assert FAKE_KEY not in str(exc)
            assert FAKE_TEMPLATE not in str(exc)

    with pytest.raises(CredentialStoreMissing) as excinfo:
        store.clear()
        store.get()
    assert FAKE_KEY not in str(excinfo.value)


def test_set_encrypt_failure_message_is_safe(tmp_path) -> None:
    store = _store(tmp_path)

    def boom(data: bytes) -> bytes:
        raise RuntimeError(f"boom {data!r}")

    failing = CredentialStore(
        path=store.path, protect=boom, unprotect=_fake_cipher()[1],
        apply_user_only_acl=False,
    )
    with pytest.raises(CredentialStoreError) as excinfo:
        failing.set(FAKE_KEY, FAKE_TEMPLATE)
    assert FAKE_KEY not in str(excinfo.value)
    assert "runpod_api_key" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# status / clear
# ---------------------------------------------------------------------------


def test_status_not_configured_when_missing(tmp_path) -> None:
    store = _store(tmp_path)
    st = store.status()
    assert st.file_present is False
    assert st.api_key_configured is False
    assert st.template_configured is False
    assert st.readable is False


def test_status_configured(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    st = store.status()
    assert st.file_present is True
    assert st.api_key_configured is True
    assert st.template_configured is True
    assert st.readable is True
    rendered = repr(st) + json.dumps(st.__dict__)
    assert FAKE_KEY not in rendered
    assert FAKE_TEMPLATE not in rendered


def test_status_corrupt(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    store.path.write_bytes(b"garbage")
    st = store.status()
    assert st.file_present is True
    assert st.readable is False
    assert st.api_key_configured is False


def test_clear_removes_credential_file(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    assert store.clear() is True
    assert not store.path.exists()
    with pytest.raises(CredentialStoreMissing):
        store.get()
    assert store.status().file_present is False


def test_clear_idempotent_when_absent(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.clear() is False
    assert store.status().file_present is False


# ---------------------------------------------------------------------------
# no secret leakage (logs / repr)
# ---------------------------------------------------------------------------


def test_set_and_get_do_not_log_secrets(tmp_path, caplog) -> None:
    store = _store(tmp_path)
    with caplog.at_level("DEBUG"):
        store.set(FAKE_KEY, FAKE_TEMPLATE)
        store.get()
        store.status()
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE_KEY not in joined
    assert FAKE_TEMPLATE not in joined


def test_credentials_repr_redacted(tmp_path) -> None:
    creds = RunPodCredentials(runpod_api_key=FAKE_KEY, runpod_template_id=FAKE_TEMPLATE)
    assert FAKE_KEY not in repr(creds)
    assert FAKE_TEMPLATE not in repr(creds)
    assert FAKE_KEY not in str(creds)


# ---------------------------------------------------------------------------
# atomicity
# ---------------------------------------------------------------------------


def test_set_failure_leaves_existing_file_untouched(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    before = store.path.read_bytes()

    def boom(data: bytes) -> bytes:
        raise RuntimeError("dpapi down")

    failing = CredentialStore(
        path=store.path, protect=boom, unprotect=_fake_cipher()[1],
        apply_user_only_acl=False,
    )
    with pytest.raises(CredentialStoreError):
        failing.set(FAKE_KEY_2, FAKE_TEMPLATE_2)
    assert store.path.read_bytes() == before
    leftovers = [p for p in store.path.parent.iterdir() if p.name != store.path.name]
    assert leftovers == []


def test_set_cleans_temp_file_when_replace_fails(tmp_path, monkeypatch) -> None:
    import launcher.credentials as cred_mod

    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    before = store.path.read_bytes()

    def bad_replace(src, dst) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cred_mod.os, "replace", bad_replace)
    with pytest.raises(CredentialStoreError):
        store.set(FAKE_KEY_2, FAKE_TEMPLATE_2)
    assert store.path.read_bytes() == before
    leftovers = [p for p in store.path.parent.iterdir() if p.name != store.path.name]
    assert leftovers == []


def test_no_plaintext_credential_file_is_created(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    files = list(store.path.parent.iterdir())
    assert [p.name for p in files] == [store.path.name]
    for path in files:
        data = path.read_bytes()
        assert FAKE_KEY.encode("utf-8") not in data
        assert FAKE_TEMPLATE.encode("utf-8") not in data


# ---------------------------------------------------------------------------
# path resolution / platform behavior
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="default store path is Windows-specific")
def test_default_path_requires_appdata(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("APPDATA", raising=False)
    # The suite-wide isolation fixture points the store at a temp directory;
    # this test is about the *fallback* resolution, so the override goes too.
    monkeypatch.delenv("MINIMAX_LAUNCHER_CREDENTIALS_DIR", raising=False)
    with pytest.raises(CredentialStoreUnsupported):
        default_credentials_path()


def _fake_unsupported_check() -> None:
    raise CredentialStoreUnsupported(
        "the secure credential store requires Windows DPAPI and is not "
        "available on this platform"
    )


def test_real_dpapi_refused_when_platform_check_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(credentials, "_require_windows", _fake_unsupported_check)
    store = CredentialStore(path=tmp_path / "c.dpapi")
    with pytest.raises(CredentialStoreUnsupported):
        store.set(FAKE_KEY, FAKE_TEMPLATE)
    assert not store.path.exists()


def test_core_works_with_injected_cipher_and_no_platform_use(tmp_path) -> None:
    store = _store(tmp_path)
    store.set(FAKE_KEY, FAKE_TEMPLATE)
    assert store.get().runpod_api_key == FAKE_KEY


@pytest.mark.skipif(os.name != "nt", reason="platform check is Windows-only")
def test_platform_check_passes_on_nt() -> None:
    credentials._require_windows()


# ---------------------------------------------------------------------------
# real DPAPI (Windows only, generated fake credentials)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="real DPAPI is Windows-only")
def test_real_dpapi_rejects_foreign_ciphertext(tmp_path) -> None:
    store = CredentialStore(apply_user_only_acl=False)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b"\xff" + os.urandom(63))
    with pytest.raises(CredentialStoreCorrupt):
        store.get()


@pytest.mark.skipif(os.name != "nt", reason="real DPAPI is Windows-only")
def test_real_dpapi_roundtrip_with_extras(tmp_path) -> None:
    hf = "hf_test_" + uuid.uuid4().hex
    civ = "civ_test_" + uuid.uuid4().hex
    store = CredentialStore(apply_user_only_acl=False)
    store.set(
        "rp_test_" + uuid.uuid4().hex,
        "tmpl_test_" + uuid.uuid4().hex,
        extra={"HF_TOKEN": hf, "CIVITAI_API_KEY": civ},
    )
    fresh = CredentialStore(apply_user_only_acl=False).get()
    assert fresh.extra == {"HF_TOKEN": hf, "CIVITAI_API_KEY": civ}
    assert store.clear() is True


@pytest.mark.skipif(os.name != "nt", reason="real DPAPI is Windows-only")
def test_real_dpapi_status_configured_and_clear(tmp_path) -> None:
    key = "rp_test_" + uuid.uuid4().hex
    store = CredentialStore(apply_user_only_acl=False)
    assert store.status().file_present is False
    store.set(key, "tmpl_test_" + uuid.uuid4().hex)
    st = store.status()
    assert st.file_present is True and st.readable is True
    store.clear()
    assert store.status().file_present is False


@pytest.mark.skipif(os.name != "nt", reason="icacls is Windows-only")
def test_apply_user_only_acl_invokes_icacls(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(credentials.subprocess, "run", fake_run)
    monkeypatch.setenv("USERNAME", "testuser")
    store = _store(tmp_path, apply_user_only_acl=True)
    store.set(FAKE_KEY, FAKE_TEMPLATE)

    icacls_calls = [c for c in calls if c and c[0] == "icacls"]
    assert icacls_calls, "expected icacls to be invoked for user-only protection"
    for cmd in icacls_calls:
        assert "/inheritance:r" in cmd
        assert "testuser:F" in cmd


# ---------------------------------------------------------------------------
# CLI (credentials set / status / clear)
# ---------------------------------------------------------------------------


class _FakeStoreFactory:
    def __init__(self, tmp_path: Path):
        self.store = _store(tmp_path)
        self.constructed = 0

    def __call__(self, *args, **kwargs):
        self.constructed += 1
        return self.store


def _patch_cli(tmp_path, monkeypatch, prompts, store_factory=None):
    from launcher import main as main_module

    factory = store_factory or _FakeStoreFactory(tmp_path)
    monkeypatch.setattr(main_module.credentials, "CredentialStore", factory)
    it = iter(prompts)
    monkeypatch.setattr(main_module.getpass, "getpass", lambda prompt: next(it))
    return main_module, factory


def test_credentials_set_stores_without_echoing(tmp_path, monkeypatch, capsys) -> None:
    main_module, factory = _patch_cli(
        tmp_path, monkeypatch, [FAKE_KEY, FAKE_TEMPLATE, "", "", "", "", "", ""]
    )
    rc = main_module.main(["credentials", "set"])
    assert rc == 0
    assert factory.constructed >= 1
    loaded = factory.store.get()
    assert loaded.runpod_api_key == FAKE_KEY
    assert loaded.runpod_template_id == FAKE_TEMPLATE
    assert loaded.extra == {}
    out = capsys.readouterr()
    assert FAKE_KEY not in out.out + out.err
    assert FAKE_TEMPLATE not in out.out + out.err


def test_credentials_set_empty_input_fails(tmp_path, monkeypatch, caplog) -> None:
    main_module, factory = _patch_cli(
        tmp_path, monkeypatch, ["", FAKE_TEMPLATE, "", "", "", "", "", ""]
    )
    with caplog.at_level("ERROR"):
        rc = main_module.main(["credentials", "set"])
    assert rc == 1
    assert not factory.store.path.exists()
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE_TEMPLATE not in joined


FAKE_COMFY_TEMPLATE = "tmpl_comfy_test_1234567890abcdef"
FAKE_LLAMACPP_TEMPLATE = "tmpl_llamacpp_test_0987654321fedcba"
FAKE_TRAIN_TEMPLATE = "tmpl_train_test_abcdef0123456789"


def test_credentials_set_stores_comfy_template(tmp_path, monkeypatch, capsys) -> None:
    main_module, factory = _patch_cli(
        tmp_path, monkeypatch,
        [FAKE_KEY, FAKE_TEMPLATE, FAKE_COMFY_TEMPLATE, "", "", "", "", ""],
    )
    rc = main_module.main(["credentials", "set"])
    assert rc == 0
    loaded = factory.store.get()
    assert loaded.comfy_template_id == FAKE_COMFY_TEMPLATE
    out = capsys.readouterr()
    assert FAKE_COMFY_TEMPLATE not in out.out + out.err


def test_credentials_set_blank_train_template_keeps_existing(
    tmp_path, monkeypatch
) -> None:
    factory = _FakeStoreFactory(tmp_path)
    factory.store.set(FAKE_KEY, FAKE_TEMPLATE, train_template_id=FAKE_TRAIN_TEMPLATE)
    main_module, _ = _patch_cli(
        tmp_path, monkeypatch,
        [FAKE_KEY_2, FAKE_TEMPLATE_2, "", "", "", "", "", ""],
        store_factory=factory,
    )
    assert main_module.main(["credentials", "set"]) == 0
    loaded = factory.store.get()
    assert loaded.train_template_id == FAKE_TRAIN_TEMPLATE


def test_credentials_status_reports_train_template(
    tmp_path, monkeypatch, capsys
) -> None:
    factory = _FakeStoreFactory(tmp_path)
    factory.store.set(FAKE_KEY, FAKE_TEMPLATE, train_template_id=FAKE_TRAIN_TEMPLATE)
    main_module, _ = _patch_cli(tmp_path, monkeypatch, [], store_factory=factory)
    rc = main_module.main(["credentials", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "train template" in out.lower()


FAKE_VNC_PASSWORD = "desktop-password-12"


def test_credentials_status_reports_the_desktop_password(
    tmp_path, monkeypatch, capsys
) -> None:
    factory = _FakeStoreFactory(tmp_path)
    factory.store.set(
        FAKE_KEY, FAKE_TEMPLATE, vnc_password=FAKE_VNC_PASSWORD
    )
    main_module, _ = _patch_cli(tmp_path, monkeypatch, [], store_factory=factory)
    assert main_module.main(["credentials", "status"]) == 0
    out = capsys.readouterr().out
    assert "desktop password" in out.lower()
    assert "SET" in out
    assert FAKE_VNC_PASSWORD not in out


def test_credentials_set_blank_comfy_template_keeps_existing(tmp_path, monkeypatch) -> None:
    factory = _FakeStoreFactory(tmp_path)
    factory.store.set(
        FAKE_KEY, FAKE_TEMPLATE, comfy_template_id=FAKE_COMFY_TEMPLATE
    )
    main_module, _ = _patch_cli(
        tmp_path, monkeypatch,
        [FAKE_KEY_2, FAKE_TEMPLATE_2, "", "", "", "", "", ""],
        store_factory=factory,
    )
    assert main_module.main(["credentials", "set"]) == 0
    loaded = factory.store.get()
    assert loaded.comfy_template_id == FAKE_COMFY_TEMPLATE


def test_credentials_status_reports_comfy_template(tmp_path, monkeypatch, capsys) -> None:
    factory = _FakeStoreFactory(tmp_path)
    factory.store.set(FAKE_KEY, FAKE_TEMPLATE, comfy_template_id=FAKE_COMFY_TEMPLATE)
    main_module, _ = _patch_cli(tmp_path, monkeypatch, [], store_factory=factory)
    rc = main_module.main(["credentials", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ComfyUI template" in out
    assert "SET" in out
    assert FAKE_COMFY_TEMPLATE not in out


def test_credentials_set_blank_extras_keep_existing(tmp_path, monkeypatch) -> None:
    factory = _FakeStoreFactory(tmp_path)
    factory.store.set(
        FAKE_KEY, FAKE_TEMPLATE, extra={"HF_TOKEN": FAKE_HF}
    )
    main_module, _ = _patch_cli(
        tmp_path, monkeypatch, [FAKE_KEY_2, FAKE_TEMPLATE_2, "", "", "", "", "", ""],
        store_factory=factory,
    )
    assert main_module.main(["credentials", "set"]) == 0
    loaded = factory.store.get()
    assert loaded.runpod_api_key == FAKE_KEY_2
    assert loaded.extra == {"HF_TOKEN": FAKE_HF}


def test_credentials_status_reports_configured(tmp_path, monkeypatch, capsys) -> None:
    factory = _FakeStoreFactory(tmp_path)
    factory.store.set(FAKE_KEY, FAKE_TEMPLATE)
    main_module, _ = _patch_cli(tmp_path, monkeypatch, [], store_factory=factory)
    rc = main_module.main(["credentials", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CONFIGURED" in out
    assert FAKE_KEY not in out
    assert FAKE_TEMPLATE not in out


def test_credentials_status_lists_extra_keys(tmp_path, monkeypatch, capsys) -> None:
    factory = _FakeStoreFactory(tmp_path)
    factory.store.set(FAKE_KEY, FAKE_TEMPLATE, extra={"HF_TOKEN": FAKE_HF})
    main_module, _ = _patch_cli(tmp_path, monkeypatch, [], store_factory=factory)
    rc = main_module.main(["credentials", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "HF_TOKEN" in out and "SET" in out
    assert "CIVITAI_API_KEY" in out and "ABSENT" in out
    assert FAKE_HF not in out


def test_credentials_status_reports_not_configured(tmp_path, monkeypatch, capsys) -> None:
    main_module, _ = _patch_cli(tmp_path, monkeypatch, [])
    rc = main_module.main(["credentials", "status"])
    assert rc == 0
    assert "NOT CONFIGURED" in capsys.readouterr().out


def test_credentials_status_reports_corrupt(tmp_path, monkeypatch, capsys) -> None:
    factory = _FakeStoreFactory(tmp_path)
    factory.store.set(FAKE_KEY, FAKE_TEMPLATE)
    factory.store.path.write_bytes(b"garbage")
    main_module, _ = _patch_cli(tmp_path, monkeypatch, [], store_factory=factory)
    rc = main_module.main(["credentials", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CORRUPT" in out
    assert FAKE_KEY not in out


def test_credentials_clear_removes_and_is_idempotent(tmp_path, monkeypatch) -> None:
    factory = _FakeStoreFactory(tmp_path)
    factory.store.set(FAKE_KEY, FAKE_TEMPLATE)
    main_module, _ = _patch_cli(tmp_path, monkeypatch, [], store_factory=factory)
    assert main_module.main(["credentials", "clear"]) == 0
    assert not factory.store.path.exists()
    assert main_module.main(["credentials", "clear"]) == 0


def test_credentials_bare_defaults_to_status(tmp_path, monkeypatch, capsys) -> None:
    main_module, _ = _patch_cli(tmp_path, monkeypatch, [])
    rc = main_module.main(["credentials"])
    assert rc == 0
    assert "NOT CONFIGURED" in capsys.readouterr().out


def test_start_config_path_uses_stored_credentials_when_env_absent(
    monkeypatch,
) -> None:
    """The config path used by `start` fills secrets from the store."""
    from launcher import main as main_module

    class _WiredStore:
        def __init__(self, *args, **kwargs):
            pass

        def get(self):
            return RunPodCredentials(
                runpod_api_key=FAKE_KEY, runpod_template_id=FAKE_TEMPLATE
            )

    monkeypatch.setattr(main_module.credentials, "CredentialStore", _WiredStore)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("RUNPOD_TEMPLATE_ID", raising=False)
    config, err = main_module._load_config()
    assert err is None
    assert config.secrets.runpod_api_key == FAKE_KEY
    assert config.secrets.runpod_template_id == FAKE_TEMPLATE


def test_credentials_status_unsupported_platform(tmp_path, monkeypatch, caplog) -> None:
    from launcher import main as main_module

    class _UnsupportedStore:
        def __init__(self, *args, **kwargs):
            pass

        def status(self):
            raise CredentialStoreUnsupported(
                "the secure credential store requires Windows DPAPI"
            )

    monkeypatch.setattr(main_module.credentials, "CredentialStore", _UnsupportedStore)
    with caplog.at_level("ERROR"):
        rc = main_module.main(["credentials", "status"])
    assert rc == 1
