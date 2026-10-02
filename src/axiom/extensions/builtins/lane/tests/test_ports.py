# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Port selection — the precedence ladder and the probe.

Ported from a working implementation rather than reasoned out, so the tests
pin the behaviours that made it work: an override that wins unconditionally, a
canonical pair that survives a restart, and a refusal rather than a silent
reuse when the band is full.
"""

from __future__ import annotations

import socket

import pytest

from axiom.extensions.builtins.lane import naming, ports


def _all_free(_p):
    return True


def _none_free(_p):
    return False


# --- precedence -------------------------------------------------------------


def test_an_explicit_override_wins_even_when_it_looks_busy():
    """Somebody naming a port has a reason. Let them have the bind error,
    which says more than a silent substitution would."""
    got = ports.resolve("chat", taken={9100, 9101}, override=(9100, 9101), probe=_none_free)

    assert got == (9100, 9101)


def test_a_free_canonical_pair_is_kept_so_a_bookmark_survives_a_restart():
    got = ports.resolve("chat", taken=set(), canonical=(8850, 8851), probe=_all_free)

    assert got == (8850, 8851)


def test_a_busy_canonical_pair_is_abandoned_rather_than_fought_over():
    got = ports.resolve("chat", taken={8850}, canonical=(8850, 8851), probe=_all_free)

    assert got != (8850, 8851)


def test_with_no_hints_it_starts_at_the_names_preferred_slot():
    got = ports.resolve("chat", taken=set(), probe=_all_free)

    assert got[0] == naming.preferred_port("chat")


def test_the_pair_is_adjacent_and_inside_the_band():
    front, api = ports.resolve("anything", taken=set(), probe=_all_free)

    assert api == front + 1
    assert naming.FIRST_LANE_PORT <= front <= naming.LAST_LANE_PORT


# --- the probe --------------------------------------------------------------


def test_a_port_nobody_listens_on_reads_free():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()  # nothing is listening now

    assert ports.is_free(free_port)


def test_a_port_with_a_listener_reads_taken():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        assert not ports.is_free(s.getsockname()[1])
    finally:
        s.close()


def test_the_registry_and_the_probe_are_both_consulted():
    """A port free in the registry can be occupied in reality, and the
    reverse. Either one saying busy is enough."""
    taken_only = ports.resolve("chat", taken={naming.preferred_port("chat")}, probe=_all_free)
    assert taken_only[0] != naming.preferred_port("chat")

    seen: list[int] = []

    def probe(p):
        seen.append(p)
        return p != naming.preferred_port("chat")

    probe_only = ports.resolve("chat", taken=set(), probe=probe)
    assert probe_only[0] != naming.preferred_port("chat")


# --- the full band ----------------------------------------------------------


def test_a_full_band_refuses_instead_of_reusing_the_base():
    """The source falls back to the base port so a single app still starts.
    For parallel lanes that hands two of them the same socket and the second
    loses silently, which is the whole failure being prevented."""
    with pytest.raises(ports.NoPortsFree, match="will not"):
        ports.resolve("chat", taken=set(), probe=_none_free)


def test_the_refusal_says_what_to_do_about_it():
    with pytest.raises(ports.NoPortsFree) as exc:
        ports.resolve("chat", taken=set(), probe=_none_free)

    text = str(exc.value)
    assert "Release a lane" in text and str(naming.FIRST_LANE_PORT) in text
