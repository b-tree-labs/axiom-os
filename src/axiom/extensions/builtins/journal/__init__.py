# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The journal: one append-only index of what happened on a node, in lanes (ADR-163).

A row is a reference to a record another extension keeps (a signed entry in its logbook, a
skill call in the action audit chain, an agent action in the ledger, a command in the CLI event
log), with the time, the lane, the actor, one display line, the record's access labels and a
deep link. It holds no payload and no official copy of anything, is fed only from each lane's
committed record or the event that record published, and can be rebuilt from the lane records
(ADR-241 D6). Reading the record goes through its owner, under the owner's rules. If the journal
and a record disagree, the record wins and ``axi journal verify`` says so without writing to
either.

Modules: ``lanes`` (the closed vocabulary and the subject map), ``rows`` (a row and the builders
that read only a record's summary form), ``writer`` (the one idempotent append), ``sources``
(catch-up readers over the durable sources), ``ingest`` (the live subscription and the
catch-up ticker), ``rights`` (what a caller may see), ``service`` (query, lanes, verify),
``skills`` and ``api`` (read-only surfaces; the CLI verbs arrive with the ``log`` noun in unit F5
as ``axi log timeline``, per ADR-243). Design: ``docs/working/design-note-j1-journal.md``.
"""

__all__: list[str] = []
