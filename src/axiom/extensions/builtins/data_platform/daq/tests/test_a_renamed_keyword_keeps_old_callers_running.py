# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A keyword renamed in a minor release must not crash the callers it renamed.

0.62.0 renamed ``DAQConsolidator(stream=...)`` to ``feed=`` with no alias. Every
producer still passing ``stream=`` then crashed at construction on any release
past 0.61.x. It surfaced when a site moved its pin forward: the ROM bridge, a
live producer feeding the platform, would have been the first thing the upgrade
broke, and two more site scripts made the same call.

The callers that could be found are being migrated. The ones that cannot be are
the reason for this file: partner sites run producers in their own repositories,
on their own schedules, and an upgrade must not be the moment they find out.

So ``stream=`` is accepted as a deprecated alias. It warns, it never silently
disagrees with ``feed=``, and the canonical name is what the consolidator keeps.
"""

from __future__ import annotations

import warnings

import pytest

from ..consolidator import DAQConsolidator
from ..journal import DAQJournal, OverflowPolicy


def _journal(tmp_path):
    return DAQJournal(tmp_path / "j", policy=OverflowPolicy.DROP_OLDEST, segment_records=50)


def test_the_canonical_keyword_works_and_does_not_warn(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        c = DAQConsolidator(journal=_journal(tmp_path), producer_id="p", feed="rod")
    assert c.feed == "rod"


def test_the_old_keyword_still_constructs(tmp_path):
    """The exact call the ROM bridge makes."""
    with pytest.warns(DeprecationWarning, match="feed"):
        c = DAQConsolidator(journal=_journal(tmp_path), producer_id="p", stream="rod")
    assert c.feed == "rod", "the alias was accepted but its value was dropped"


def test_the_same_value_under_both_names_is_accepted(tmp_path):
    """A caller mid-migration may pass both. Agreement is not an error."""
    with pytest.warns(DeprecationWarning):
        c = DAQConsolidator(journal=_journal(tmp_path), producer_id="p", feed="rod", stream="rod")
    assert c.feed == "rod"


def test_two_names_that_disagree_are_refused(tmp_path):
    """Silently preferring one would route records to the wrong feed."""
    with pytest.raises(ValueError, match="feed"):
        DAQConsolidator(journal=_journal(tmp_path), producer_id="p", feed="rod", stream="power")


def test_neither_name_is_still_an_error(tmp_path):
    with pytest.raises((ValueError, TypeError)):
        DAQConsolidator(journal=_journal(tmp_path), producer_id="p")
