"""Which Fizgig the pod runs, and which one upstream has published.

The ``train`` stack runs the **upstream** Fizgig image
(``ghcr.io/shootthesound/fizgig``), not a fork of it. Upstream pulls Fizgig's
``master`` at every pod boot (``FIZGIG_REF``, default ``master``), so a pod is
current whenever it starts — and falls behind for as long as it keeps running.

Nothing in the launcher ever said so. This module answers two questions and
nothing else:

* what revision is the pod running — ``TrainOps.pod_fizgig_version()`` reads it
  off the pod with ``git describe``;
* what is the newest release upstream has published — one HTTPS ``GET`` here.

The comparison is **advisory**. Nothing in this module starts, stops, updates
or changes anything: with ``FIZGIG_REF=master`` the update *is* a pod restart,
which is the launcher's existing start/stop path. The point is to make the
drift visible so that restart is a decision rather than an accident.

Read-only, stdlib only, no credentials. Every failure path returns a value —
a version check must never be the reason a GUI action fails.
"""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence

#: Upstream's release feed. Public, no auth; unauthenticated GitHub API calls
#: are rate-limited per IP, which is why the caller is expected to cache the
#: result rather than poll.
UPSTREAM_RELEASES_API = "https://api.github.com/repos/shootthesound/Fizgig/releases"

#: Human-facing page, for the "see what changed" link.
UPSTREAM_RELEASES_PAGE = "https://github.com/shootthesound/Fizgig/releases"

#: GitHub rejects requests without one.
USER_AGENT = "minimax-launcher"

DEFAULT_TIMEOUT = 10.0

#: ``6.0.1``, ``v6.0.1``, ``6.0.1-rc1`` — the numeric core is what we compare.
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)")

#: ``git describe --tags`` on a tree ahead of its last tag prints
#: ``6.0.1-3-gabc1234``: nearest tag, commits since, abbreviated sha.
_DESCRIBE_RE = re.compile(r"^v?(\d+(?:\.\d+)*)-(\d+)-g[0-9a-f]+$")


class Status(str, Enum):
    """Outcome of a pod-vs-upstream comparison."""

    UP_TO_DATE = "up_to_date"
    BEHIND = "behind"
    AHEAD = "ahead"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Release:
    """One published upstream release."""

    tag: str
    name: str = ""
    published_at: str = ""
    url: str = ""

    @property
    def version(self) -> Optional[tuple[int, ...]]:
        return parse_version(self.tag)


@dataclass(frozen=True)
class PodVersion:
    """What the pod is running, as read from the pod itself."""

    #: The raw string ``git describe`` produced, e.g. ``6.0.1-3-gabc1234``.
    raw: str
    #: The nearest tag, when the raw string carries one.
    tag: Optional[str] = None
    #: Commits since that tag. ``None`` when the pod is exactly on the tag, or
    #: when the raw string was a bare sha.
    commits_ahead: Optional[int] = None

    @property
    def version(self) -> Optional[tuple[int, ...]]:
        return parse_version(self.tag) if self.tag else None

    def render(self) -> str:
        return self.raw or "(unknown)"


@dataclass(frozen=True)
class VersionCheck:
    """The comparison, ready to render."""

    status: Status
    pod: Optional[PodVersion]
    latest: Optional[Release]
    detail: str = ""

    @property
    def is_stale(self) -> bool:
        return self.status is Status.BEHIND


def parse_version(text: str) -> Optional[tuple[int, ...]]:
    """Numeric core of a version string, or None.

    ``v6.0.1`` -> ``(6, 0, 1)``; ``6.0.1-rc1`` -> ``(6, 0, 1)``; anything
    without a leading number -> None.
    """
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def parse_describe(text: str) -> PodVersion:
    """Interpret a ``git describe --tags --always`` string.

    Three shapes matter: a tag exactly (``6.0.1``), a tag plus commits
    (``6.0.1-3-gabc1234``), and a bare abbreviated sha for a tree with no tags
    in reach (``abc1234``).
    """
    raw = (text or "").strip()
    match = _DESCRIBE_RE.match(raw)
    if match:
        return PodVersion(raw=raw, tag=match.group(1), commits_ahead=int(match.group(2)))
    if parse_version(raw) is not None:
        return PodVersion(raw=raw, tag=raw, commits_ahead=0)
    return PodVersion(raw=raw)


def compare_versions(left: Sequence[int], right: Sequence[int]) -> int:
    """-1 / 0 / 1, zero-padding the shorter side so 6.0 == 6.0.0."""
    size = max(len(left), len(right))
    a = tuple(left) + (0,) * (size - len(left))
    b = tuple(right) + (0,) * (size - len(right))
    return (a > b) - (a < b)


def parse_releases(payload: object) -> list[Release]:
    """Turn GitHub's ``/releases`` payload into releases, newest first.

    Tolerant on purpose: GitHub returns a mixture of drafts, prereleases and
    entries with missing fields, and a malformed entry must not cost us the
    whole list.
    """
    if not isinstance(payload, list):
        return []
    releases: list[Release] = []
    prereleases: list[Release] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if entry.get("draft"):
            continue
        tag = entry.get("tag_name") or ""
        if not isinstance(tag, str) or parse_version(tag) is None:
            continue
        release = Release(
            tag=tag.strip(),
            name=str(entry.get("name") or "").strip(),
            published_at=str(entry.get("published_at") or "").strip(),
            url=str(entry.get("html_url") or "").strip(),
        )
        # A prerelease is NOT "the latest release": ``_VERSION_RE`` strips the
        # ``-rcN`` suffix, so ``7.0.0-rc1`` parsed as (7,0,0) and the GUI told
        # the user they were behind — and offered a paid pod restart that would
        # change nothing (the image follows ``FIZGIG_REF``). Prereleases are
        # kept only as a fallback for a repo with no stable release at all.
        if entry.get("prerelease"):
            prereleases.append(release)
        else:
            releases.append(release)
    releases = releases or prereleases
    releases.sort(key=lambda r: r.version or (), reverse=True)
    return releases


def fetch_releases(
    timeout: float = DEFAULT_TIMEOUT,
    opener: Optional[Callable[..., object]] = None,
) -> list[Release]:
    """Upstream releases, newest first. Empty list on any failure.

    *opener* exists so tests can supply a fake and never touch the network; it
    is called with ``(url, timeout)`` and must return a file-like object.
    """
    request = urllib.request.Request(UPSTREAM_RELEASES_API)
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "application/vnd.github+json")
    try:
        if opener is None:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        else:
            with opener(request, timeout) as response:  # type: ignore[attr-defined]
                payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - a version check never breaks the caller
        return []
    return parse_releases(payload)


def latest_release(releases: Sequence[Release]) -> Optional[Release]:
    """The highest version in *releases*, or None."""
    best: Optional[Release] = None
    for release in releases:
        if release.version is None:
            continue
        if best is None or compare_versions(release.version, best.version or ()) > 0:
            best = release
    return best


def check(pod_raw: str, releases: Sequence[Release]) -> VersionCheck:
    """Compare what the pod runs against the newest published release.

    A pod sitting *ahead* of the last release is normal and healthy — it means
    ``master`` has moved past the newest tag — so it is reported as such rather
    than as an error.
    """
    pod = parse_describe(pod_raw)
    latest = latest_release(releases)

    if latest is None:
        return VersionCheck(
            Status.UNKNOWN, pod, None,
            "no upstream release readable (network or GitHub quota)",
        )
    if pod.version is None:
        return VersionCheck(
            Status.UNKNOWN, pod, latest,
            f"unreadable pod version ({pod.render()}) — latest release {latest.tag}",
        )

    order = compare_versions(pod.version, latest.version or ())
    if order > 0:
        return VersionCheck(
            Status.AHEAD, pod, latest,
            f"the pod ({pod.tag}) is ahead of the latest release ({latest.tag})",
        )
    if order < 0:
        return VersionCheck(
            Status.BEHIND, pod, latest,
            f"the pod runs {pod.tag}, the latest release is {latest.tag}",
        )
    if pod.commits_ahead:
        return VersionCheck(
            Status.AHEAD, pod, latest,
            f"the pod is on {latest.tag} + {pod.commits_ahead} master commit(s)",
        )
    return VersionCheck(Status.UP_TO_DATE, pod, latest, f"up to date ({latest.tag})")
