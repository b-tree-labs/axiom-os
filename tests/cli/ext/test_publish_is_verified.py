# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

""""Published" should mean a consumer can resolve it, not that put() returned.

`publish_extension` registers the artifact and then announces "Published <name>
<version>" on the strength of the call having returned. Nothing confirms the
extension is resolvable afterwards — that the index carries the record, and
that the artifact is where the record says it is.

Both can be false while `put()` succeeds: an index written but not flushed, a
record pointing at a path that was cleaned up, a version string normalised on
write and not on read. The publisher sees success, the consumer sees nothing,
and the gap is found by whoever installs next.

`get()` is the consumer's own read path and it is one call. Using it turns "the
registry accepted this" into "a consumer can resolve this", which is what the
word published is taken to mean.
"""

from __future__ import annotations

from axiom.cli.ext.commands.publish import verify_published


class _Record:
    def __init__(self, artifact_path):
        self.artifact_path = artifact_path


def test_a_resolvable_extension_verifies(tmp_path):
    artifact = tmp_path / "ext-1.0.0.whl"
    artifact.write_bytes(b"x")

    result = verify_published("demo", "1.0.0", get=lambda n, v: _Record(artifact))

    assert result.verified is True


def test_an_extension_the_registry_cannot_resolve_is_not_verified():
    """put() returned, get() finds nothing — the case the announcement hid."""
    result = verify_published("demo", "1.0.0", get=lambda n, v: None)

    assert result.verified is False
    assert "resolve" in result.detail.lower()


def test_a_record_pointing_at_a_missing_artifact_is_not_verified(tmp_path):
    """The index can carry a record whose file is gone. A consumer resolving it
    then fails at install time rather than at publish time."""
    result = verify_published(
        "demo", "1.0.0", get=lambda n, v: _Record(tmp_path / "gone.whl")
    )

    assert result.verified is False
    assert "artifact" in result.detail.lower()


def test_a_registry_that_raises_is_unverified_not_failed():
    """"I could not check" is not the claim "it did not publish"."""

    def _boom(n, v):
        raise RuntimeError("registry unreadable")

    result = verify_published("demo", "1.0.0", get=_boom)

    assert result.verified is False
    assert result.checked is False


def test_a_successful_check_records_that_it_looked():
    """Negative control on `checked`: looked-and-absent must be distinguishable
    from never-looked, since only the first is evidence of absence."""
    result = verify_published("demo", "1.0.0", get=lambda n, v: None)

    assert result.checked is True
    assert result.verified is False


def test_verification_never_raises():
    def _explode(n, v):
        raise MemoryError("catastrophic")

    assert verify_published("demo", "1.0.0", get=_explode).verified is False
