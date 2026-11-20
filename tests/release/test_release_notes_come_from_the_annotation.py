# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The release notes a user reads must be notes somebody WROTE.

v0.62.0 is why this file exists. `publish.yml` read the tag annotation with
`git tag -l --format='%(contents)'`, which is correct only if the local ref
is an annotated tag object. `actions/checkout` leaves a LIGHTWEIGHT ref for
the tag it checks out, and on a lightweight tag `%(contents)` silently falls
through to the pointed-to COMMIT's message. So the public mirror published
the squash commit body, `Co-authored-by` trailer and internal PR numbers
included, while the step's own log claimed it was publishing written notes.

That is the failure this repo cares about most: not a step that breaks, but
a step that reads the wrong source and reports success. Nothing went red.

Two things are tested, because fixing only one leaves the hole open:

1. The git behaviour itself (`test_lightweight_*`), run against real git, so
   the premise is pinned rather than asserted from memory. If a future git
   changes what `%(contents)` does on a lightweight tag, this tells us.
2. The workflow text, because the workflow is the thing that ships and no
   unit test executes it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "publish.yml"


def _git(*args: str, cwd: Path) -> str:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git("init", "-q", "-b", "main", ".", cwd=tmp_path)
    (tmp_path / "f").write_text("x", encoding="utf-8")
    _git("add", "f", cwd=tmp_path)
    _git("commit", "-q", "-m", "COMMIT SUBJECT\n\nCOMMIT BODY, internal.", cwd=tmp_path)
    return tmp_path


def test_lightweight_tag_contents_is_really_the_commit_message(repo: Path) -> None:
    """The trap, pinned. This is what shipped in v0.62.0."""
    _git("tag", "v1.0.0", cwd=repo)
    got = _git("tag", "-l", "--format=%(contents)", "v1.0.0", cwd=repo)
    assert "COMMIT SUBJECT" in got
    assert "COMMIT BODY, internal." in got


def test_lightweight_tag_is_detectable_as_not_a_tag_object(repo: Path) -> None:
    """The check the workflow now makes, and the reason it works."""
    _git("tag", "v1.0.0", cwd=repo)
    assert _git("cat-file", "-t", "refs/tags/v1.0.0", cwd=repo).strip() == "commit"


def test_annotated_tag_body_is_the_annotation_without_the_subject(repo: Path) -> None:
    _git("tag", "-a", "v1.0.0", "-m", "v1.0.0\n\nWRITTEN NOTES.", cwd=repo)
    assert _git("cat-file", "-t", "refs/tags/v1.0.0", cwd=repo).strip() == "tag"
    body = _git("for-each-ref", "refs/tags/v1.0.0", "--format=%(contents:body)", cwd=repo)
    assert "WRITTEN NOTES." in body
    # The subject is the tag name and is already the release title.
    assert "v1.0.0" not in body
    # And none of the commit message leaks in.
    assert "COMMIT" not in body


def _release_job() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"]["create_github_release"]


def _steps_text() -> str:
    return "\n".join(str(s.get("run", "")) for s in _release_job()["steps"])


def test_workflow_never_reads_contents_without_the_body_suffix() -> None:
    """`%(contents)` is the bug. Only `%(contents:body)` may appear."""
    text = _steps_text()
    assert "%(contents)" not in text, (
        "publish.yml reads %(contents), which returns the COMMIT message when "
        "the local tag ref is lightweight — the v0.62.0 leak. Use "
        "%(contents:body) on a ref proven to be a tag object."
    )


def test_workflow_proves_the_ref_is_a_tag_object_before_trusting_it() -> None:
    text = _steps_text()
    assert "cat-file -t" in text, "nothing proves the ref is an annotated tag"
    assert "refs/tags/$TAG:refs/tags/$TAG" in text, (
        "the annotated tag object is never fetched, so cat-file can only ever "
        "see the lightweight ref actions/checkout left behind"
    )


def test_the_mirror_step_publishes_only_written_notes() -> None:
    """The promise the mirror step makes to the public repo."""
    steps = _release_job()["steps"]
    mirror = [s for s in steps if "Mirror" in str(s.get("name", ""))]
    assert mirror, "the mirror step is gone"
    run = str(mirror[0]["run"])
    assert "--generate-notes" not in run, (
        "auto-generated notes name internal commits, partners and people; they "
        "must never reach the public mirror"
    )
    assert "ANNOTATED:-0" in run, "the mirror step does not check for an annotation"
    assert "--notes-file" in run
