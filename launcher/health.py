"""Readiness probes for the workload stacks, over the local SSH tunnel.

Each stack exposes a different readiness signal:

* ``comfy`` — ComfyUI ``GET /system_stats`` (plus the queue and VRAM views);
* ``train`` — the KasmVNC desktop, whose 401 is the READY answer.

Uses the standard library (``urllib``) with bounded retries/timeouts and
actionable errors. This module is credential-independent and fully unit-testable
(no GPU, RunPod, or SSH server required at import time).
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional

class HealthError(RuntimeError):
    """Raised when a stack service is unreachable or not ready in time."""


def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise HealthError(f"GET {url} -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HealthError(f"GET {url} failed: {exc.reason}") from exc
    except http.client.HTTPException as exc:
        # A malformed status line / truncated body is not an OSError: a
        # half-started server, or a non-HTTP service answering on the forwarded
        # port, raises BadStatusLine or IncompleteRead. That is exactly the
        # "keep waiting" case, not a crash.
        raise HealthError(f"GET {url} protocol error: {exc}") from exc
    except OSError as exc:
        # Transient network/socket failures during pod startup: connection
        # reset, refused, timeout, etc. These are retryable readiness signals,
        # not permanent errors.
        raise HealthError(f"GET {url} transient network error: {exc}") from exc


# ---------------------------------------------------------------------------
# ComfyUI / MiniMax H3 stack (the ``comfy`` stack)
# ---------------------------------------------------------------------------

COMFY_STATS_PATH = "/system_stats"
COMFY_QUEUE_PATH = "/api/queue"
DEFAULT_COMFY_WAIT_TIMEOUT = 3600.0
DEFAULT_COMFY_WAIT_INTERVAL = 10.0

# The training pod pulls a large image, resolves the venv, starts KasmVNC and
# only then draws the app on the virtual screen — and with FETCH_MODELS set it
# downloads ~45 GB of weights first. Same generous budget as ComfyUI for the
# same reason.
DEFAULT_TRAIN_WAIT_TIMEOUT = 3600.0
DEFAULT_TRAIN_WAIT_INTERVAL = 10.0


def check_comfy(base_url: str, timeout: float = 5.0) -> bool:
    """Return True if the ComfyUI server at *base_url* answers ``/system_stats``.

    ``base_url`` is the host root (no API version segment), e.g.
    ``http://127.0.0.1:8188`` through the SSH tunnel. A non-JSON or
    incomplete response — or an unreachable host (tunnel down) — means
    "not ready yet"; this function never raises.
    """
    try:
        body = _http_get(f"{base_url.rstrip('/')}{COMFY_STATS_PATH}", timeout)
    except (HealthError, OSError):
        return False
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return False
    return isinstance(data, Mapping) and "system" in data


def comfy_vram_text(stats: Mapping[str, Any]) -> str:
    """``"12.3 / 48.0 Go"`` for the first GPU of a ``/system_stats`` payload.

    ComfyUI reports the memory **flat** on each device
    (``vram_total`` / ``vram_free``, plus its own ``torch_vram_*`` counters) —
    reading a nested ``devices[].vram.total`` (as this used to) never matched
    anything, which is why the dashboard showed no VRAM at all. Returns ""
    when the payload carries no usable device.
    """
    devices = stats.get("devices") or ()
    for device in devices:
        if not isinstance(device, Mapping):
            continue
        total = device.get("vram_total")
        free = device.get("vram_free")
        if not isinstance(total, (int, float)) or not isinstance(free, (int, float)):
            continue
        if total <= 0:
            continue
        used = max(0.0, float(total) - float(free))
        return f"{used / (1024 ** 3):.1f} / {float(total) / (1024 ** 3):.1f} Go"
    return ""


def get_comfy_stats(base_url: str, timeout: float = 5.0) -> dict:
    """Return the parsed ``/system_stats`` mapping ({} when unavailable).

    Never raises: dashboards and tools degrade to empty instead of failing.
    """
    try:
        body = _http_get(f"{base_url.rstrip('/')}{COMFY_STATS_PATH}", timeout)
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return {}
    return data if isinstance(data, Mapping) else {}


def get_comfy_queue(base_url: str, timeout: float = 5.0) -> dict:
    """Return ``{"running": n, "pending": n}`` from ``/api/queue`` ({} on error)."""
    try:
        body = _http_get(f"{base_url.rstrip('/')}{COMFY_QUEUE_PATH}", timeout)
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return {}
    if not isinstance(data, Mapping):
        return {}
    return {
        "running": len(data.get("queue_running") or ()),
        "pending": len(data.get("queue_pending") or ()),
    }


def wait_comfy(
    base_url: str,
    timeout: float = DEFAULT_COMFY_WAIT_TIMEOUT,
    interval: float = DEFAULT_COMFY_WAIT_INTERVAL,
    health_timeout: float = 5.0,
) -> None:
    """Poll until the ComfyUI server answers, or *timeout*.

    The default timeout is generous on purpose: after the pod is RUNNING,
    the container entrypoint still has to verify/swap PyTorch, download the
    preset's model weights (tens of GB), install the preset's custom nodes,
    and then start ComfyUI.
    """
    deadline = time.monotonic() + timeout
    last_error: Optional[str] = None
    while True:
        try:
            if check_comfy(base_url, health_timeout):
                return
            last_error = "ComfyUI /system_stats not answering yet"
        except HealthError as exc:
            last_error = str(exc)
        except OSError as exc:
            last_error = f"transient network error: {exc}"
        if time.monotonic() >= deadline:
            raise HealthError(
                f"ComfyUI not ready at {base_url} after {timeout:.0f}s "
                f"(last error: {last_error}). The pod may still be downloading "
                "the preset's model weights — re-run the start or check the "
                "pod logs in the RunPod console."
            )
        time.sleep(interval)


def _http_status(url: str, timeout: float) -> Optional[int]:
    """Return the HTTP status of a GET, or None when nothing answered.

    Unlike :func:`_http_get` this does NOT treat 4xx/5xx as an error: for the
    training stack a 401 is a success signal (see :func:`check_train`).
    """
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return None


def check_train(base_url: str, timeout: float = 5.0) -> bool:
    """Return True if the training desktop answers at *base_url*.

    KasmVNC serves its web client behind HTTP Basic auth
    (``-DisableBasicAuth=0``), so an unauthenticated request is answered with
    401. That is the READY signal, not a failure — requiring 2xx would wait
    forever on a service that is up and correctly refusing anonymous access.

    A bare TCP connect would not do either. With ``ssh -L`` the local socket is
    accepted before ssh knows whether the remote port is listening, so a plain
    connect can succeed against a pod whose desktop has not started yet. An
    actual HTTP response is the first evidence that the remote service is
    really there.

    Any *other* status (404, 500, 502 …) is NOT readiness: the local socket is
    accepted by ``ssh -L`` before the remote port is known to be listening, so
    an unrelated service or an internal-proxy error page would otherwise be
    declared ready and the launcher would open a dead desktop.
    """
    status = _http_status(base_url, timeout)
    if status is None:
        return False
    return status == 401 or 200 <= status < 300


def wait_train(
    base_url: str,
    timeout: float = DEFAULT_TRAIN_WAIT_TIMEOUT,
    interval: float = DEFAULT_TRAIN_WAIT_INTERVAL,
    health_timeout: float = 5.0,
) -> None:
    """Poll until the training desktop answers, or *timeout*.

    Generous on purpose: after the pod is RUNNING, the container still has to
    pull its image, resolve the venv, optionally download ~45 GB of model
    weights (``FETCH_MODELS``), start KasmVNC on the virtual screen and then
    launch the app on it.
    """
    deadline = time.monotonic() + timeout
    last_error: Optional[str] = None
    while True:
        if check_train(base_url, health_timeout):
            return
        last_error = "no HTTP response from the desktop yet"
        if time.monotonic() >= deadline:
            raise HealthError(
                f"Training desktop not ready at {base_url} after {timeout:.0f}s "
                f"(last error: {last_error}). The pod may still be pulling its "
                "image or downloading model weights — check the pod log in the "
                "RunPod console; the entrypoint prints 'Serving Fizgig on "
                ":6080' when it is up."
            )
        time.sleep(interval)
