# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``no_action_without_authz`` static-analysis lint
(PRD §5.6). Each test feeds a synthetic snippet into ``check_source``
and asserts on the violations/allowlisted lists."""

from __future__ import annotations

from axiom.extensions.builtins.authz.lint import check_source


def test_clean_function_passes():
    src = '''
def handle(envelope: ActionEnvelope) -> None:
    """A clean function consults decide first."""
    verdict = decide(envelope)
    if verdict.next_action_for_caller == "proceed":
        do_the_work()
'''
    r = check_source(src)
    assert r.ok
    assert r.violations == []
    assert r.checked_functions == 1


def test_missing_decide_call_is_violation():
    src = '''
def handle(envelope: ActionEnvelope) -> None:
    do_the_work()
'''
    r = check_source(src)
    assert not r.ok
    assert len(r.violations) == 1
    assert r.violations[0].function == "handle"


def test_function_without_envelope_param_is_skipped():
    src = '''
def helper(name: str) -> int:
    return len(name)
'''
    r = check_source(src)
    assert r.ok
    # checked_functions counts every public function regardless of params,
    # but only envelope-takers can violate.
    assert r.checked_functions == 1


def test_private_function_is_skipped():
    src = '''
def _internal(envelope: ActionEnvelope) -> None:
    # Private; PRD says public functions only.
    do_the_work()
'''
    r = check_source(src)
    assert r.ok  # no violations
    assert r.checked_functions == 0  # underscore-prefix excluded


def test_docstring_before_decide_is_ok():
    src = '''
def handle(envelope: ActionEnvelope) -> None:
    """Docstring is fine before decide."""
    decide(envelope)
'''
    r = check_source(src)
    assert r.ok


def test_imports_before_decide_is_ok():
    src = '''
def handle(envelope: ActionEnvelope) -> None:
    import json
    from x import y
    decide(envelope)
'''
    r = check_source(src)
    assert r.ok


def test_call_before_decide_is_violation():
    src = '''
def handle(envelope: ActionEnvelope) -> None:
    side_effect()
    decide(envelope)
'''
    r = check_source(src)
    assert not r.ok


def test_simple_data_assign_before_decide_is_ok():
    src = '''
def handle(envelope: ActionEnvelope) -> None:
    intent = envelope.intent
    decide(envelope)
'''
    r = check_source(src)
    assert r.ok


def test_function_call_in_assign_before_decide_is_violation():
    src = '''
def handle(envelope: ActionEnvelope) -> None:
    x = some_side_effecting_call()
    decide(envelope)
'''
    r = check_source(src)
    assert not r.ok


def test_module_call_to_decide_recognized():
    src = '''
def handle(envelope: ActionEnvelope) -> None:
    verdict = authz.decide(envelope)
    do_the_work()
'''
    r = check_source(src)
    assert r.ok


def test_noqa_marker_moves_to_allowlist():
    src = '''
def handle(envelope: ActionEnvelope) -> None:
    # noqa: no-action-without-authz — synthetic envelope at boot
    do_the_work()
'''
    r = check_source(src)
    assert r.ok  # allowlisted means no violations
    assert len(r.allowlisted) == 1
    assert r.allowlisted[0].function == "handle"


def test_envelope_alias_recognized():
    src = '''
def handle(env: Envelope) -> None:
    decide(env)
'''
    r = check_source(src)
    assert r.ok


def test_async_function_supported():
    src = '''
async def handle(envelope: ActionEnvelope) -> None:
    await something()
'''
    r = check_source(src)
    assert not r.ok


def test_async_function_clean_passes():
    src = '''
async def handle(envelope: ActionEnvelope) -> None:
    decide(envelope)
    await do_work()
'''
    r = check_source(src)
    assert r.ok


def test_optional_envelope_annotation_recognized():
    src = '''
def handle(envelope: ActionEnvelope | None) -> None:
    do_the_work()
'''
    r = check_source(src)
    assert not r.ok  # still must consult


def test_syntax_error_file_is_silently_skipped():
    r = check_source("def broken(:\n")
    assert r.ok
    assert r.checked_files == 1
    assert r.checked_functions == 0
