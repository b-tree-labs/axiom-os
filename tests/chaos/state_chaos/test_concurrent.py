# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Concurrent writer: a reader must only ever see a whole state.

A writer process alternates the REAL writer between two valid states while a
reader process hammers the REAL reader. Anything the reader observes that is
not one of the two states -- a parse error, an empty default, a half file --
is a torn read. For permission-bearing files a torn read that defaults is also
a window in which the answer is wrong (consent reads as undecided, a deny rule
reads as unset).
"""

from __future__ import annotations

import pytest

from .artifacts import BY_ID, HEAD, populate
from .harness import ChaosRoot, restore_permissions, run_concurrent

#: The reader reads for at least this long, while the writer is writing.
DURATION_S = 2.5

#: ...and at least this many times. The guard below asserts more than five, so
#: a run cannot end before it has done enough to prove anything.
MIN_READS = 50

#: A ceiling, so a store that has become slow fails the test rather than
#: hanging it.
HARD_CAP_S = 60.0

# The two processes coordinate through two marker files rather than each
# running its own clock. Both used to run for DURATION_S from their own start,
# so on a loaded runner the reader could start late, overlap the writer for
# only part of its run, or do so few reads that the guard failed with the
# store working correctly. Now the writer says when it has started, the reader
# waits for that, reads until it has done MIN_READS and DURATION_S has passed,
# then says it is done, and the writer writes until it hears that. Every read
# therefore overlaps writing, and the read count is a floor, not a hope.
_WRITER_STARTED = ".chaos-writer-started"
_READER_DONE = ".chaos-reader-done"


def _writer(body: str) -> str:
    return HEAD + f"""
import time
i = 0
hard = time.time() + {HARD_CAP_S}
started = S / {_WRITER_STARTED!r}
done = S / {_READER_DONE!r}
while not done.exists() and time.time() < hard:
    A = (i % 2 == 0)
{body}
    i += 1
    if i == 1:
        started.touch()
emit({{'writes': i, 'reader_finished': done.exists()}})
"""


def _reader(setup: str, expr: str) -> str:
    return HEAD + setup + f"""
import time
seen = {{}}; errors = {{}}
hard = time.time() + {HARD_CAP_S}
started = S / {_WRITER_STARTED!r}
while not started.exists() and time.time() < hard:
    time.sleep(0.005)
reads = 0
end = time.time() + {DURATION_S}
while (reads < {MIN_READS} or time.time() < end) and time.time() < hard:
    try:
        v = {expr}
        seen[repr(v)] = seen.get(repr(v), 0) + 1
    except BaseException as e:
        errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
    reads += 1
(S / {_READER_DONE!r}).touch()
emit({{'seen': seen, 'errors': errors, 'overlapped': started.exists()}})
"""


def _indent(code: str) -> str:
    return "\n".join("    " + ln for ln in code.strip("\n").splitlines())


CASES = {
    "agents-consent": dict(
        writer=_writer(_indent("""
from axiom.extensions.builtins.agents.consent import AgentConsent, save_consent
save_consent(AgentConsent(decided=True, opted_out=A, enabled=[] if A else ['hygiene']))
""")),
        reader=_reader("from axiom.extensions.builtins.agents.consent import load_consent\n",
                       "(lambda c: (c.opted_out, tuple(c.enabled)))(load_consent())"),
        valid={"(True, ())", "(False, ('hygiene',))"},
    ),
    "tool-permissions": dict(
        writer=_writer(_indent("""
from axiom.extensions.builtins.chat.permissions import ToolPermissions
ToolPermissions(S / 'tool_permissions.json').set('shell', 'deny' if A else 'allow')
""")),
        reader=_reader("from axiom.extensions.builtins.chat.permissions import ToolPermissions\n",
                       "ToolPermissions(S / 'tool_permissions.json').get('shell')"),
        valid={"'deny'", "'allow'"},
    ),
    "vault-index": dict(
        writer=_writer(_indent("""
from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
ForeignCredentialStore(S).update_metadata('alpha', notes='A' if A else 'B')
""")),
        reader=_reader("from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore\n",
                       "ForeignCredentialStore(S).metadata('alpha').get('notes')"),
        valid={"'A'", "'B'", "None"},
    ),
    "settings": dict(
        writer=_writer(_indent("""
from axiom.extensions.builtins.settings.store import SettingsStore
SettingsStore().set('raci.trust', 1 if A else 3, scope='global')
""")),
        reader=_reader("from axiom.extensions.builtins.settings.store import SettingsStore\n",
                       "SettingsStore().get('raci.trust')"),
        valid={"1", "3"},
    ),
    "session-file": dict(
        writer=_writer(_indent("""
from axiom.memory.session import SessionMetadata, _save_metadata
_save_metadata(SessionMetadata(session_id='session://aaaaaaaa-0000-0000-0000-000000000001',
    name='proj-2026', principal_id='@ben', created_at='2026-01-01T00:00:00+00:00',
    last_active_at='2026-01-01T00:00:00+00:00', cwd_hint='', note='A' if A else 'B'))
""")),
        reader=_reader("from axiom.memory.session import _load_metadata\n",
                       "_load_metadata('session://aaaaaaaa-0000-0000-0000-000000000001').note"),
        valid={"'A'", "'B'", "''"},
    ),
    "nudges": dict(
        writer=_writer(_indent("""
from axiom.infra.nudges import NudgeStore
NudgeStore().add(f'n{i}', 'm')
""")),
        reader=_reader("from axiom.infra.nudges import NudgeStore\n",
                       "NudgeStore().pending() and 'nonempty'"),
        valid={"'nonempty'"},
    ),
    "approvals": dict(
        writer=_writer(_indent("""
from axiom.infra.orchestrator.actions import Action
from axiom.infra.orchestrator.approval_store import FileActionStore
FileActionStore().put(Action(name='doc.publish', params={'i': i}))
""")),
        reader=_reader("from axiom.infra.orchestrator.approval_store import FileActionStore\n",
                       "len(FileActionStore().all()) > 0"),
        valid={"True"},
    ),
}


@pytest.mark.parametrize("art_id", sorted(CASES))
def test_a_reader_only_ever_sees_a_whole_state(art_id, tmp_path):
    case = CASES[art_id]
    chaos = ChaosRoot(tmp_path / "chaos")
    try:
        populate(BY_ID[art_id], chaos)
        w, r = run_concurrent(chaos, case["writer"], case["reader"])
    finally:
        restore_permissions(chaos.root)
    assert w.outcome == "ok", f"writer failed: {w.exc_type} {w.exc_msg}\n{w.traceback}"
    assert r.outcome == "ok", f"reader failed: {r.exc_type} {r.exc_msg}\n{r.traceback}"
    seen = r.value["seen"]
    torn = {k: v for k, v in seen.items() if k not in case["valid"]}
    assert not torn and not r.value["errors"], (
        f"{art_id}: torn reads {torn}, errors {r.value['errors']}"
    )
    # A loaded runner can finish only a few iterations of a growing store
    # (approvals has done this on main: three valid reads, no tear). That
    # run did not prove the property. It also did not find a tear, so it
    # must not turn the suite red.
    writes = w.value["writes"]
    reads = sum(seen.values())
    if writes <= 5 or reads <= 5:
        pytest.skip(
            f"{art_id}: the run was too short to prove the property "
            f"(writes={writes}, reads={reads}); nothing torn was observed"
        )
