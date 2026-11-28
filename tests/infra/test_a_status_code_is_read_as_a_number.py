# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A status code in an error is a number on its own, not three digits inside something else.

Several places decided what went wrong by asking whether ``"404"`` (or 401, 403,
429) appeared anywhere in an error's text. Error text carries addresses, commit
ids, run ids and issue numbers, and any of them can contain those digits. A
camera on port 54010 was told its login was refused (video, 2026-11-24). A branch
lookup for a commit whose id contains ``404`` reported "no such branch" for any
failure at all.

``mentions_status`` reads a code only where it stands as its own number, with
addresses removed first. Every place that matched the bare digits uses it.
"""

from __future__ import annotations

import pytest

from axiom.infra.text_utils import mentions_status

NOT_A_STATUS = [
    "rtsp://127.0.0.1:54040/t: Invalid data found when processing input",
    "https://api.github.com/repos/o/r/issues/404 timed out",
    "Connection to 10.4.0.4:8404 failed: Connection refused",
    "could not reach build-404.internal:8080",
    "gh: unknown revision 9f1c404ab27de6 (HTTP 422)",
    "run 45465404350 was cancelled",
    "see #404 for the fix",
    "pulls/404 is closed",
    "version 2.404.1 is not installed",
    "request id req_a404b",
    "",
]

IS_A_STATUS = [
    "HTTP 404: Not Found",
    "gh: Not Found (HTTP 404)",
    "404 Client Error: Not Found for url: https://api.example/items/7",
    "Server returned 404 Not Found",
    "status code: 404",
    "status=404",
    "status:404",
    '{"code": 404, "message": "nope"}',
    "error 404.",
    "404",
    "Error code: 404 - {'error': 'x'}",
    "[404] not found",
]


@pytest.mark.parametrize("text", NOT_A_STATUS)
def test_digits_inside_something_else_are_not_a_status(text):
    assert not mentions_status(text, 404)


@pytest.mark.parametrize("text", IS_A_STATUS)
def test_a_code_on_its_own_is_a_status(text):
    assert mentions_status(text, 404)
    assert mentions_status(text, "404")
    assert not mentions_status(text, 403)


def test_any_of_several_codes():
    assert mentions_status("HTTP 403: Forbidden", 401, 403)
    assert not mentions_status("HTTP 500", 401, 403)


def test_text_that_is_not_a_string_is_read_as_one():
    assert mentions_status(RuntimeError("HTTP 429 Too Many Requests"), 429)
    assert not mentions_status(None, 429)


# ---- the places that used to match the bare digits ---------------------------------


def test_a_branch_lookup_that_failed_is_not_a_missing_branch():
    from axiom.extensions.builtins.release.handoff import BranchLookupError, GhBranchSource

    def runner(args):
        raise RuntimeError("gh: compare 9f1c404ab27de6aa...rivet/fix-1 failed: Bad credentials (HTTP 401)")

    with pytest.raises(BranchLookupError):
        GhBranchSource(runner).get("o/r", "rivet/fix-1", "9f1c404ab27de6aa")


def test_a_branch_that_is_missing_is_still_missing():
    from axiom.extensions.builtins.release.handoff import GhBranchSource

    def runner(args):
        raise RuntimeError("gh: Not Found (HTTP 404)")

    assert GhBranchSource(runner).get("o/r", "rivet/fix-1", "9f1c404ab27de6aa") is None


def test_chat_does_not_call_a_refused_connection_a_rate_limit_or_a_bad_key():
    from axiom.extensions.builtins.chat.errors import friendly

    said = friendly(RuntimeError("upstream http://10.0.0.5:4290/v1 returned garbage"))
    assert "Rate limit" not in said and "API key" not in said
    said = friendly(RuntimeError("model at http://127.0.0.1:14010/v1 returned garbage"))
    assert "API key" not in said
    assert "Rate limit" in friendly(RuntimeError("Error code: 429 - slow down"))
    assert "API key rejected" in friendly(RuntimeError("Error code: 401 - nope"))


def test_a_tool_result_is_rate_limited_only_when_it_says_so():
    from axiom.extensions.builtins.chat.agent import _rate_limited

    assert not _rate_limited({"error": "run 45465429350 failed"})
    assert _rate_limited({"error": "HTTP 429"})
    assert _rate_limited({"errors": ["rate limit reached"]})


def test_a_calendar_error_is_classified_by_its_status_not_its_ids():
    from axiom.extensions.builtins.schedule.calendar.setup import _classify, _classify_m365

    assert _classify(RuntimeError("invalid_grant for key 5404031 (bad signature)")) == "auth"
    assert (
        _classify(RuntimeError("<HttpError 404 when requesting x returned Not Found>")) == "share"
    )
    assert _classify(RuntimeError("<HttpError 403 when requesting x>")) == "share"
    assert _classify_m365(RuntimeError("request 7403991 failed: socket closed")) == "unknown"
    assert _classify_m365(RuntimeError("Graph returned 403")) == "consent"
    assert _classify_m365(RuntimeError("Graph returned 404")) == "mailbox"
