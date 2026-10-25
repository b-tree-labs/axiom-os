# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The upgrade question shows the official notes of every release it would bring.

It showed nothing in practice: the notes are the GitHub Releases of private
repositories, fetched anonymously (refused), from a repository named only by
an environment variable nobody set, and for the newest release only. A person
three releases behind is deciding about all three, so they are shown all
three, newest first, from the official release notes, with a link to the full
notes when they cannot be read or are too long to show.
"""

from __future__ import annotations

from axiom.extensions.builtins.update import release_notes as rn

RELEASES = [
    {
        "tag_name": "v1.4.0",
        "body": "* Faster sync by @a in https://x/pull/9",
        "html_url": "https://gh/r/v1.4.0",
    },
    {
        "tag_name": "v1.3.0",
        "body": "* New review verbs by @a in https://x/pull/8\n* Keys from the vault by @a in https://x/pull/7",
        "html_url": "u",
    },
    {"tag_name": "v1.2.0", "body": "* Fix A by @a in https://x/pull/6", "html_url": "u"},
    {
        "tag_name": "v1.1.0",
        "body": "* Already installed by @a in https://x/pull/5",
        "html_url": "u",
    },
    {"tag_name": "v1.5.0rc1", "body": "* not yet released", "html_url": "u"},
]


def test_every_release_between_installed_and_offered_newest_first():
    got = rn.releases_between(RELEASES, current="1.1.0", available="1.4.0")
    assert [r["version"] for r in got] == ["1.4.0", "1.3.0", "1.2.0"]


def test_the_notice_lists_each_release_under_its_version():
    got = rn.releases_between(RELEASES, current="1.1.0", available="1.4.0")
    text = rn.format_update_notice_for(
        product="P", current="1.1.0", available="1.4.0", releases=got, repo="o/r"
    )
    assert "3 releases" in text
    assert text.index("1.4.0") < text.index("1.3.0") < text.index("1.2.0")
    assert "• New review verbs" in text and "by @a" not in text
    assert "Already installed" not in text


def test_a_long_gap_is_capped_with_a_count_and_the_link_to_all_notes():
    many = [
        {
            "tag_name": f"v1.{i}.0",
            "body": "\n".join(f"* change {i}.{j}" for j in range(10)),
            "html_url": "u",
        }
        for i in range(2, 12)
    ]
    got = rn.releases_between(many, current="1.1.0", available="1.11.0")
    text = rn.format_update_notice_for(
        product="P", current="1.1.0", available="1.11.0", releases=got, repo="o/r"
    )
    assert len(text.splitlines()) <= 45
    assert "more changes" in text and "https://github.com/o/r/releases" in text


def test_no_notes_readable_still_links_the_official_notes():
    text = rn.format_update_notice_for(
        product="P", current="1.1.0", available="1.4.0", releases=[], repo="o/r"
    )
    assert "https://github.com/o/r/releases" in text


def test_the_repository_comes_from_the_package_itself():
    meta = ["Homepage, https://example.org", "Repository, https://github.com/Owner/Name"]
    assert rn.repo_from_project_urls(meta) == "Owner/Name"
    assert (
        rn.repo_from_project_urls(["Homepage, https://github.com/Owner/Name.git"]) == "Owner/Name"
    )
    assert rn.repo_from_project_urls(["Docs, https://example.org"]) == ""


def test_a_private_repository_is_read_with_the_persons_own_github_login():
    seen = []

    def getter(url, token):
        seen.append(token)
        if token is None:
            raise PermissionError("404")
        return RELEASES

    got = rn.fetch_releases("o/r", getter=getter, token_source=lambda: "t0k")
    assert got == RELEASES and seen == [None, "t0k"]


def test_without_a_login_a_private_repository_yields_nothing_not_an_error():
    def getter(url, token):
        raise PermissionError("404")

    assert rn.fetch_releases("o/r", getter=getter, token_source=lambda: None) == []


def test_identifiers_keep_their_underscores_and_emphasis_is_dropped():
    lines = rn._readable(
        "* fix: keys scoped to **triga_telemetry** keep `working` by @a in https://x/1"
    )
    assert lines == ["• fix: keys scoped to triga_telemetry keep working"]


def test_version_bumps_are_not_shown_as_changes():
    assert rn._readable("* bump: v1.15.0 by @a in https://x/2\n* real change") == ["• real change"]
