# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S18 Close out names the period in words (Log views design 2026-11-13, build changes 26 to 29).

The appkit design (``docs/design/proposed-2026-11-13/log-views/``, Close out) titles the drawer by
the act and the period ("Close out the day shift"), shows the period in the site's time, lists on the
confirm card the period with its offset and the number of entries it covers, and, when there is
nothing to close, says who closed the last one and when the running one ends. The node named a
clock period only by its declaration id ("day") and refused "nothing to seal" in a sentence a page
could not read. A period may now name each of its start times; the period carries ``label``; the
seal's card reads the period, the count and the chain head as the design draws them; and nothing
to close is a refusal with its facts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from .conftest import ANN, BO, LOG, SITE, auth, keys, sign_cli, signatory


def _site_zone(zone="Etc/GMT+6"):
    from axiom.extensions.builtins.attest import site_settings

    site_settings.register(lambda site: {"site": {"timezone": zone}} if site == SITE else {})


def _at(world, day, hh, mm=0):
    """The node's clock at a time on the site's own clock (six hours behind UTC here)."""
    world.clock["t"] = datetime(2026, 11, day, hh, mm, tzinfo=UTC) + timedelta(hours=6)


def _seal(world, boundary="day", summary="Day closed."):
    from axiom.extensions.builtins.attest import grants, service, signing

    d = service.create_seal_draft(site_id=SITE, logbook=LOG, summary=summary, origin="human",
                                  for_principal=BO.handle, boundary=boundary)  # fmt: skip
    pres = service.present(d, modality="screen")
    service.respond(pres.presentation_id, principal=signatory(BO).principal, answer="sign", via="screen")
    token = grants.mint(pres.presentation_id, auth=auth(BO), device=grants.Device("desk-1", "personal"),
                        signer=signing.signer())  # fmt: skip
    return pres, service.sign(pres.presentation_id, signatory=signatory(BO), signer=signing.signer(),
                              source="screen", grant=token, grant_keys=keys()).record  # fmt: skip


@pytest.fixture
def zoned(world):
    from axiom.extensions.builtins.attest import site_settings

    _site_zone()
    yield world
    site_settings.unregister()


@pytest.mark.acceptance("LV-26-A1")
def test_lv_26_a1_a_period_is_named_by_its_start_time_in_words(zoned):
    from axiom.extensions.builtins.attest import service

    _at(zoned, 10, 7)
    sign_cli(ANN, "NOTE", {"text": "in the day"})
    _at(zoned, 10, 18, 5)
    preview = service.seal_preview(SITE, LOG, boundary="day")
    assert preview["covers"]["period"] == {"name": "day", "label": "day shift",
                                           "start": "2026-11-10T06:00:00-06:00",
                                           "end": "2026-11-10T18:00:00-06:00"}  # fmt: skip
    assert preview["title"] == "Close out: day shift"
    # the summary is written for the period running now: the page names it ("for the night shift")
    assert preview["next"] == {"name": "day", "label": "night shift", "start": "2026-11-10T18:00:00-06:00",
                               "end": "2026-11-11T06:00:00-06:00"}  # fmt: skip
    _at(zoned, 11, 6, 45)  # the night shift ended at 06:00, and holds no entry
    with pytest.raises(service.AttestRefused):
        service.seal_preview(SITE, LOG, boundary="day")


@pytest.mark.acceptance("LV-26-A2")
def test_lv_26_a2_the_card_reads_the_period_in_site_time_the_count_and_the_chain_head(zoned):
    _at(zoned, 10, 7)
    first = sign_cli(ANN, "NOTE", {"text": "one"})
    sign_cli(ANN, "NOTE", {"text": "two"})
    last = sign_cli(ANN, "NOTE", {"text": "three"})
    _at(zoned, 10, 18, 5)
    pres, _ = _seal(zoned)
    lines = pres.forms["card"].splitlines()
    assert lines[0] == "Close out: day shift"
    assert "  Period: Tue 10 Nov 2026, 06:00 to 18:00 (UTC−06:00)" in lines
    n = last["seq"] - first["seq"] + 1
    assert f"  Covers: #{first['seq']} to #{last['seq']}, {n} entries" in lines
    head = last["digest"]
    assert f"  Chain head: #{last['seq']} {head[:4]}…{head[-4:]}" in lines


@pytest.mark.acceptance("LV-29-A1")
def test_lv_29_a1_nothing_to_close_says_who_closed_it_and_when_the_running_one_ends(zoned, web):
    from axiom.extensions.builtins.attest import service

    from .conftest import as_

    _at(zoned, 10, 7)
    sign_cli(ANN, "NOTE", {"text": "in the day"})
    _at(zoned, 10, 18, 5)
    _, seal = _seal(zoned)
    with pytest.raises(service.NothingToClose) as caught:
        service.seal_preview(SITE, LOG, boundary="day")
    facts = caught.value.facts
    assert facts["period"]["label"] == "day shift"
    assert facts["sealed"] == {"seq": seal["seq"], "by": "Bo Example", "at": "2026-11-10T18:05:00-06:00"}
    assert facts["next"] == {"name": "day", "label": "night shift", "start": "2026-11-10T18:00:00-06:00",
                             "end": "2026-11-11T06:00:00-06:00"}  # fmt: skip
    r = as_(web, BO).get(f"/api/v1/attest/{LOG}/seal/preview", params={"boundary": "day"})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "nothing_to_close" and body["sealed"]["by"] == "Bo Example"
    assert body["next"]["label"] == "night shift" and body["period"]["label"] == "day shift"


@pytest.mark.acceptance("LV-29-A2")
def test_lv_29_a2_a_period_with_no_entries_is_nothing_to_close_with_none_sealed(zoned):
    from axiom.extensions.builtins.attest import service

    _at(zoned, 10, 18, 5)
    with pytest.raises(service.NothingToClose) as caught:
        service.seal_preview(SITE, LOG, boundary="day")
    assert caught.value.facts["sealed"] is None
    assert caught.value.facts["period"]["label"] == "day shift"
    assert caught.value.facts["next"]["label"] == "night shift"


@pytest.mark.acceptance("LV-29-A3")
def test_lv_29_a3_the_not_closed_period_carries_its_name_in_words(zoned):
    from axiom.extensions.builtins.attest import service

    _at(zoned, 10, 9)
    sign_cli(ANN, "NOTE", {"text": "a day's entry"})
    _at(zoned, 10, 18, 45)
    (due,) = service.unsealed_periods(SITE, LOG, viewer=BO.handle, state_dir=zoned.state_dir)
    assert due["period"]["label"] == "day shift"


@pytest.mark.acceptance("LV-26-A3")
def test_lv_26_a3_names_must_match_the_start_times(tmp_path):
    from axiom.extensions.builtins.attest.logbooks import LogbookError, load_logbook

    p = tmp_path / "bad.toml"
    p.write_text(
        '[logbook]\nid = "bad"\nversion = "1"\ndisplay = "Bad"\nhandle = "bad"\n'
        '[[type]]\nid = "NOTE"\nmeanings = ["authored"]\nroles = ["member"]\n'
        '[period.shift]\nstarts = ["06:00", "14:00"]\nnames = ["day shift"]\n',
        encoding="utf-8",
    )
    with pytest.raises(LogbookError, match="one name per start time"):
        load_logbook(p)
