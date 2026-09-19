# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a person is shown when deciding whether to upgrade.

"An update is available" is not a reason to take one. The question a user is
actually answering is whether the new version fixes something that is biting
them, and that means showing what changed in words they can read.

The notes come from the release itself, so they describe the version being
offered rather than the one already installed — an installed package cannot
carry notes about its own successor.

Nothing here may raise: this runs on a CLI startup path, and a chat that cannot
start because a changelog fetch failed is a worse outcome than an unannounced
update.
"""

from __future__ import annotations

import json

from axiom.extensions.builtins.update.release_notes import (
    fetch_release_notes,
    format_update_notice,
)


def _fake_github(monkeypatch, payload, *, record=None):
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
            raise OSError("no network")
        return _Response(json.dumps(payload))

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


def test_it_reads_the_body_of_the_matching_release(monkeypatch):
    _fake_github(monkeypatch, {"body": "## What's Changed\n* Fixed the thing"})

    notes = fetch_release_notes("owner/repo", "1.2.3", timeout=1.0)

    assert "Fixed the thing" in notes


def test_it_asks_for_the_tag_of_the_offered_version(monkeypatch):
    seen: list[str] = []
    _fake_github(monkeypatch, {"body": "x"}, record=seen)

    fetch_release_notes("owner/repo", "1.2.3", timeout=1.0)

    assert seen and "v1.2.3" in seen[0]


def test_a_placeholder_body_counts_as_no_notes(monkeypatch):
    """Every release before this change carries exactly this string. Showing it
    is worse than showing nothing — it looks like the changelog and says
    nothing."""
    _fake_github(monkeypatch, {"body": "Automated release v1.2.3."})

    assert fetch_release_notes("owner/repo", "1.2.3", timeout=1.0) == ""


def test_an_unreachable_forge_is_not_an_error(monkeypatch):
    """Negative control: this runs at startup and must never raise."""
    _fake_github(monkeypatch, None)

    assert fetch_release_notes("owner/repo", "1.2.3", timeout=1.0) == ""


def test_no_repository_configured_returns_nothing(monkeypatch):
    assert fetch_release_notes("", "1.2.3", timeout=1.0) == ""


# --- the notice a person reads ---------------------------------------------


def test_the_notice_leads_with_both_versions():
    notice = format_update_notice(
        product="Neutron OS", current="1.4.1", available="1.11.1", notes=""
    )

    assert "1.4.1" in notice
    assert "1.11.1" in notice


def test_the_notice_includes_the_notes_when_there_are_some():
    notice = format_update_notice(
        product="Neutron OS",
        current="1.4.1",
        available="1.11.1",
        notes="* Monitors no longer report sent for alerts nobody received",
    )

    assert "nobody received" in notice


def test_markdown_noise_is_stripped_for_a_terminal():
    """`## What's Changed` and `* item` read as clutter in a CLI."""
    notice = format_update_notice(
        product="Neutron OS",
        current="1.0.0",
        available="1.1.0",
        notes="## What's Changed\n* Fixed a thing by @someone in #12\n",
    )

    assert "##" not in notice
    assert "Fixed a thing" in notice


def test_a_long_changelog_is_trimmed_rather_than_flooding_the_terminal():
    notes = "\n".join(f"* change number {i}" for i in range(200))

    notice = format_update_notice(
        product="Neutron OS", current="1.0.0", available="1.1.0", notes=notes
    )

    assert notice.count("\n") < 30
    assert "more" in notice.lower()


def test_without_notes_it_still_says_something_useful():
    """Negative control: no notes must not produce a blank or broken notice."""
    notice = format_update_notice(
        product="Neutron OS", current="1.0.0", available="1.1.0", notes=""
    )

    assert notice.strip()
    assert "1.1.0" in notice


def test_the_github_attribution_clause_is_removed_cleanly():
    """`by @user in #123` is one clause. Stripping the author and the number
    separately leaves a dangling "in" at the end of every line."""
    notice = format_update_notice(
        product="Neutron OS",
        current="1.0.0",
        available="1.1.0",
        notes="* A branding field must not kill the CLI by @bbooth in #215",
    )

    line = [ln for ln in notice.splitlines() if "branding" in ln][0]

    assert line.rstrip().endswith("CLI"), line
    assert "@bbooth" not in line
    assert "#215" not in line
