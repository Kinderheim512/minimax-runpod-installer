"""Tests for the Fizgig version check (`launcher.fizgig_version`).

No network: `fetch_releases` takes an injectable opener, and the pure helpers
are exercised directly. The module must never raise — a version check is
advisory and must not be able to break a GUI action.
"""

from __future__ import annotations

import io
import json

import pytest

from launcher import fizgig_version as fv


# --------------------------------------------------------------------------- #
# parse_version
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6.0.1", (6, 0, 1)),
        ("v6.0.1", (6, 0, 1)),
        ("6", (6,)),
        ("6.0", (6, 0)),
        ("6.0.1-rc1", (6, 0, 1)),
        ("  6.0.1  ", (6, 0, 1)),
        ("5.8.2", (5, 8, 2)),
    ],
)
def test_parse_version_accepts_common_shapes(text: str, expected: tuple[int, ...]) -> None:
    assert fv.parse_version(text) == expected


@pytest.mark.parametrize("text", ["", "master", "abc1234", "release-notes", None])
def test_parse_version_rejects_non_versions(text) -> None:
    assert fv.parse_version(text) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# parse_describe
# --------------------------------------------------------------------------- #

def test_parse_describe_exact_tag() -> None:
    pod = fv.parse_describe("6.0.1")
    assert pod.tag == "6.0.1"
    assert pod.commits_ahead == 0
    assert pod.version == (6, 0, 1)


def test_parse_describe_tag_plus_commits() -> None:
    pod = fv.parse_describe("6.0.1-3-gabc1234")
    assert pod.tag == "6.0.1"
    assert pod.commits_ahead == 3
    assert pod.version == (6, 0, 1)


def test_parse_describe_bare_sha_has_no_version() -> None:
    pod = fv.parse_describe("abc1234")
    assert pod.tag is None
    assert pod.version is None
    assert pod.render() == "abc1234"


# --------------------------------------------------------------------------- #
# compare_versions
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ((6, 0, 1), (6, 0, 1), 0),
        ((6, 0), (6, 0, 0), 0),
        ((6, 0, 0), (6, 0), 0),
        ((6, 0, 1), (6, 0, 2), -1),
        ((6, 1, 0), (6, 0, 9), 1),
        ((7,), (6, 9, 9), 1),
        ((5, 8, 1), (6, 0, 1), -1),
    ],
)
def test_compare_versions(left, right, expected: int) -> None:
    assert fv.compare_versions(left, right) == expected


# --------------------------------------------------------------------------- #
# parse_releases
# --------------------------------------------------------------------------- #

def _release(tag: str, **extra) -> dict:
    entry = {"tag_name": tag, "name": tag, "published_at": "2026-09-17T00:00:00Z",
             "html_url": f"https://github.com/x/y/releases/tag/{tag}"}
    entry.update(extra)
    return entry


def test_parse_releases_sorts_newest_first() -> None:
    releases = fv.parse_releases([_release("5.8.1"), _release("6.0.1"), _release("6.0.0")])
    assert [r.tag for r in releases] == ["6.0.1", "6.0.0", "5.8.1"]


def test_parse_releases_skips_drafts() -> None:
    releases = fv.parse_releases([_release("6.0.2", draft=True), _release("6.0.1")])
    assert [r.tag for r in releases] == ["6.0.1"]


def test_parse_releases_skips_unparseable_tags() -> None:
    releases = fv.parse_releases([_release("nightly"), _release("6.0.1")])
    assert [r.tag for r in releases] == ["6.0.1"]


def test_parse_releases_tolerates_junk_entries() -> None:
    payload = ["not a dict", {}, _release("6.0.1"), {"tag_name": None}]
    assert [r.tag for r in fv.parse_releases(payload)] == ["6.0.1"]


def test_parse_releases_rejects_non_list() -> None:
    assert fv.parse_releases({"message": "Not Found"}) == []


def test_parse_releases_keeps_prereleases() -> None:
    # A release candidate is still worth surfacing when it is all there is;
    # only drafts are dropped outright.
    assert [r.tag for r in fv.parse_releases([_release("6.1.0-rc1")])] == ["6.1.0-rc1"]


def test_parse_releases_prefers_a_stable_release_over_a_prerelease() -> None:
    """A prerelease must not be reported as "the latest release".

    ``_VERSION_RE`` strips the ``-rcN`` suffix, so ``7.0.0-rc1`` parsed as
    (7,0,0) and the GUI told the user they were behind — offering a paid pod
    restart that changes nothing when the image follows ``FIZGIG_REF``.
    """
    payload = [
        _release("7.0.0-rc1", prerelease=True),
        _release("6.0.1"),
    ]
    assert [r.tag for r in fv.parse_releases(payload)] == ["6.0.1"]


def test_parse_releases_falls_back_to_prereleases_when_there_is_nothing_else() -> None:
    payload = [_release("6.1.0-rc2", prerelease=True), _release("6.1.0-rc1", prerelease=True)]
    assert [r.tag for r in fv.parse_releases(payload)] == ["6.1.0-rc2", "6.1.0-rc1"]


# --------------------------------------------------------------------------- #
# fetch_releases
# --------------------------------------------------------------------------- #

class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_releases_uses_the_injected_opener() -> None:
    body = json.dumps([_release("6.0.1"), _release("6.0.0")]).encode()
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["ua"] = request.get_header("User-agent")
        return _FakeResponse(body)

    releases = fv.fetch_releases(opener=opener)
    assert [r.tag for r in releases] == ["6.0.1", "6.0.0"]
    assert seen["url"] == fv.UPSTREAM_RELEASES_API
    assert seen["ua"] == fv.USER_AGENT


def test_fetch_releases_returns_empty_on_network_error() -> None:
    def opener(request, timeout):
        raise OSError("no route to host")

    assert fv.fetch_releases(opener=opener) == []


def test_fetch_releases_returns_empty_on_bad_json() -> None:
    def opener(request, timeout):
        return _FakeResponse(b"<html>rate limited</html>")

    assert fv.fetch_releases(opener=opener) == []


def test_fetch_releases_returns_empty_on_api_error_payload() -> None:
    def opener(request, timeout):
        return _FakeResponse(json.dumps({"message": "API rate limit exceeded"}).encode())

    assert fv.fetch_releases(opener=opener) == []


# --------------------------------------------------------------------------- #
# latest_release
# --------------------------------------------------------------------------- #

def test_latest_release_picks_the_highest_version() -> None:
    releases = fv.parse_releases([_release("5.8.1"), _release("6.0.0"), _release("6.0.1")])
    assert fv.latest_release(releases).tag == "6.0.1"


def test_latest_release_of_nothing_is_none() -> None:
    assert fv.latest_release([]) is None


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #

def _releases(*tags: str) -> list[fv.Release]:
    return fv.parse_releases([_release(t) for t in tags])


def test_check_up_to_date() -> None:
    result = fv.check("6.0.1", _releases("6.0.1", "6.0.0"))
    assert result.status is fv.Status.UP_TO_DATE
    assert not result.is_stale
    assert "6.0.1" in result.detail


def test_check_behind() -> None:
    result = fv.check("5.8.1", _releases("6.0.1", "5.8.1"))
    assert result.status is fv.Status.BEHIND
    assert result.is_stale
    assert "5.8.1" in result.detail and "6.0.1" in result.detail


def test_check_ahead_of_the_last_tag_is_not_an_error() -> None:
    # master has moved past the newest release — normal and healthy.
    result = fv.check("6.0.1-3-gabc1234", _releases("6.0.1"))
    assert result.status is fv.Status.AHEAD
    assert not result.is_stale


def test_check_unreadable_pod_version_is_unknown() -> None:
    result = fv.check("abc1234", _releases("6.0.1"))
    assert result.status is fv.Status.UNKNOWN
    assert not result.is_stale
    assert "abc1234" in result.detail


def test_check_without_releases_is_unknown() -> None:
    result = fv.check("6.0.1", [])
    assert result.status is fv.Status.UNKNOWN
    assert "network" in result.detail


def test_check_never_raises_on_garbage() -> None:
    for pod in ("", "???", "v", "-1"):
        fv.check(pod, _releases("6.0.1"))
