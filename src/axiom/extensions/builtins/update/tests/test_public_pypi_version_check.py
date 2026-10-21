# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An install from public PyPI has to be able to notice a newer release.

The update nudge never fired for anyone installing the normal way.
``_check_pypi_registry`` returns immediately unless ``NEUT_REGISTRY_URL`` and a
token are set — it was written for a private GitLab index — and the fallback
``_check_github_mirror`` points at ``example-org/example-consumer``, a
placeholder. Public PyPI, the only place most installs come from, was never
consulted. Confirmed live: ``available=None, is_newer=False`` while a newer
version sat on PyPI.

The user-visible shape of that: an adopter sits on a stale version until a
human tells them, which is exactly what happened — twice in one day, once for a
release that could not start at all.

A private index still wins where one is configured; this is the fallback for
everyone else.

The package name here is a placeholder: the platform never names a consumer,
and the checker asks about whatever `BrandingConfig.package_name` says.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.update.version_check import VersionChecker


@pytest.fixture
def checker(tmp_path, monkeypatch):
    """A checker whose cache is its own.

    The cache path comes from the project state directory, not from
    ``repo_root``: without this the cache is shared, one test's result is read
    by the next (a private-index result was served to the PyPI test), and the
    suite writes into the developer's real state directory.
    """
    from axiom.extensions.builtins.update import version_check as module

    monkeypatch.setattr(module, "state_dir", lambda: tmp_path / ".neut")
    monkeypatch.setattr(
        module, "update_state_file", lambda: tmp_path / ".neut" / "update-state.json"
    )
    return VersionChecker(repo_root=tmp_path)


def _fake_pypi(monkeypatch, payload, *, record=None):
    """Stand in for the PyPI JSON endpoint."""
    import urllib.request

    class _Response:
        def __init__(self, body):
            self._body = body.encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(req, timeout=None):
        if record is not None:
            record.append(getattr(req, "full_url", req))
        if payload is None:
            raise OSError("network down")
        return _Response(json.dumps(payload))

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


def test_it_reads_the_latest_version_from_public_pypi(checker, monkeypatch):
    _fake_pypi(monkeypatch, {"info": {"version": "9.9.9"}})

    assert checker._check_public_pypi("example-consumer", timeout=1.0) == "9.9.9"


def test_it_asks_about_the_branded_package(checker, monkeypatch):
    """A consumer product must not be told about the platform's version."""
    seen: list[str] = []
    _fake_pypi(monkeypatch, {"info": {"version": "9.9.9"}}, record=seen)

    checker._check_public_pypi("example-consumer", timeout=1.0)

    assert seen and "example-consumer" in seen[0]


def test_a_network_failure_is_not_an_update(checker, monkeypatch):
    """Negative control: unreachable must not read as 'you are current', nor
    raise into a CLI startup path."""
    _fake_pypi(monkeypatch, None)

    assert checker._check_public_pypi("example-consumer", timeout=1.0) is None


def test_a_junk_payload_is_not_a_version(checker, monkeypatch):
    _fake_pypi(monkeypatch, {"info": {}})

    assert checker._check_public_pypi("example-consumer", timeout=1.0) is None


def test_the_check_reports_a_newer_version_end_to_end(checker, monkeypatch):
    """The whole point: installed < PyPI must come back as is_newer."""
    monkeypatch.setattr(checker, "get_current_version", lambda: "1.0.0")
    monkeypatch.setattr(checker, "_check_pypi_registry", lambda timeout: None)
    _fake_pypi(monkeypatch, {"info": {"version": "1.1.0"}})

    info = checker.check_remote_version(timeout=1.0)

    assert info.available == "1.1.0"
    assert info.is_newer is True
    assert info.source == "pypi"


def test_an_equal_version_is_not_newer(checker, monkeypatch):
    """Negative control: it must not nag someone who is already current."""
    monkeypatch.setattr(checker, "get_current_version", lambda: "1.1.0")
    monkeypatch.setattr(checker, "_check_pypi_registry", lambda timeout: None)
    _fake_pypi(monkeypatch, {"info": {"version": "1.1.0"}})

    info = checker.check_remote_version(timeout=1.0)

    assert info.is_newer is False


def test_a_configured_private_index_still_wins(checker, monkeypatch):
    """Deployments on a private index must not be redirected to public PyPI."""
    monkeypatch.setattr(checker, "get_current_version", lambda: "1.0.0")
    monkeypatch.setattr(checker, "_check_pypi_registry", lambda timeout: "2.0.0")
    _fake_pypi(monkeypatch, {"info": {"version": "1.1.0"}})

    info = checker.check_remote_version(timeout=1.0)

    assert info.available == "2.0.0"


def test_the_github_placeholder_is_never_contacted(checker):
    """The old fallback pointed at example-org/example-consumer. Asking a
    placeholder repo about releases is not a check, it is a no-op that looks
    like one."""
    import inspect

    source = inspect.getsource(VersionChecker._check_github_mirror)

    assert "example-org/example-consumer" not in source
