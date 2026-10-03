# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Presenters turn a statement into what a person sees or hears (ADR-144).

What is shown is what is signed: every density renders the same content, the
spoken form reads identifiers digit by digit, and a presenter never shows a
value the statement does not contain."""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.attest import presenters
from axiom.extensions.builtins.attest.logbooks import load_logbook

DEMO = Path(__file__).parent / "logbooks" / "demo_log.toml"


@pytest.fixture
def et():
    return load_logbook(DEMO).type("ROUND_CHECK")


CONTENT = {
    "title": "Round check 1207",
    "fields": {"reading": "4.2 bar", "walkdown": True, "note": "valve 12 closed"},
}


def test_default_presenter_identifies_itself_by_logbook_type_and_version(et):
    p = presenters.for_type(et, logbook_version="1")
    assert (p.id, p.version) == ("demo_log.round_check", "1")


def test_every_density_carries_the_title(et):
    p = presenters.for_type(et, logbook_version="1")
    for density in presenters.DENSITIES:
        assert "Round check 1207" in p.render(CONTENT, density)


def test_card_and_page_show_every_field_with_its_label(et):
    p = presenters.for_type(et, logbook_version="1")
    card = p.render(CONTENT, "card")
    assert "Walkdown performed: yes" in card
    assert "reading: 4.2 bar" in card
    assert "note: valve 12 closed" in card
    assert p.render({**CONTENT, "body": "All normal."}, "page").endswith("All normal.")


def test_strip_is_one_line(et):
    p = presenters.for_type(et, logbook_version="1")
    assert "\n" not in p.render(CONTENT, "strip")
    assert "\n" not in p.render(CONTENT, "line")


def test_speakable_reads_numbers_digit_by_digit_and_says_point(et):
    spoken = presenters.for_type(et, logbook_version="1").speakable(CONTENT)
    assert "<digits>1207</digits>" in spoken
    assert "<digits>4</digits> point <digits>2</digits>" in spoken
    assert "Walkdown performed: yes" in spoken


def test_speakable_escapes_markup_in_content(et):
    spoken = presenters.for_type(et, logbook_version="1").speakable(
        {"title": "<digits>9</digits> & co", "fields": {}}
    )
    assert "&lt;digits&gt;" in spoken and "&amp;" in spoken


def test_correctable_fields_are_the_types_fields_in_logbook_order(et):
    refs = presenters.for_type(et, logbook_version="1").correctable_fields(CONTENT)
    assert [r.id for r in refs] == ["reading", "walkdown", "note"]
    assert refs[1].label == "Walkdown performed"


def test_a_registered_presenter_replaces_the_default(et):
    class Custom:
        id = "custom.round"
        version = "7"

        def render(self, content, density):
            return f"custom {density}"

        def speakable(self, content):
            return "custom"

        def correctable_fields(self, content):
            return []

    presenters.register("demo_log", "ROUND_CHECK", Custom())
    try:
        assert presenters.for_type(et, logbook_version="1").id == "custom.round"
    finally:
        presenters.unregister("demo_log", "ROUND_CHECK")
    assert presenters.for_type(et, logbook_version="1").id == "demo_log.round_check"


def test_unknown_density_is_refused(et):
    with pytest.raises(ValueError, match="density"):
        presenters.for_type(et, logbook_version="1").render(CONTENT, "billboard")
