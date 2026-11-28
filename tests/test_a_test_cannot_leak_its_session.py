# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A test that binds a session id does not leave it bound for the next test on its worker.

The first test binds one on purpose and never unbinds; the second (which pytest runs after it
in this file) must still see none. See the autouse fixture in the root conftest.py.
"""

from axiom.infra import trace


def test_a_binds_a_session_and_does_not_clean_up():
    trace.set_session("left-behind-by-a-test")
    assert trace.current_session() == "left-behind-by-a-test"


def test_b_sees_no_session():
    assert trace.current_session() == "anonymous"
