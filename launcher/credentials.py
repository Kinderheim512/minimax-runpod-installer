"""Secure credential storage for the MiniMax H3 Launcher.

Stores a small structured secret bundle — the RunPod API key, the private
RunPod template ID, and optional extra API keys (``extra_secrets`` map,
e.g. ``HF_TOKEN`` / ``CIVITAI_API_KEY``) — encrypted at rest at a
deterministic per-user location:

    Windows  %APPDATA%\\MiniMaxH3Launcher\\credentials.dpapi
    macOS    ~/Library/Application Support/MiniMaxH3Launcher/credentials.dpapi
    Linux    $XDG_DATA_HOME/minimax-launcher/credentials.dpapi
             (or ~/.local/share/minimax-launcher/credentials.dpapi)

Three backends, picked automatically (see :func:`default_backend_name`):

* **Windows** — DPAPI (``CryptProtectData`` / ``CryptUnprotectData``) through
  ``ctypes``: the key is bound to the Windows user account.
* **macOS** — the login Keychain, through the ``security`` CLI
  (``add-generic-password`` / ``find-generic-password``). The file holds only
  a token; the secret itself never touches disk.
* **Linux** — the Secret Service, through ``secret-tool``
  (``libsecret``). Same token-file design.

When neither ``security`` nor ``secret-tool`` is available (a headless
container, a minimal distro), the fallback is a ``0600`` file in the
per-user data directory: it is plaintext on disk, and the launcher says so in
its log rather than refusing to run. There is deliberately no *insecure*
fallback beyond that: the file is user-only readable, and no custom
cryptography is invented.

Design rules:

* No external dependency beyond the OS's own credential store. No custom
  cryptography and no key material next to the data.
* The plaintext bundle never touches disk, logs, exceptions, status output,
  or audit records.
* Writes are atomic (temporary file + ``os.replace``) and verified by reading
  the file back and decrypting it.
* Load failures are classified errors with fixed, safe messages — they never
  expose ciphertext or plaintext.
* ``POD_ID`` is intentionally NOT part of the credential bundle; pod
  identification remains a transient environment value until dynamic
  provisioning replaces it.
* The optional ``extra_secrets`` map (v2 payloads) holds additional API keys
  (e.g. ``HF_TOKEN`` / ``CIVITAI_API_KEY``) with the same invariants as the
  RunPod pair. Version 1 payloads remain readable (empty map).
* v3 payloads add an optional ``comfy_template_id`` — the private RunPod
  template ID of the ComfyUI/MiniMax H3 image, used by the ``comfy`` stack.
  Older payloads load with it unset; nothing is force-migrated on read.
* v5 payloads add an optional ``train_template_id`` — the private RunPod
  template ID of the LoRA training image, used by the ``train`` stack — and
  an optional ``vnc_password``, the desktop/file-manager credential the
  ``train`` image serves. A v4 bundle loads with both unset, which is the
  correct state for anyone who has not set that stack up yet; nothing is
  force-migrated on read.
* The module imports cleanly on any platform;
  :class:`CredentialStoreUnsupported` is raised only when the DPAPI store is
  actually used on a non-Windows platform. There is deliberately no insecure
  fallback.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

from . import winproc

#: Schema version of the encrypted payload (bump for future migrations).
CREDENTIALS_VERSION = 5

#: Payload versions this module can read. Version 1 bundles carry only the
#: RunPod pair (no ``extra_secrets`` key); they load with an empty map.
#: Version 3 adds the optional ``comfy_template_id``, version 5 the optional
#: ``train_template_id``.
SUPPORTED_VERSIONS = (1, 2, 3, 4, 5)

CREDENTIALS_DIRNAME = "MiniMaxH3Launcher"
CREDENTIALS_DIRNAME_LINUX = "minimax-launcher"
CREDENTIALS_FILENAME = "credentials.dpapi"

__all__ = [
    "CREDENTIALS_VERSION",
    "SUPPORTED_VERSIONS",
    "CredentialStore",
    "CredentialStoreCorrupt",
    "CredentialStoreError",
    "CredentialStoreMalformed",
    "CredentialStoreMissing",
    "CredentialStoreUnsupported",
    "CredentialStatus",
    "CredentialStoreVersionError",
    "RunPodCredentials",
    "default_backend_name",
    "default_credentials_path",
]


class CredentialStoreError(RuntimeError):
    """Base class for secure credential store failures.

    Messages are fixed and safe: they never contain ciphertext or plaintext.
    """


class CredentialStoreUnsupported(CredentialStoreError):
    """The DPAPI store was used on a platform that does not support it."""


class CredentialStoreMissing(CredentialStoreError):
    """No credential file exists at the store path."""


class CredentialStoreCorrupt(CredentialStoreError):
    """The file exists but its ciphertext cannot be read or decrypted."""


class CredentialStoreMalformed(CredentialStoreError):
    """The decrypted payload is not a valid credential document."""


class CredentialStoreVersionError(CredentialStoreError):
    """The decrypted payload uses an unsupported schema version."""


@dataclass(frozen=True)
class RunPodCredentials:
    """The decoded credential bundle (secret; repr is redacted).

    ``extra`` holds the optional extra API keys (e.g. ``HF_TOKEN`` /
    ``CIVITAI_API_KEY``) from v2 payloads; version 1 payloads load with an
    empty map. ``comfy_template_id`` (v3) is the private template ID of the
    ComfyUI/MiniMax H3 image, ``train_template_id`` (v5) that of the LoRA
    training image, and ``vnc_password`` (v5) the desktop credential that
    image serves; older payloads load with them unset.
    """

    runpod_api_key: str
    runpod_template_id: str
    extra: dict[str, str] = field(default_factory=dict)
    comfy_template_id: Optional[str] = None
    train_template_id: Optional[str] = None
    vnc_password: Optional[str] = None

    def __repr__(self) -> str:  # pragma: no cover - defensive, not a behavior
        return "RunPodCredentials(<redacted>)"


@dataclass(frozen=True)
class CredentialStatus:
    """Safe, value-free view of the credential store state.

    ``extra_keys`` lists only the NAMES of configured extra secrets — never
    their values.
    """

    file_present: bool
    api_key_configured: bool
    template_configured: bool
    readable: bool
    extra_keys: tuple[str, ...] = ()
    comfy_template_configured: bool = False
    train_template_configured: bool = False
    vnc_password_configured: bool = False


def _require_windows() -> None:
    if os.name != "nt":
        raise CredentialStoreUnsupported(
            "the secure credential store requires Windows DPAPI and is not "
            "available on this platform"
        )


def default_credentials_path() -> Path:
    """Return the deterministic per-user credential store path.

    Windows: ``%APPDATA%\\MiniMaxH3Launcher\\credentials.dpapi``.
    macOS: ``~/Library/Application Support/MiniMaxH3Launcher/credentials.dpapi``.
    Linux: ``$XDG_DATA_HOME/minimax-launcher/credentials.dpapi`` (or
    ``~/.local/share/...``).

    Raises :class:`CredentialStoreUnsupported` only when the per-user
    directory cannot be resolved at all (no ``APPDATA``, no ``HOME``).
    """
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise CredentialStoreUnsupported(
                "APPDATA is not set; cannot resolve the credential store path"
            )
        return Path(appdata) / CREDENTIALS_DIRNAME / CREDENTIALS_FILENAME
    if sys.platform == "darwin":
        home = os.environ.get("HOME")
        if not home:
            raise CredentialStoreUnsupported(
                "HOME is not set; cannot resolve the credential store path"
            )
        return (
            Path(home) / "Library" / "Application Support"
            / CREDENTIALS_DIRNAME / CREDENTIALS_FILENAME
        )
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        root = Path(base)
    else:
        home = os.environ.get("HOME")
        if not home:
            raise CredentialStoreUnsupported(
                "HOME is not set; cannot resolve the credential store path"
            )
        root = Path(home) / ".local" / "share"
    return root / CREDENTIALS_DIRNAME_LINUX / CREDENTIALS_FILENAME


#: Service/account names of the external stores (Keychain, Secret Service).
_VAULT_SERVICE = "minimax-launcher"
_VAULT_ACCOUNT = "runpod-credentials"


def _which(name: str) -> bool:
    """True when *name* is an executable on PATH."""
    from shutil import which

    return which(name) is not None


def _run_vault_cli(argv: list[str], stdin_text: Optional[str] = None) -> tuple[int, str]:
    """Run a credential-store CLI, returning ``(returncode, stdout)``.

    Never raises: a missing binary is reported as a non-zero code so the
    caller can classify it.
    """
    try:
        proc = subprocess.run(
            argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, (proc.stdout or "").strip()


def _keychain_protect(data: bytes) -> bytes:
    """Store *data* in the login Keychain; return the token the file holds."""
    payload = base64.b64encode(data).decode("ascii")
    code, _ = _run_vault_cli(
        [
            "security", "add-generic-password", "-U",
            "-a", _VAULT_ACCOUNT, "-s", _VAULT_SERVICE, "-w", payload,
        ]
    )
    if code != 0:
        raise CredentialStoreError("failed to store credentials in the Keychain")
    return _vault_token()


def _keychain_unprotect(_blob: bytes) -> bytes:
    code, out = _run_vault_cli(
        [
            "security", "find-generic-password",
            "-a", _VAULT_ACCOUNT, "-s", _VAULT_SERVICE, "-w",
        ]
    )
    if code != 0 or not out:
        raise CredentialStoreCorrupt(
            "stored credentials could not be read from the Keychain"
        )
    try:
        return base64.b64decode(out)
    except (ValueError, binascii.Error) as exc:
        raise CredentialStoreCorrupt(
            "stored credentials could not be decoded"
        ) from exc


def _keychain_drop() -> None:
    _run_vault_cli(
        [
            "security", "delete-generic-password",
            "-a", _VAULT_ACCOUNT, "-s", _VAULT_SERVICE,
        ]
    )


def _secret_service_protect(data: bytes) -> bytes:
    """Store *data* in the Secret Service; return the token the file holds."""
    payload = base64.b64encode(data).decode("ascii")
    code, _ = _run_vault_cli(
        [
            "secret-tool", "store",
            "--label=MiniMax H3 Launcher credentials",
            "service", _VAULT_SERVICE, "account", _VAULT_ACCOUNT,
        ],
        stdin_text=payload + "\n",
    )
    if code != 0:
        raise CredentialStoreError("failed to store credentials in the Secret Service")
    return _vault_token()


def _secret_service_unprotect(_blob: bytes) -> bytes:
    code, out = _run_vault_cli(
        [
            "secret-tool", "lookup",
            "service", _VAULT_SERVICE, "account", _VAULT_ACCOUNT,
        ]
    )
    if code != 0 or not out:
        raise CredentialStoreCorrupt(
            "stored credentials could not be read from the Secret Service"
        )
    try:
        return base64.b64decode(out)
    except (ValueError, binascii.Error) as exc:
        raise CredentialStoreCorrupt(
            "stored credentials could not be decoded"
        ) from exc


def _secret_service_drop() -> None:
    _run_vault_cli(
        ["secret-tool", "clear", "service", _VAULT_SERVICE, "account", _VAULT_ACCOUNT]
    )


def _vault_token() -> bytes:
    return f"vault:{_VAULT_SERVICE}:{_VAULT_ACCOUNT}".encode("ascii")


def _file_protect(data: bytes) -> bytes:
    """Identity: the fallback store relies on the file's ``0600`` mode."""
    return data


def _file_unprotect(blob: bytes) -> bytes:
    return blob


def default_backend_name() -> str:
    """Which backend this machine will use (``dpapi``/``keychain``/``secret-service``/``file``)."""
    if os.name == "nt":
        return "dpapi"
    if sys.platform == "darwin" and _which("security"):
        return "keychain"
    if _which("secret-tool"):
        return "secret-service"
    return "file"


def _default_protect() -> Callable[[bytes], bytes]:
    backend = default_backend_name()
    if backend == "dpapi":
        return _dpapi_protect
    if backend == "keychain":
        return _keychain_protect
    if backend == "secret-service":
        return _secret_service_protect
    return _file_protect


def _default_unprotect() -> Callable[[bytes], bytes]:
    backend = default_backend_name()
    if backend == "dpapi":
        return _dpapi_unprotect
    if backend == "keychain":
        return _keychain_unprotect
    if backend == "secret-service":
        return _secret_service_unprotect
    return _file_unprotect


def _drop_external_entry() -> None:
    """Remove the entry from the platform store (no-op for the file backend)."""
    backend = default_backend_name()
    if backend == "keychain":
        _keychain_drop()
    elif backend == "secret-service":
        _secret_service_drop()


def _dpapi_protect(data: bytes) -> bytes:
    """Encrypt *data* with DPAPI (current-user scope, no UI prompts)."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = (
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        )

    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    src_buffer = ctypes.create_string_buffer(data, len(data))
    src = DATA_BLOB(len(data), ctypes.cast(src_buffer, ctypes.POINTER(ctypes.c_byte)))
    dst = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(
        ctypes.byref(src),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(dst),
    ):
        raise CredentialStoreError("failed to encrypt RunPod credentials")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    """Decrypt a DPAPI *blob* (current-user scope, no UI prompts)."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = (
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        )

    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    src_buffer = ctypes.create_string_buffer(blob, len(blob))
    src = DATA_BLOB(len(blob), ctypes.cast(src_buffer, ctypes.POINTER(ctypes.c_byte)))
    dst = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(src),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(dst),
    ):
        raise CredentialStoreCorrupt(
            "stored RunPod credentials could not be decrypted"
        )
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _apply_user_only_acl(path: Path) -> None:
    """Best-effort: restrict *path* to the current user.

    Windows: an ACL through ``icacls`` (the DPAPI ciphertext is already
    user-bound, so this is defense in depth). POSIX: mode ``0600``, which the
    plaintext file fallback actually depends on.
    """
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    username = os.environ.get("USERNAME")
    if not username:
        return
    try:
        winproc.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _clean_secret(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise CredentialStoreError(f"{label} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise CredentialStoreError(f"{label} must not be empty")
    return cleaned


def _parse_payload(plaintext: bytes) -> dict:
    """Parse and validate a decrypted credential payload.

    Raises a classified error on any structural problem; no payload content
    ever reaches the exception message.
    """
    try:
        data = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CredentialStoreMalformed(
            "stored RunPod credentials are malformed"
        )
    if not isinstance(data, dict):
        raise CredentialStoreMalformed(
            "stored RunPod credentials are malformed"
        )
    if data.get("version") not in SUPPORTED_VERSIONS:
        raise CredentialStoreVersionError(
            "stored RunPod credentials use an unsupported version"
        )
    for field_name in ("runpod_api_key", "runpod_template_id"):
        value = data.get(field_name)
        if not isinstance(value, str) or not value:
            raise CredentialStoreMalformed(
                "stored RunPod credentials are malformed"
            )
    extra_raw = data.get("extra_secrets", {})
    if not isinstance(extra_raw, dict):
        raise CredentialStoreMalformed(
            "stored RunPod credentials are malformed"
        )
    extra: dict[str, str] = {}
    for key, value in extra_raw.items():
        if not isinstance(key, str) or not key.strip():
            raise CredentialStoreMalformed(
                "stored RunPod credentials are malformed"
            )
        if not isinstance(value, str) or not value.strip():
            raise CredentialStoreMalformed(
                "stored RunPod credentials are malformed"
            )
        extra[key.strip().upper()] = value.strip()
    comfy_template_id_raw = data.get("comfy_template_id")
    if comfy_template_id_raw is not None:
        if not isinstance(comfy_template_id_raw, str):
            raise CredentialStoreMalformed(
                "stored RunPod credentials are malformed"
            )
        comfy_template_id = comfy_template_id_raw.strip() or None
    else:
        comfy_template_id = None
    train_template_id_raw = data.get("train_template_id")
    if train_template_id_raw is not None:
        if not isinstance(train_template_id_raw, str):
            raise CredentialStoreMalformed(
                "stored RunPod credentials are malformed"
            )
        train_template_id = train_template_id_raw.strip() or None
    else:
        train_template_id = None
    vnc_password_raw = data.get("vnc_password")
    if vnc_password_raw is not None:
        if not isinstance(vnc_password_raw, str):
            raise CredentialStoreMalformed(
                "stored RunPod credentials are malformed"
            )
        vnc_password = vnc_password_raw.strip() or None
    else:
        vnc_password = None
    return {
        "version": data["version"],
        "runpod_api_key": data["runpod_api_key"],
        "runpod_template_id": data["runpod_template_id"],
        "extra": extra,
        "comfy_template_id": comfy_template_id,
        "train_template_id": train_template_id,
        "vnc_password": vnc_password,
    }


def _clean_extra(extra: Mapping[str, str]) -> dict[str, str]:
    """Normalize an explicit extra-secrets mapping.

    Keys are trimmed and upper-cased; values are trimmed. A blank value means
    "drop this key" (the mapping fully defines the new extra set). Any
    structural problem raises a classified, safe error.
    """
    result: dict[str, str] = {}
    for key, value in extra.items():
        if not isinstance(key, str):
            raise CredentialStoreError("extra secret names must be strings")
        name = key.strip().upper()
        if not name:
            raise CredentialStoreError("extra secret names must not be empty")
        if not isinstance(value, str):
            raise CredentialStoreError(f"{name} must be a string")
        cleaned = value.strip()
        if cleaned:
            result[name] = cleaned
    return result


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


class CredentialStore:
    """Read/write the DPAPI-encrypted RunPod credential bundle.

    Parameters
    ----------
    path:
        Explicit file path. When omitted, the deterministic per-user default
        (``%APPDATA%\\MiniMaxH3Launcher\\credentials.dpapi``) is resolved lazily on
        first use.
    protect:
        Injectable encryption function ``bytes -> bytes`` (test hook).
        Defaults to DPAPI ``CryptProtectData``.
    unprotect:
        Injectable decryption function ``bytes -> bytes`` (test hook).
        Defaults to DPAPI ``CryptUnprotectData``.
    apply_user_only_acl:
        Apply a best-effort user-only ACL to the store directory and file on
        write (Windows only, failures ignored).
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        protect: Optional[Callable[[bytes], bytes]] = None,
        unprotect: Optional[Callable[[bytes], bytes]] = None,
        apply_user_only_acl: bool = True,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._protect = protect if protect is not None else _default_protect()
        self._unprotect = (
            unprotect if unprotect is not None else _default_unprotect()
        )
        self._apply_user_only_acl = apply_user_only_acl
        self.backend = (
            "injected" if protect is not None or unprotect is not None
            else default_backend_name()
        )

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = default_credentials_path()
        return self._path

    def set(
        self,
        runpod_api_key: str,
        runpod_template_id: str,
        extra: Optional[Mapping[str, str]] = None,
        comfy_template_id: Optional[str] = None,
        train_template_id: Optional[str] = None,
        vnc_password: Optional[str] = None,
    ) -> None:
        """Store (or rotate) the credential bundle.

        Encrypts, writes atomically (temp file + ``os.replace``), and verifies
        the result by reading the file back and decrypting it. Raises a
        classified :class:`CredentialStoreError` on any failure without
        exposing secrets.

        ``extra`` (v2) sets the extra API keys (e.g. ``HF_TOKEN`` /
        ``CIVITAI_API_KEY``). When ``None``, the currently stored extra keys
        are preserved (a missing store means "none"). An explicit mapping
        fully defines the new extra set: a blank value drops that key.

        ``comfy_template_id`` (v3) is the private template ID of the
        ComfyUI/MiniMax H3 image. ``None`` preserves the stored value; a
        blank string clears it; a non-blank value stores it.

        ``train_template_id`` (v5) is the private template ID of the LoRA
        training image, with the same semantics.

        ``vnc_password`` (v5) is the desktop/file-manager credential the
        training image serves, with the same semantics. It is a secret like
        any other: it gates a browser-reachable desktop, so it belongs here
        rather than in a plain environment variable.
        """
        api_key = _clean_secret(runpod_api_key, "RunPod API key")
        template_id = _clean_secret(runpod_template_id, "RunPod template ID")
        try:
            stored = self._read_payload()
        except CredentialStoreMissing:
            stored = None
        except (CredentialStoreCorrupt, CredentialStoreMalformed, CredentialStoreVersionError):
            # An unreadable/corrupt store must not make the store unwritable:
            # the user's only escape used to be `credentials clear`, which the
            # error message never mentioned. The write below is verified, so
            # starting from an empty bundle is safe.
            stored = None
        if extra is None:
            extra_map = dict(stored.get("extra", {})) if stored else {}
        else:
            extra_map = _clean_extra(extra)
        if comfy_template_id is None:
            comfy_id: Optional[str] = stored.get("comfy_template_id") if stored else None
        else:
            comfy_id = comfy_template_id.strip() or None
        if train_template_id is None:
            train_id: Optional[str] = (
                stored.get("train_template_id") if stored else None
            )
        else:
            train_id = train_template_id.strip() or None
        if vnc_password is None:
            vnc_pw: Optional[str] = stored.get("vnc_password") if stored else None
        else:
            vnc_pw = vnc_password.strip() or None
        payload_data = {
            "version": CREDENTIALS_VERSION,
            "runpod_api_key": api_key,
            "runpod_template_id": template_id,
            "extra_secrets": extra_map,
        }
        if comfy_id is not None:
            payload_data["comfy_template_id"] = comfy_id
        if train_id is not None:
            payload_data["train_template_id"] = train_id
        if vnc_pw is not None:
            payload_data["vnc_password"] = vnc_pw
        payload = json.dumps(payload_data, sort_keys=True).encode("utf-8")
        try:
            ciphertext = self._protect(payload)
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("failed to encrypt credentials")
        self._atomic_write(self.path, ciphertext)
        self._verify_written(
            api_key, template_id, extra_map, comfy_id, train_id, vnc_pw
        )

    def get(self) -> RunPodCredentials:
        """Load and decrypt the credential bundle.

        Raises :class:`CredentialStoreMissing`,
        :class:`CredentialStoreCorrupt`,
        :class:`CredentialStoreMalformed`, or
        :class:`CredentialStoreVersionError` with a safe, fixed message.
        """
        data = self._read_payload()
        return RunPodCredentials(
            runpod_api_key=data["runpod_api_key"],
            runpod_template_id=data["runpod_template_id"],
            extra=dict(data.get("extra", {})),
            comfy_template_id=data.get("comfy_template_id"),
            train_template_id=data.get("train_template_id"),
            vnc_password=data.get("vnc_password"),
        )

    def status(self) -> CredentialStatus:
        """Return a value-free view of the store state (never raises for
        missing or unreadable files)."""
        if not self.path.is_file():
            return CredentialStatus(False, False, False, False)
        try:
            creds = self.get()
        except CredentialStoreError:
            return CredentialStatus(True, False, False, False)
        return CredentialStatus(
            True,
            True,
            True,
            True,
            tuple(sorted(creds.extra)),
            comfy_template_configured=creds.comfy_template_id is not None,
            train_template_configured=creds.train_template_id is not None,
            vnc_password_configured=creds.vnc_password is not None,
        )

    def clear(self) -> bool:
        """Remove the credential file (best-effort zero overwrite first).

        Returns whether a file was removed. Idempotent: a missing file is not
        an error, and a file another process holds (antivirus, a second
        launcher) raises :class:`CredentialStoreError` — never a bare
        ``OSError``, which the CLI and the GUI do not catch.
        """
        try:
            path = self.path
        except CredentialStoreError:
            raise
        if not path.is_file():
            return False
        try:
            with path.open("r+b") as fh:
                size = fh.seek(0, os.SEEK_END)
                if size:
                    fh.seek(0)
                    fh.write(b"\x00" * size)
                    fh.flush()
                    os.fsync(fh.fileno())
        except OSError:
            pass
        try:
            path.unlink()
        except OSError as exc:
            raise CredentialStoreError(
                f"could not remove the credential file {path}: {exc}"
            ) from exc
        # The secret itself lives outside the file on macOS/Linux: dropping
        # only the file would leave a readable copy in the Keychain.
        _drop_external_entry()
        return True

    def _read_payload(self) -> dict:
        path = self.path
        if not path.is_file():
            raise CredentialStoreMissing("RunPod credentials are not stored")
        try:
            raw = path.read_bytes()
        except OSError:
            raise CredentialStoreCorrupt(
                "stored RunPod credentials could not be read"
            )
        try:
            plaintext = self._unprotect(raw)
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreCorrupt(
                "stored RunPod credentials could not be decrypted"
            )
        return _parse_payload(plaintext)

    def _verify_written(
        self,
        api_key: str,
        template_id: str,
        extra: dict[str, str],
        comfy_template_id: Optional[str] = None,
        train_template_id: Optional[str] = None,
        vnc_password: Optional[str] = None,
    ) -> None:
        data = self._read_payload()
        if (
            data["runpod_api_key"],
            data["runpod_template_id"],
            data.get("extra", {}),
            data.get("comfy_template_id"),
            data.get("train_template_id"),
            data.get("vnc_password"),
        ) != (
            api_key,
            template_id,
            extra,
            comfy_template_id,
            train_template_id,
            vnc_password,
        ):
            raise CredentialStoreError("credential write verification failed")

    def _atomic_write(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".credentials-", suffix=".tmp", dir=str(target.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if self._apply_user_only_acl:
                _apply_user_only_acl(target.parent)
                _apply_user_only_acl(tmp_path)
            os.replace(tmp_path, target)
        except Exception as exc:
            _quiet_unlink(tmp_path)
            raise CredentialStoreError(
                "failed to store RunPod credentials"
            ) from exc
