# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A key that may only read can ask the data door a question, and cannot submit a kit.

Every data question arrives as POST, which the gate reads as ``invoke``, so a
``viewer``'s ``*:read`` key (or a site's reviewer) was refused the one route that
exists for reading. Found when the first colleague signed in, made a self-service
key and every ``neut data`` read answered 403. The mount now declares its query
path a read; the one write it carried, a kit submission, has its own path that
stays ``invoke``.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from axiom.extensions.builtins.data_platform.read_mount import mount_spec
from axiom.extensions.builtins.http.authz_hook import (
    _covering_scope,
    default_envelope_builder,
)
from axiom.vega.identity.principal import Principal


def _intent_for(path: str, method: str = "POST"):
    spec = mount_spec()
    request = SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        state=SimpleNamespace(mount_extension=spec.extension, mount_spec=spec),
    )
    envelope = default_envelope_builder(request, Principal(handle="@reader:example-loop", public_bytes=hashlib.sha256(b"@reader").digest()))
    return envelope


def test_a_data_question_is_a_read_whatever_its_method():
    assert str(_intent_for("/data/query").intent) == "http.read"


def test_a_kit_submission_stays_a_write():
    assert str(_intent_for("/data/submit").intent) == "http.invoke"


def test_a_read_only_key_covers_the_question_and_not_the_submission():
    read_only = ("*:read",)
    q = _intent_for("/data/query")
    s = _intent_for("/data/submit")
    assert _covering_scope(read_only, q.intent, q.resource) is not None
    assert _covering_scope(read_only, s.intent, s.resource) is None
    assert _covering_scope(("data_platform:invoke",), s.intent, s.resource) is not None
