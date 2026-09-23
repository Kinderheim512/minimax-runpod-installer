"""RunPod REST API v2 client for the MiniMax H3 Launcher.

Thin, typed client for the RunPod REST API (``https://api.runpod.io/v2``).
It covers the subset of the API the launcher needs for pod lifecycle and
provisioning:

* ``GET  /v2/pods/{id}``          — inspect a pod.
* ``POST /v2/pods/{id}/action``   — start / stop / restart / terminate.
* ``POST /v2/pods``               — create a pod from a private template.
* ``GET  /v2/templates/{id}``     — check template availability.
* ``GET  /v2/pods``               — list pods.
* ``GET  /v2/ssh-keys``           — list registered SSH keys.
* ``GET  /v2/catalog/gpus``       — GPU catalog (availability is advisory).
* ``POST /graphql``               — wallet balance (``myself`` query).

This module is credential-independent and fully unit-testable: it performs
plain HTTP requests via the standard library and never requires a real pod,
GPU, or API key at import time.

See https://docs.runpod.io/api-reference-v2/ for the API reference.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from . import logging as launcher_logging

logger = launcher_logging.get_logger("minimax-launcher.runpod")

DEFAULT_BASE_URL = "https://api.runpod.io/v2"

# A browser-like User-Agent. RunPod's API is fronted by Cloudflare, which
# rejects the default `urllib` request (no User-Agent) with HTTP 403
# "error code: 1010". An explicit User-Agent avoids that block.
USER_AGENT = (
    "MiniMaxH3Launcher/1.0 "
    "(+https://github.com/Kinderheim512/minimax-runpod-installer)"
)

_WALLET_QUERY = """
    query {
        myself {
            email
            clientBalance
            currentSpendPerHr
            spendLimit
        }
    }
"""

# Pod lifecycle statuses (see RunPod API v2 `PodStatus`).
STATUS_PROVISIONING = "PROVISIONING"
STATUS_STARTING = "STARTING"
STATUS_RUNNING = "RUNNING"
STATUS_EXITED = "EXITED"
STATUS_ERROR = "ERROR"
STATUS_TERMINATED = "TERMINATED"

#: Statuses a polling loop must treat as final: waiting longer cannot change
#: them, and the generic "no SSH mapping after 600 s" message hid the cause.
_TERMINAL_STATUSES = frozenset({STATUS_ERROR, STATUS_EXITED, STATUS_TERMINATED})

# Statuses from which a pod can be started.
_STARTABLE_STATUSES = {STATUS_EXITED, STATUS_ERROR}

# Statuses that indicate the pod is on its way to being ready.
_TRANSIENT_STATUSES = {STATUS_PROVISIONING, STATUS_STARTING}


class RunPodError(RuntimeError):
    """Base class for RunPod client errors."""


class PodNotFoundError(RunPodError):
    """Raised when the requested pod does not exist or is not accessible."""


class InvalidActionError(RunPodError):
    """Raised when a state transition is not valid for the pod's status."""


class ApiError(RunPodError):
    """Raised for other API/HTTP failures."""


class TransientNetworkError(ApiError):
    """Raised when the connection itself fails (DNS, TLS reset, read EOF, timeout).

    The API never produced an HTTP response, so the failure is inherently
    transient (a load-balancer reset, a momentary outage): callers should
    retry within a bounded window. Subclasses :class:`ApiError` so existing
    error handling keeps working unchanged.
    """


class TemplateNotFoundError(RunPodError):
    """Raised when a template does not exist or is not accessible."""


class ContractError(RunPodError):
    """Raised when RunPod rejects a pod request with validation errors."""

    def __init__(self, message: str, errors: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.errors = tuple(errors)


class PlacementError(RunPodError):
    """Raised when RunPod cannot place a pod (e.g. no capacity)."""


class InsufficientFundsError(RunPodError):
    """Raised when the account balance is insufficient to create a pod."""


class AccessDeniedError(RunPodError):
    """Raised when the API key lacks permission for the requested resource."""


class RateLimitedError(RunPodError):
    """Raised when RunPod rate-limits pod creation after retries."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientApiError(RunPodError):
    """Raised when transient server errors persist across bounded retries."""


class _RawHttpError(Exception):
    """Internal: an HTTP error with decoded body and headers, pre-mapping."""

    def __init__(self, code: int, detail: str, headers: Mapping[str, str]) -> None:
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.detail = detail
        self.headers = dict(headers)


_CREATE_MAX_ATTEMPTS = 3
_CREATE_RETRY_BACKOFF = (5.0, 10.0)
_RATE_LIMIT_MIN_WAIT = 5.0


@dataclass(frozen=True)
class SshEndpoint:
    """An SSH connection endpoint for a pod."""

    host: str
    port: int
    username: str


@dataclass(frozen=True)
class PortMapping:
    """One exposed port of a pod (from ``runtime.ports``).

    ``public_url`` is the console-style public URL for the mapping: HTTPS for
    ``http``-protocol entries (the ComfyUI direct-access case), plain HTTP for
    exposed ``tcp`` ports that have a public port. ``None`` when no public
    host is available yet (pod still provisioning).
    """

    private_port: int
    ip: str
    public_port: Optional[int] = None
    protocol: str = "tcp"

    @property
    def public_url(self) -> Optional[str]:
        if not self.ip:
            return None
        if self.protocol == "http":
            suffix = ""
            if self.public_port is not None and self.public_port not in (80, 443):
                suffix = f":{self.public_port}"
            return f"https://{self.ip}{suffix}"
        if self.public_port is not None:
            return f"http://{self.ip}:{self.public_port}"
        return f"http://{self.ip}:{self.private_port}"


@dataclass(frozen=True)
class Pod:
    """A minimal, typed view of a RunPod pod."""

    id: str
    name: str
    status: str
    actions: tuple[str, ...] = ()
    cuda_version: Optional[str] = None
    gpu_id: Optional[str] = None
    proxy_endpoint: Optional[SshEndpoint] = None
    direct_endpoint: Optional[SshEndpoint] = None
    tcp_ssh_endpoint: Optional[SshEndpoint] = None
    data_center_id: Optional[str] = None
    image: Optional[str] = None
    disk: Optional[int] = None
    cost_per_hour: Optional[float] = None
    template_id: Optional[str] = None
    env: Optional[Mapping[str, str]] = None
    port_mappings: tuple[PortMapping, ...] = ()
    started_at: Optional[float] = None
    created_at: Optional[float] = None

    def public_url_for_port(self, private_port: int) -> Optional[str]:
        """Public URL of the first exposed mapping for *private_port* (or None)."""
        for mapping in self.port_mappings:
            if mapping.private_port == private_port:
                url = mapping.public_url
                if url is not None:
                    return url
        return None

    @property
    def is_running(self) -> bool:
        return self.status == STATUS_RUNNING

    @property
    def is_startable(self) -> bool:
        return self.status in _STARTABLE_STATUSES

    @property
    def is_transient(self) -> bool:
        return self.status in _TRANSIENT_STATUSES

    def ssh_tunnel_endpoint(self) -> Optional[SshEndpoint]:
        """Return the best SSH endpoint for ``-L`` port forwarding.

        Prefers ``direct_endpoint`` (full SSH feature set), then the derived
        ``tcp_ssh_endpoint`` (exposed TCP 22 via ``runtime.ports``). ``ssh.proxy``
        is intentionally excluded because it does not support port forwarding.
        """
        return self.direct_endpoint or self.tcp_ssh_endpoint


def _as_int(value: Any) -> Optional[int]:
    """Best-effort int, for values RunPod sends in several shapes.

    ``runtime.ports`` entries are documented as "partial while a pod is
    provisioning": ``private: null`` or ``public: "auto"`` raised a bare
    ``TypeError``/``ValueError`` out of the parser instead of being skipped.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _parse_ssh_endpoint(data: Optional[Mapping[str, Any]]) -> Optional[SshEndpoint]:
    if not data:
        return None
    host = data.get("host")
    port = _as_int(data.get("port"))
    username = data.get("username")
    if not host or port is None or not username:
        return None
    return SshEndpoint(host=str(host), port=port, username=str(username))


def _parse_tcp_ssh_endpoint(runtime: Optional[Mapping[str, Any]]) -> Optional[SshEndpoint]:
    """Derive an SSH endpoint from ``runtime.ports`` (exposed TCP 22).

    RunPod exposes "SSH over exposed TCP" as a ``runtime.ports`` entry with
    ``private=22``, ``type=tcp``, ``public=<external port>``, ``ip=<public host>``.
    This is the dynamic endpoint the console shows and which must be used for
    ``ssh -L`` when ``ssh.direct`` is absent.
    """
    if not runtime:
        return None
    ports = runtime.get("ports")
    if not isinstance(ports, list):
        return None
    for entry in ports:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("type") != "tcp":
            continue
        if _as_int(entry.get("private")) != 22:
            continue
        ip = entry.get("ip")
        public = _as_int(entry.get("public"))
        if not ip or public is None:
            continue
        return SshEndpoint(host=str(ip), port=public, username="root")
    return None


def _parse_retry_after(headers: Mapping[str, str]) -> Optional[float]:
    """Return the Retry-After value in seconds, or None when absent/invalid."""
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _parse_contract_errors(detail: str) -> tuple[str, ...]:
    """Extract human-readable validation messages from a 422 response body."""
    try:
        data = json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        return ()
    raw = None
    if isinstance(data, Mapping):
        detail_obj = data.get("detail")
        if isinstance(detail_obj, Mapping) and isinstance(
            detail_obj.get("errors"), list
        ):
            raw = detail_obj["errors"]
        elif isinstance(data.get("errors"), list):
            raw = data["errors"]
    if not isinstance(raw, list):
        return ()
    messages: list[str] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            msg = entry.get("msg") or entry.get("message")
            if msg:
                messages.append(str(msg))
        elif isinstance(entry, str):
            messages.append(entry)
    return tuple(messages)


def _parse_iso_epoch(value: Any) -> Optional[float]:
    """Parse a RunPod ISO-8601 timestamp (``...Z``) to epoch seconds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            # RunPod's timestamps are UTC. ``astimezone`` on a *naive* datetime
            # assumes LOCAL time, which shifted started_at/created_at by the
            # host's UTC offset — and with them the elapsed/cost display and
            # pod_presence's "fresh record" window.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class Wallet:
    """RunPod account wallet (prepaid credits), via the GraphQL ``myself`` query."""

    balance: Optional[float] = None
    spend_per_hour: Optional[float] = None
    spend_limit: Optional[float] = None
    email: Optional[str] = None


def _parse_cost_per_hour(cost: Any) -> Optional[float]:
    """Extract an hourly cost from a scalar ``cost`` or a cost mapping."""
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost)
    if isinstance(cost, Mapping):
        for key in ("costPerHour", "perHour", "price"):
            value = cost.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def _parse_port_mappings(runtime: Optional[Mapping[str, Any]]) -> tuple[PortMapping, ...]:
    """Extract every exposed port mapping from ``runtime.ports``.

    Entries that lack a usable private port or host are skipped (RunPod sends
    partial entries while a pod is provisioning).
    """
    if not runtime:
        return ()
    ports = runtime.get("ports")
    if not isinstance(ports, list):
        return ()
    result: list[PortMapping] = []
    for entry in ports:
        if not isinstance(entry, Mapping):
            continue
        private = entry.get("private")
        if private is None or isinstance(private, bool) or not isinstance(private, (int, float)):
            continue
        public = entry.get("public")
        public_port = (
            int(public)
            if isinstance(public, (int, float)) and not isinstance(public, bool)
            else None
        )
        result.append(
            PortMapping(
                private_port=int(private),
                ip=str(entry.get("ip") or ""),
                public_port=public_port,
                protocol=str(entry.get("type") or "tcp"),
            )
        )
    return tuple(result)


def parse_pod(data: Mapping[str, Any]) -> Pod:
    """Build a :class:`Pod` from a RunPod API v2 pod object.

    Tolerates the nulls RunPod sends while a pod is provisioning or stopped
    (``dataCenterId``, ``ssh.*``, ``runtime``, ``cost`` all may be null).
    """
    ssh = data.get("ssh") or {}

    gpu = data.get("gpu") or {}
    gpu_id = gpu.get("id")

    template = data.get("template")
    if isinstance(template, Mapping):
        template_id = template.get("id")
    else:
        template_id = None
    if template_id is None:
        template_id = data.get("templateId")

    disk_raw = data.get("disk")
    disk = (
        int(disk_raw)
        if isinstance(disk_raw, (int, float)) and not isinstance(disk_raw, bool)
        else None
    )

    env_raw = data.get("env")
    env = dict(env_raw) if isinstance(env_raw, Mapping) else None

    pod_id = data.get("id")
    if not isinstance(pod_id, str) or not pod_id:
        # A 200 body without an id (an action envelope, an error payload, a
        # truncated object) raised a bare KeyError, which is not a RunPodError
        # and therefore escaped every caller's error handling.
        raise ApiError("RunPod pod response is missing 'id'")

    return Pod(
        id=pod_id,
        name=str(data.get("name", "")),
        status=str(data.get("status", "")),
        actions=tuple(data.get("actions") or ()),
        cuda_version=data.get("cudaVersion"),
        gpu_id=str(gpu_id) if gpu_id else None,
        proxy_endpoint=_parse_ssh_endpoint(ssh.get("proxy")),
        direct_endpoint=_parse_ssh_endpoint(ssh.get("direct")),
        tcp_ssh_endpoint=_parse_tcp_ssh_endpoint(data.get("runtime")),
        data_center_id=data.get("dataCenterId") or None,
        image=data.get("image") or None,
        disk=disk,
        cost_per_hour=_parse_cost_per_hour(data.get("cost")),
        template_id=str(template_id) if template_id else None,
        env=env,
        port_mappings=_parse_port_mappings(data.get("runtime")),
        started_at=_parse_iso_epoch(data.get("startedAt")),
        created_at=_parse_iso_epoch(data.get("createdAt")),
    )


class RunPodClient:
    """Minimal typed client for the RunPod REST API v2.

    Parameters
    ----------
    api_key:
        RunPod API key (bearer token). Never logged.
    base_url:
        API base URL. Defaults to ``https://api.runpod.io/v2``.
    timeout:
        HTTP timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise RunPodError("RunPod API key must not be empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._sleep_fn = time.sleep

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": USER_AGENT,
        }

    def _request_raw(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        """Perform the HTTP request; raise :class:`_RawHttpError` on HTTP errors."""
        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=self._headers(), method=method
        )
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                headers = dict(exc.headers.items())
            except (AttributeError, TypeError):
                headers = {}
            raise _RawHttpError(exc.code, detail, headers) from exc
        except urllib.error.URLError as exc:
            raise TransientNetworkError(f"RunPod API network error: {exc.reason}") from exc
        except http.client.HTTPException as exc:
            # ``urllib`` only wraps the *request* phase in URLError: a malformed
            # status line or a truncated body raises an HTTPException subclass
            # (BadStatusLine, IncompleteRead, LineTooLong) that is not an OSError.
            raise TransientNetworkError(
                f"RunPod API protocol error: {exc}"
            ) from exc
        except OSError as exc:
            # Connection reset / aborted / timed out *after* the request was
            # sent. ``urlopen`` does not wrap these in URLError either, so an
            # unhandled OSError used to escape every caller that only knows
            # RunPodError — crashing the launcher on a one-second network blip.
            raise TransientNetworkError(
                f"RunPod API connection error: {exc}"
            ) from exc

        text = raw.decode("utf-8", errors="replace")
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ApiError(f"RunPod API returned invalid JSON: {exc}") from exc

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        """Perform the HTTP request with the standard error mapping."""
        try:
            return self._request_raw(method, path, body)
        except _RawHttpError as exc:
            if exc.code == 404:
                raise PodNotFoundError(
                    f"Pod not found (HTTP 404): {exc.detail}"
                ) from exc
            if exc.code == 409:
                raise InvalidActionError(
                    f"Action not valid for current pod status (HTTP 409): {exc.detail}"
                ) from exc
            if exc.code == 429 or 500 <= exc.code < 600:
                # A 5xx/429 is transient, so the polling loops
                # (wait_ready/wait_ssh_endpoint/wait_public_port) retry it
                # instead of aborting the whole startup on one Cloudflare 502.
                # ``get_pod`` itself still issues exactly one request.
                raise TransientNetworkError(
                    f"RunPod API temporarily unavailable (HTTP {exc.code}): "
                    f"{exc.detail}"
                ) from exc
            raise ApiError(f"RunPod API HTTP {exc.code}: {exc.detail}") from exc

    def get_pod(self, pod_id: str) -> Pod:
        """Return the current state of *pod_id*."""
        data = self._request("GET", f"/pods/{pod_id}")
        return parse_pod(data)

    def _action(self, pod_id: str, action: str) -> Pod:
        try:
            data = self._request_raw("POST", f"/pods/{pod_id}/action", {"action": action})
        except _RawHttpError as exc:
            if action == "start" and exc.code == 400 and "free gpu" in exc.detail.lower():
                # The pod's current host has no free GPU; a fresh pod may be
                # placed on a different host that does.
                raise PlacementError(
                    f"RunPod has no free GPU on the pod's host (HTTP 400): {exc.detail}"
                ) from exc
            raise self._map_action_error(exc) from exc
        if data:
            return parse_pod(data)
        if action == "terminate":
            # terminate returns 204 (empty body) on success; the pod is already
            # gone, so re-fetching via get_pod would 404. Return a synthetic
            # acknowledgement pod instead.
            return Pod(id=pod_id, name="", status=STATUS_TERMINATED)
        return self.get_pod(pod_id)

    @staticmethod
    def _map_action_error(exc: _RawHttpError) -> RunPodError:
        if exc.code == 404:
            return PodNotFoundError(f"Pod not found (HTTP 404): {exc.detail}")
        if exc.code == 409:
            return InvalidActionError(
                f"Action not valid for current pod status (HTTP 409): {exc.detail}"
            )
        return ApiError(f"RunPod API HTTP {exc.code}: {exc.detail}")

    def start_pod(self, pod_id: str) -> Pod:
        """Start (or resume) a stopped pod."""
        return self._action(pod_id, "start")

    def stop_pod(self, pod_id: str) -> Pod:
        """Stop a running pod, releasing GPU compute but keeping its disk."""
        return self._action(pod_id, "stop")

    def terminate_pod(self, pod_id: str) -> Pod:
        """Terminate a pod, releasing its disk (destructive, irreversible)."""
        return self._action(pod_id, "terminate")

    def update_pod_env(
        self,
        pod_id: str,
        env: Mapping[str, str],
        *,
        replace_all: bool = False,
    ) -> Pod:
        """Update a pod's environment variables (read-then-full-replace).

        RunPod's pod-update endpoint treats ``env`` as a **full replacement**:
        any key omitted from the body is deleted from the pod. To avoid the
        documented ``PUBLIC_KEY`` lockout (see
        ``docs/runpod-patch-env-gotcha.md``), this method first fetches the
        pod's current env, then PATCHes the complete object back.

        By default *env* is applied as per-key overrides over the current env.
        ``replace_all=True`` sends exactly *env* as the whole object (caller
        asserts it is complete).

        Returns the updated :class:`Pod`. A no-op (nothing changes) returns
        the freshly-fetched pod without a PATCH call.
        """
        current = self.get_pod(pod_id)
        base = dict(current.env or {})
        merged = dict(env) if replace_all else {**base, **dict(env)}
        if merged == base:
            return current
        data = self._request("PATCH", f"/pods/{pod_id}", {"env": merged})
        if data:
            return parse_pod(data)
        return self.get_pod(pod_id)

    def _map_create_error(self, exc: _RawHttpError) -> RunPodError:
        if exc.code == 404:
            return TemplateNotFoundError(
                f"Template not found (HTTP 404): {exc.detail}"
            )
        if exc.code == 422:
            return ContractError(
                f"RunPod rejected the pod request (HTTP 422): {exc.detail}",
                errors=_parse_contract_errors(exc.detail),
            )
        if exc.code == 400:
            return PlacementError(
                f"RunPod cannot place the pod (HTTP 400): {exc.detail}"
            )
        if exc.code == 402:
            return InsufficientFundsError(
                f"Insufficient funds (HTTP 402): {exc.detail}"
            )
        if exc.code == 403:
            return AccessDeniedError(f"Access denied (HTTP 403): {exc.detail}")
        if exc.code == 429:
            return RateLimitedError(
                f"Rate limited (HTTP 429): {exc.detail}",
                retry_after=_parse_retry_after(exc.headers),
            )
        return ApiError(f"RunPod API HTTP {exc.code}: {exc.detail}")

    def create_pod(
        self,
        name: str,
        *,
        template_id: str,
        gpu_id: str,
        gpu_count: int = 1,
        data_center_ids=(),
        env: Optional[Mapping[str, str]] = None,
    ) -> Pod:
        """Create a pod from a private template and return it (PROVISIONING).

        Retries transient failures with bounded backoff: HTTP 429 waits
        ``max(Retry-After, 5s)`` and HTTP 5xx waits 5s/10s, for at most three
        total attempts. Connection-level failures (TLS reset, EOF, timeout)
        are retried with the same 5s/10s backoff. Other 4xx responses fail
        immediately and are mapped to a specific :class:`RunPodError`
        subclass.

        ``env`` is passed through as per-pod environment overrides (e.g.
        MAX_MODEL_LEN). Cloud-type/public-IP are not sent here: the v2 REST
        endpoint does not accept them (placement defaults are the scheduler's).
        """
        body: dict[str, Any] = {
            "name": name,
            "templateId": template_id,
            "gpu": {"id": gpu_id, "count": int(gpu_count)},
            "startSsh": True,
            "env": dict(env or {}),
        }
        dc_ids = [dc for dc in data_center_ids if dc]
        if dc_ids:
            body["dataCenterIds"] = dc_ids

        last_error: Optional[_RawHttpError] = None
        last_network_error: Optional[TransientNetworkError] = None
        for attempt in range(_CREATE_MAX_ATTEMPTS):
            try:
                data = self._request_raw("POST", "/pods", body)
                return parse_pod(data)
            except _RawHttpError as exc:
                last_error = exc
                retriable = exc.code == 429 or 500 <= exc.code < 600
                if not retriable or attempt == _CREATE_MAX_ATTEMPTS - 1:
                    break
                if exc.code == 429:
                    wait = _parse_retry_after(exc.headers) or 0.0
                    self._sleep_fn(max(wait, _RATE_LIMIT_MIN_WAIT))
                else:
                    self._sleep_fn(
                        _CREATE_RETRY_BACKOFF[
                            min(attempt, len(_CREATE_RETRY_BACKOFF) - 1)
                        ]
                    )
            except TransientNetworkError as exc:
                last_network_error = exc
                if attempt == _CREATE_MAX_ATTEMPTS - 1:
                    break
                logger.warning(
                    "RunPod API transiently unreachable; retrying in %.0fs (%s)",
                    _CREATE_RETRY_BACKOFF[min(attempt, len(_CREATE_RETRY_BACKOFF) - 1)],
                    exc,
                )
                self._sleep_fn(
                    _CREATE_RETRY_BACKOFF[min(attempt, len(_CREATE_RETRY_BACKOFF) - 1)]
                )

        if last_error is not None and not (
            last_error.code == 429 or 500 <= last_error.code < 600
        ):
            # A deterministic 4xx is reported as itself even when an earlier
            # attempt failed on the network. Checking ``last_network_error``
            # first made a [network error, then HTTP 400/402/403/422] sequence
            # read as "API unavailable (network error)" — i.e. as retriable,
            # when it can never succeed (no capacity, no funds, bad template).
            raise self._map_create_error(last_error)
        if last_network_error is not None:
            raise TransientApiError(
                f"RunPod API unavailable after {_CREATE_MAX_ATTEMPTS} attempts "
                f"(network error): {last_network_error}"
            ) from last_network_error
        if last_error is not None:
            if last_error.code == 429:
                raise self._map_create_error(last_error)
            if 500 <= last_error.code < 600:
                raise TransientApiError(
                    f"RunPod API unavailable after {_CREATE_MAX_ATTEMPTS} "
                    f"attempts (HTTP {last_error.code}): {last_error.detail}"
                )
            raise self._map_create_error(last_error)
        raise ApiError("RunPod pod creation failed unexpectedly")

    def get_template(self, template_id: str) -> Mapping[str, Any]:
        """Return the template object for *template_id* (advisory check)."""
        try:
            return self._request_raw("GET", f"/templates/{template_id}")
        except _RawHttpError as exc:
            if exc.code == 404:
                raise TemplateNotFoundError(
                    f"Template not found (HTTP 404): {exc.detail}"
                ) from exc
            if exc.code == 403:
                raise AccessDeniedError(
                    f"Access denied (HTTP 403): {exc.detail}"
                ) from exc
            raise ApiError(f"RunPod API HTTP {exc.code}: {exc.detail}") from exc

    def list_pods(self) -> list[Pod]:
        """Return the caller's pods.

        Raises :class:`ApiError` when the payload cannot be understood. An
        unparsed list used to be reported as "no pods", and
        ``orchestrator.pod_presence`` treats an empty list as proof that a
        registered pod is gone — so an unrecognised envelope made the launcher
        clear the registry record of a pod that was alive and billing.
        """
        data = self._request("GET", "/pods")
        if isinstance(data, list):
            items = data
        elif isinstance(data, Mapping):
            items = None
            for key in ("pods", "items", "data"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    items = candidate
                    break
            if items is None:
                raise ApiError(
                    "RunPod pods listing has an unrecognised shape: "
                    f"keys {sorted(data)}"
                )
        else:
            raise ApiError(
                f"RunPod pods listing has an unrecognised shape: {type(data).__name__}"
            )
        return [parse_pod(item) for item in items if isinstance(item, Mapping)]

    def get_wallet(self) -> Wallet:
        """Return the account wallet (credits balance, spend/hour, limit).

        The balance is not exposed by the REST v2 API; this uses the same
        GraphQL ``myself`` query as ``runpodctl user`` (``POST /graphql`` on
        the API host). Raises :class:`RunPodError` on transport or GraphQL
        errors; null fields degrade to ``None`` in the returned :class:`Wallet`.
        """
        body = json.dumps({"query": _WALLET_QUERY}).encode("utf-8")
        req = urllib.request.Request(
            self._graphql_url(),
            data=body,
            headers=self._headers(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ApiError(f"RunPod GraphQL HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TransientNetworkError(
                f"RunPod GraphQL network error: {exc.reason}"
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ApiError("RunPod GraphQL returned invalid JSON") from exc
        errors = payload.get("errors") or []
        if errors and isinstance(errors[0], Mapping):
            message = errors[0].get("message") or "unknown GraphQL error"
            raise ApiError(f"RunPod GraphQL: {message}")
        me = (payload.get("data") or {}).get("myself")
        if not isinstance(me, Mapping):
            me = {}

        def _num(key: str) -> Optional[float]:
            value = me.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            return None

        email = me.get("email")
        return Wallet(
            balance=_num("clientBalance"),
            spend_per_hour=_num("currentSpendPerHr"),
            spend_limit=_num("spendLimit"),
            email=email if isinstance(email, str) else None,
        )

    def _graphql_url(self) -> str:
        """GraphQL endpoint URL derived from the REST base (``…/v2`` -> ``…/graphql``)."""
        base = self._base_url
        if base.endswith("/v2"):
            base = base[: -len("/v2")]
        return base + "/graphql"

    def list_ssh_keys(self) -> list[dict[str, Any]]:
        """Return the SSH keys registered to the account."""
        data = self._request("GET", "/account/ssh-keys")
        if isinstance(data, list):
            return list(data)
        if isinstance(data, Mapping):
            for key in ("sshKeys", "keys", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return list(value)
        return []

    def get_gpu_types(
        self,
        include_availability: bool = True,
        product: str = "POD",
        cloud: Optional[str] = None,
        count: Optional[int] = None,
    ) -> list[Any]:
        """Return the GPU catalog (availability is advisory, not a reservation)."""
        params: list[tuple[str, str]] = []
        if include_availability:
            params.append(("include", "AVAILABILITY"))
        if product:
            params.append(("product", product))
        if cloud:
            params.append(("cloud", cloud))
        if count is not None:
            params.append(("count", str(count)))
        query = urllib.parse.urlencode(params)
        path = f"/catalog/gpus?{query}" if query else "/catalog/gpus"
        data = self._request("GET", path)
        if isinstance(data, list):
            return list(data)
        if isinstance(data, Mapping):
            for key in ("gpus", "items", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return list(value)
        return []

    def wait_ready(
        self,
        pod_id: str,
        timeout: float = 600.0,
        interval: float = 10.0,
    ) -> Pod:
        """Poll until *pod_id* reaches ``RUNNING`` or *timeout* elapses.

        Transient network failures (TLS reset, connection EOF, DNS blips)
        are tolerated: the poll is skipped and the loop continues until the
        deadline. If the API stays unreachable, the last
        :class:`TransientNetworkError` is re-raised at the deadline.

        Raises :class:`RunPodError` if the pod enters ``ERROR``/``EXITED`` or
        the timeout expires before the pod is running.
        """
        deadline = time.monotonic() + timeout
        last: Optional[Pod] = None
        last_network_error: Optional[TransientNetworkError] = None
        while True:
            if time.monotonic() >= deadline:
                if last_network_error is not None:
                    raise TransientNetworkError(
                        f"Pod {pod_id} not reachable for {timeout:.0f}s "
                        f"(last error: {last_network_error})"
                    ) from last_network_error
                suffix = f" (last status: {last.status})" if last is not None else ""
                raise RunPodError(
                    f"Pod {pod_id} did not become RUNNING within {timeout:.0f}s{suffix}"
                )
            try:
                pod = self.get_pod(pod_id)
            except TransientNetworkError as exc:
                logger.warning(
                    "RunPod API transiently unreachable; retrying in %.0fs (%s)",
                    interval,
                    exc,
                )
                last_network_error = exc
                time.sleep(interval)
                continue
            last = pod
            if pod.status == STATUS_RUNNING:
                return pod
            if pod.status in _TERMINAL_STATUSES:
                raise RunPodError(f"Pod {pod_id} entered {pod.status} state")
            time.sleep(interval)

    def wait_ssh_endpoint(
        self,
        pod_id: str,
        timeout: float = 600.0,
        interval: float = 5.0,
    ) -> Pod:
        """Poll until *pod_id* has a direct SSH endpoint for ``-L`` forwarding.

        RunPod reports ``status: RUNNING`` before it assigns the public TCP
        mapping for ``22/tcp`` — in observed cases the mapping appears ~1-2
        minutes later, so ``ssh.direct`` and ``runtime.ports`` can still be
        null when ``wait_ready`` first returns. Poll ``get_pod`` until
        ``ssh_tunnel_endpoint()`` is populated (``ssh.direct`` non-null, or a
        ``runtime.ports`` entry with ``private=22``, ``type=tcp``, and a
        non-null ``public``), or *timeout* elapses.

        ``ssh.proxy`` is deliberately not a readiness signal: it supports an
        interactive shell only and can fail with "container not found" even
        when the pod is otherwise healthy.

        Transient network failures (TLS reset, connection EOF, DNS blips)
        are tolerated: the poll is skipped and the loop continues until the
        deadline. If the API stays unreachable, the last
        :class:`TransientNetworkError` is re-raised at the deadline.

        Raises :class:`RunPodError` if the pod never receives a mapping
        before the timeout — distinct from a generic "not RUNNING" failure.
        """
        deadline = time.monotonic() + timeout
        last_network_error: Optional[TransientNetworkError] = None
        while True:
            if time.monotonic() >= deadline:
                if last_network_error is not None:
                    raise TransientNetworkError(
                        f"Pod {pod_id} not reachable for {timeout:.0f}s "
                        f"(last error: {last_network_error})"
                    ) from last_network_error
                raise RunPodError(
                    f"Pod {pod_id} did not receive a public SSH (22/tcp) "
                    f"mapping within {timeout:.0f}s of becoming RUNNING. "
                    "RunPod assigns the mapping asynchronously after the pod "
                    "reports RUNNING; retry, or confirm the pod template "
                    "enables SSH over exposed TCP (SSH Terminal Access)."
                )
            try:
                pod = self.get_pod(pod_id)
            except TransientNetworkError as exc:
                logger.warning(
                    "RunPod API transiently unreachable; retrying in %.0fs (%s)",
                    interval,
                    exc,
                )
                last_network_error = exc
                time.sleep(interval)
                continue
            if pod.ssh_tunnel_endpoint() is not None:
                return pod
            if pod.status in _TERMINAL_STATUSES:
                raise RunPodError(f"Pod {pod_id} entered {pod.status} state")
            time.sleep(interval)

    def wait_public_port(
        self,
        pod_id: str,
        private_port: int,
        timeout: float = 600.0,
        interval: float = 5.0,
    ) -> Pod:
        """Poll until *pod_id* exposes *private_port* as a public mapping.

        RunPod publishes the public mapping for exposed HTTP ports
        (``runtime.ports`` with ``type=http``) asynchronously after the pod
        reports ``RUNNING`` — in observed cases anywhere from ~17s to a few
        minutes later. Poll ``get_pod`` until
        ``pod.public_url_for_port(private_port)`` is populated, or *timeout*
        elapses.

        Transient network failures (TLS reset, connection EOF, DNS blips)
        are tolerated: the poll is skipped and the loop continues until the
        deadline. If the API stays unreachable, the last
        :class:`TransientNetworkError` is re-raised at the deadline.

        Raises :class:`RunPodError` if the pod never receives a public
        mapping for the port before the timeout.
        """
        deadline = time.monotonic() + timeout
        last_network_error: Optional[TransientNetworkError] = None
        while True:
            if time.monotonic() >= deadline:
                if last_network_error is not None:
                    raise TransientNetworkError(
                        f"Pod {pod_id} not reachable for {timeout:.0f}s "
                        f"(last error: {last_network_error})"
                    ) from last_network_error
                raise RunPodError(
                    f"Pod {pod_id} did not receive a public mapping for port "
                    f"{private_port} within {timeout:.0f}s of becoming RUNNING. "
                    "RunPod publishes the mapping asynchronously after the pod "
                    "reports RUNNING; retry, or confirm the pod template "
                    "exposes the port as HTTP (e.g. '8188/http')."
                )
            try:
                pod = self.get_pod(pod_id)
            except TransientNetworkError as exc:
                logger.warning(
                    "RunPod API transiently unreachable; retrying in %.0fs (%s)",
                    interval,
                    exc,
                )
                last_network_error = exc
                time.sleep(interval)
                continue
            if pod.public_url_for_port(private_port) is not None:
                return pod
            if pod.status in _TERMINAL_STATUSES:
                raise RunPodError(f"Pod {pod_id} entered {pod.status} state")
            time.sleep(interval)
