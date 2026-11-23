# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Switching sharing off and on (ADR-180 §3-4), between two real nodes.

There are two different "sharing off", and they behave differently on purpose:

1. **The forwarder is stopped** (``features disable share-upstream``). Its
   cursor does not move, so turning it back on sends the backlog, each batch
   exactly once.
2. **The site's policy no longer includes a feed.** Batches of that feed are
   passed over and counted, so "not shared" is visible, and the cursor moves
   past them. Putting the feed back shares what lands from then on; it does
   not reach back. A site that wants the gap shared asks for it explicitly.

Neither ever deletes anything the upstream already holds.
"""

from __future__ import annotations

import json
import urllib.request

from axiom.extensions.builtins.data_platform.forward_policy import TableSharePolicy

from .test_a_local_archive_forwards_upstream_exactly_once import _forwarder


def _push(node, token, source, item, feed, n=10):
    minute = sum(item.encode()) % 60
    rows = [{"feed": feed, "channel": "TC1", "ts": f"2026-10-08T02:{minute:02d}:{i:02d}Z",
             "value": 20 + i, "unit": "degC"} for i in range(n)]
    body = {"source": source, "batches": [{"item_id": item, "schema_ref": "site/epics-v1", "rows": rows}]}
    req = urllib.request.Request(node.url + "/ingest/rows", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["rows_landed"]


def _items(node) -> list[str]:
    return sorted(r["item_id"] for r in node.outbox_records())


def test_a_stopped_forwarder_resumes_with_its_backlog_each_batch_once(world, tmp_path):
    local, upstream, local_key, up_key, box, source = world
    _push(local, local_key, source, "a-0", "site.live")
    f = _forwarder(local, upstream, up_key, box, tmp_path, source)
    assert f.forward_once().sent_intake == 1
    held = _items(upstream)

    # sharing off: the forwarder does not run while three batches land
    for i in range(1, 4):
        _push(local, local_key, source, f"a-{i}", "site.live")
    assert _items(upstream) == held          # nothing left, nothing removed

    # sharing on again: a NEW forwarder process, same cursor file
    f2 = _forwarder(local, upstream, up_key, box, tmp_path, source)
    assert f2.forward_once().sent_intake == 3
    assert len(upstream.outbox_records()) == 4
    assert len(set(_items(upstream))) == 4   # each once
    assert f2.forward_once().sent_intake == 0


def test_a_feed_taken_out_of_the_policy_is_counted_not_sent_and_not_reached_back_for(world, tmp_path):
    local, upstream, local_key, up_key, box, source = world
    both = TableSharePolicy({"feeds": ["site.live", "site.aux"]})
    live_only = TableSharePolicy({"feeds": ["site.live"]})

    _push(local, local_key, source, "x-0", "site.aux")
    f = _forwarder(local, upstream, up_key, box, tmp_path, source, policy=both)
    assert f.forward_once().sent_intake == 1

    # the site narrows its policy: aux stops leaving
    _push(local, local_key, source, "x-1", "site.aux")
    _push(local, local_key, source, "x-2", "site.live")
    f = _forwarder(local, upstream, up_key, box, tmp_path, source, policy=live_only)
    r = f.forward_once()
    assert (r.sent_intake, r.excluded_by_policy) == (1, 1)   # visible, not silent
    assert len(upstream.outbox_records()) == 2               # the earlier aux batch is still there

    # aux comes back: new aux is shared; the batch landed while it was out is not
    _push(local, local_key, source, "x-3", "site.aux")
    f = _forwarder(local, upstream, up_key, box, tmp_path, source, policy=both)
    r = f.forward_once()
    assert r.sent_intake == 1
    assert len(upstream.outbox_records()) == 3
    assert f.forward_once().sent_intake == 0


def test_turning_everything_off_never_deletes_what_the_upstream_holds(world, tmp_path):
    local, upstream, local_key, up_key, box, source = world
    for i in range(3):
        _push(local, local_key, source, f"k-{i}", "site.live")
    f = _forwarder(local, upstream, up_key, box, tmp_path, source)
    assert f.forward_once().sent_intake == 3
    before = upstream.outbox_records()

    nothing = TableSharePolicy({"feeds": []})
    _push(local, local_key, source, "k-3", "site.live")
    f = _forwarder(local, upstream, up_key, box, tmp_path, source, policy=nothing)
    r = f.forward_once()
    assert (r.sent_intake, r.excluded_by_policy) == (0, 1)
    assert upstream.outbox_records() == before
