# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Catalog of persisted harness state the chaos matrix attacks.

Each :class:`Artifact` names where the state lives (relative to the state
dir), how to populate it with the REAL writer (run in a subprocess inside the
chaos env), the REAL reader to run after damage (the ``probe``), and a
semantic ``check`` that catches silent wrong answers: the probe may succeed
and still be wrong.

Probe snippets run inside a ``try:`` block supplied by the harness; they call
``emit(value)`` to report what the reader returned. ``S`` is the state dir.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .harness import ChaosRoot, ProbeResult, run_probe

ABSENT = frozenset({'delete_file', 'delete_dir', 'rename_away', 'dangling_symlink'})

HEAD = "import os, pathlib\nS = pathlib.Path(os.environ['AXI_STATE_DIR'])\n"

#: The memory layer's runtime store is Postgres. A probe must never reach the
#: developer's live one, so it binds the same in-memory SQLite seam the root
#: conftest uses before touching anything memory-shaped.
MEM_SEAM = """
import contextlib
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from axiom.memory import pg_store, stores
from axiom.memory.pg_models import Base
_eng = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
Base.metadata.create_all(_eng)
_fac = sessionmaker(_eng)
@contextlib.contextmanager
def _prov():
    s = _fac()
    try:
        yield s
    finally:
        s.close()
pg_store.set_provider(_prov)
# The recall corpus the chat composition now builds is also a database store; the probe uses the same
# SQLite test seam the root conftest does, so nothing reaches a real server.
from axiom.rag import store_factory
from axiom.rag.sqlite_store import SQLiteRAGStore
store_factory.allow_sqlite_for_tests(True)
stores.set_recall_factory(lambda base: SQLiteRAGStore(f"sqlite:///{base / 'recall.db'}"))
"""


def sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


@dataclass(frozen=True)
class Artifact:
    id: str
    rel: str
    kind: str  # "file" | "dir"
    populate: str
    probe: str
    #: Violation message, or None. Called as check(result, chaos, populated, damage).
    check: Callable[[ProbeResult, ChaosRoot, object, str], str | None] = lambda *a: None
    #: Damaged user data that must not be silently defaulted away.
    loss_matters: bool = True
    wrong_schema: bytes | None = None
    #: Substring a warning/error should name (defaults to the file name).
    hint: str | None = None
    #: Security-bearing: damage must never widen what is allowed.
    security: bool = False
    #: A snippet that performs ONE real write; used by the concurrent-writer test.
    writer: str | None = None
    #: Snippet reading the state and emitting something comparable (concurrency).
    reader: str | None = None
    #: A zero-byte file is a valid, empty log (append-only logs have no other empty form).
    empty_is_valid: bool = False
    tags: tuple[str, ...] = field(default=())

    def hint_for(self, chaos: ChaosRoot) -> str:
        return self.hint or str(chaos.path(self.rel))


# ---------------------------------------------------------------------------
# Semantic checks
# ---------------------------------------------------------------------------


def _ok_value(res: ProbeResult) -> dict | None:
    return res.value if res.outcome == "ok" and isinstance(res.value, dict) else None


def check_identity(res, chaos, pop, dmg):
    # Whatever happened, the private key must not have been replaced...
    if sha(chaos.path("identity/private.pem")) not in (pop["priv_sha"], ""):
        return "private.pem was overwritten: signed history is orphaned"
    v = _ok_value(res)
    # ...and a "successful" run must still be the SAME identity.
    if v is not None and v.get("node_id") != pop["node_id"]:
        return f"silently became a different identity ({v.get('node_id')} != {pop['node_id']})"
    return None


def check_consent(res, chaos, pop, dmg):
    v = _ok_value(res)
    if v is None:
        return None
    # The operator opted OUT of every agent. Any enabled agent is a widening.
    if v.get("enabled") or not v.get("opted_out"):
        return f"damage widened consent: opted_out={v.get('opted_out')} enabled={v.get('enabled')}"
    return None


def check_permissions(res, chaos, pop, dmg):
    v = _ok_value(res)
    if v is None:
        return None
    if v.get("unset") == "allow":
        return "an unset tool now reads as allow"
    return None


def check_site_rules(res, chaos, pop, dmg):
    v = _ok_value(res)
    if v is None:
        return None
    if v.get("rules", 0) < 1:
        return "the site deny rule is gone and the policy reads as empty (fail-open)"
    return None


def check_settings(res, chaos, pop, dmg):
    v = _ok_value(res)
    if v is None:
        return None
    if v.get("trust") != 1:
        return f"raci.trust was 1 (locked down), now reads {v.get('trust')} (more permissive)"
    return None


def check_vault_index(res, chaos, pop, dmg):
    v = _ok_value(res)
    if v is None or dmg in ABSENT:
        return None
    missing = {"alpha", "beta"} - set(v.get("names_after", []))
    if missing:
        return f"credential metadata silently lost: {sorted(missing)}"
    return None


def check_ledger(res, chaos, pop, dmg):
    v = _ok_value(res)
    if v is None or dmg in ABSENT:
        return None
    if not v.get("new_present"):
        return "a record just written is not readable back (silent data loss)"
    return None


def check_node_key(res, chaos, pop, dmg):
    now = sha(chaos.path("memory/node.key"))
    if now and now not in (pop["key_sha"], pop.get("damaged_sha")):
        return "the node signing key was silently replaced: signed history is orphaned"
    return None


def check_declined(res, chaos, pop, dmg):
    v = _ok_value(res)
    if v is None or dmg in ABSENT or "federation_declined.json" in (res.log or ""):
        return None
    if not v.get("kept"):
        return "a previously recorded decline was silently dropped"
    return None


def check_version_directives(res, chaos, pop, dmg):
    v = _ok_value(res)
    if v is None or dmg in ABSENT or dmg == "zero_length" or "version_directives.jsonl" in (res.log or ""):
        return None
    if v.get("active", 0) < 1:
        return "a version-requirement directive silently disappeared"
    return None


def check_sessions(res, chaos, pop, dmg):
    v = _ok_value(res)
    if v is None:
        return None
    warned = "aaaaaaaa" in (res.log or "")
    if not v.get("listed") and v.get("on_disk") and not warned:
        return "the session is on disk but the listing silently omits it"
    return None


def check_memory_dir(res, chaos, pop, dmg):
    if dmg in ABSENT:
        # The whole tree went together: key, audit trail and ledger. Nothing signed
        # survives to be orphaned (the loss itself is the absent-kind finding).
        return None
    return check_node_key(res, chaos, pop, dmg)


def check_audit(res, chaos, pop, dmg):
    v = _ok_value(res)
    if v is None or dmg in ABSENT or dmg == "zero_length":
        return None
    if v.get("n", 0) < 1 and "audit.jsonl" not in (res.log or ""):
        return "the signed audit trail reads as empty (entries silently lost)"
    return None


def check_links(res, chaos, pop, dmg):
    v = _ok_value(res)
    if v is None or dmg in ABSENT:
        return None
    # A binding must never be silently dropped, nor silently pointed elsewhere.
    if v.get("ben") not in ("@ben", None):
        return f"a channel login now resolves to {v.get('ben')}"
    if v.get("ben") is None and "identity-links.json" not in (res.log or "") and dmg != "partial_write":
        return "an existing channel binding silently vanished while the link was re-written"
    return None


def check_commands(res, chaos, pop, dmg):
    return None


def check_node_toml(res, chaos, pop, dmg):
    return None


def check_connectors(res, chaos, pop, dmg):
    return None


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

IDENTITY_POP = HEAD + """
import hashlib
from axiom.vega.federation.identity import generate_identity
d = S / 'identity'
i = generate_identity(owner='bbooth', display_name='ben:laptop', keys_dir=d)
emit({'node_id': i.node_id, 'priv_sha': hashlib.sha256((d/'private.pem').read_bytes()).hexdigest()})
"""

IDENTITY_FLOW = HEAD + """
# The shape every caller (classroom, federation init) uses: load, else generate.
from axiom.vega.federation.identity import generate_identity, load_identity
d = S / 'identity'
i = load_identity(d)
if i is None:
    i = generate_identity(owner='bbooth', keys_dir=d)
emit({'node_id': i.node_id})
"""

IDENTITY_SIGN = HEAD + """
from axiom.vega.identity.node_key import node_signer
sig = node_signer(S / 'identity').sign(b'hello')
emit({'signed': len(sig)})
"""

CONSENT_POP = HEAD + """
from axiom.extensions.builtins.agents.consent import record_decision
record_decision(enabled=[], opted_out=True, version='1.0.0')
"""
CONSENT_PROBE = HEAD + """
from axiom.extensions.builtins.agents.consent import load_consent
c = load_consent()
emit({'opted_out': c.opted_out, 'enabled': c.enabled, 'decided': c.decided})
"""

PERMS_POP = HEAD + """
from axiom.extensions.builtins.chat.permissions import ToolPermissions
p = ToolPermissions(S / 'tool_permissions.json')
p.set('shell', 'deny'); p.set('fs.read', 'allow')
"""
PERMS_PROBE = HEAD + """
from axiom.extensions.builtins.chat.permissions import ToolPermissions
p = ToolPermissions.load_default()
emit({'shell': p.get('shell'), 'unset': p.get('never-configured-tool'), 'n': len(p.all())})
"""

APPROVALS_POP = HEAD + """
from axiom.infra.orchestrator.actions import Action
from axiom.infra.orchestrator.approval_store import FileActionStore
FileActionStore().put(Action(name='doc.publish', params={'source': 'a.md'}))
"""
APPROVALS_PROBE = HEAD + """
from axiom.infra.orchestrator.approval_store import FileActionStore
acts = FileActionStore().all()
emit({'n': len(acts)})
"""

TOKENS_POP = HEAD + """
from axiom.infra.orchestrator.approval_tokens import TokenStore
TokenStore().mint('act1', channel='email')
"""
TOKENS_PROBE = HEAD + """
from axiom.infra.orchestrator.approval_tokens import TokenStore
emit({'live': len(TokenStore().live_for('act1'))})
"""

SITE_RULES_POP = HEAD + """
d = S / 'authority'; d.mkdir(parents=True, exist_ok=True)
(d / 'site_rules.toml').write_text('[[rule]]\\ntool = "shell"\\ndisposition = "deny"\\n')
"""
SITE_RULES_PROBE = HEAD + """
from axiom.infra.authority_rules import load_site_policy
p = load_site_policy()
emit({'rules': len(p.rules), 'present': p.present})
"""

SETTINGS_POP = HEAD + """
(S / 'settings.toml').write_text('[raci]\\ntrust = 1\\n')
"""
SETTINGS_PROBE = HEAD + """
from axiom.extensions.builtins.settings.store import SettingsStore
emit({'trust': SettingsStore().get('raci.trust')})
"""

VAULT_POP = HEAD + """
from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
s = ForeignCredentialStore(S)
s.set('alpha', b'secret-a'); s.set('beta', b'secret-b')
"""
VAULT_PROBE = HEAD + """
from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
s = ForeignCredentialStore(S)
before = [r['name'] for r in s.list()]
s.set('gamma', b'secret-c')
emit({'names_before': before, 'names_after': [r['name'] for r in s.list()]})
"""

LEDGER_POP = HEAD + """
from axiom.policy.action_ledger import ActionLedger
l = ActionLedger(state_dir=S)
for i in range(3):
    l.record_action(agent='a', op_class='x.y', name='n', candidate=f'c{i}', guards=[], outcome='proceeded')
"""
LEDGER_PROBE = HEAD + """
from axiom.policy.action_ledger import ActionLedger
l = ActionLedger(state_dir=S)
l.record_action(agent='a', op_class='x.y', name='n', candidate='NEWREC', guards=[], outcome='proceeded')
rows = ActionLedger(state_dir=S).recent(n=50)
emit({'new_present': any(r['candidate'] == 'NEWREC' for r in rows), 'n': len(rows)})
"""

MEMORY_POP = HEAD + MEM_SEAM + """
import hashlib
from axiom.extensions.builtins.chat.memory_wiring import _build_user_composition
c = _build_user_composition()
c.write(content={'event_time': '2026-01-01T00:00:00+00:00', 'text': 'hi'}, cognitive_type='episodic',
        principal_id='@ben', agents={'a'}, resources={'r'}, accountable_human_id='user@example.org')
emit({'key_sha': hashlib.sha256((S/'memory'/'node.key').read_bytes()).hexdigest()})
"""
MEMORY_PROBE = HEAD + MEM_SEAM + """
from axiom.extensions.builtins.chat.memory_wiring import _build_user_composition
c = _build_user_composition()
emit({'built': True})
"""

AUDIT_PROBE = HEAD + """
from axiom.memory.attest import AuditLog
emit({'n': len(list(AuditLog(S / 'memory' / 'audit.jsonl').read_all()))})
"""

COMMANDS_POP = HEAD + """
d = S / 'commands'; d.mkdir(parents=True, exist_ok=True)
(d / 'hello.md').write_text('---\\ndescription: say hi\\n---\\nSay hello to $ARGUMENTS\\n')
"""
COMMANDS_PROBE = HEAD + """
from axiom.extensions.builtins.chat.user_commands import load_user_commands
emit({'names': sorted(load_user_commands())})
"""

NUDGES_POP = HEAD + """
from axiom.infra.nudges import NudgeStore
NudgeStore().add('ext-1', 'try the thing', 'axi thing')
"""
NUDGES_PROBE = HEAD + """
from axiom.infra.nudges import NudgeStore
emit({'pending': [n.id for n in NudgeStore().pending()]})
"""

NODE_TOML_POP = HEAD + """
from axiom.infra import node_functions as nf
nf.save(nf.NodeConfig(role='emitter', functions=(), features=()))
"""
NODE_TOML_PROBE = HEAD + """
from axiom.infra import node_functions as nf
c = nf.load()
emit({'role': c.role, 'problem': c.problem})
"""

CONNECTORS_POP = HEAD + """
d = S / 'connectors'; d.mkdir(parents=True, exist_ok=True)
import json
(d / 'enabled.json').write_text(json.dumps({'enabled': ['ai.axiom.connector.slack']}))
"""
CONNECTORS_PROBE = HEAD + """
from axiom.extensions.builtins.connect.connector_cli import _load_state
emit({'enabled': sorted(_load_state().enabled())})
"""

DECLINED_POP = HEAD + """
from axiom.setup.federation_probe import record_decline
record_decline('endpoint-a')
"""
DECLINED_PROBE = HEAD + """
from axiom.setup.federation_probe import has_declined, record_decline
record_decline('endpoint-b')
emit({'kept': has_declined('endpoint-a'), 'new': has_declined('endpoint-b')})
"""

DIRECTIVES_POP = HEAD + """
from axiom.policy import version_directive_store as v
v.add(v.VersionDirective(package='axiom-os-lm', min_version='1.0.0', issuer='@ben'))
"""
DIRECTIVES_PROBE = HEAD + """
from axiom.policy import version_directive_store as v
emit({'active': len(v.load_active())})
"""

LINKS_POP = HEAD + """
from axiom.extensions.builtins.connect.identity_link import link_identity
link_identity('slack', 'U123', '@ben')
"""
LINKS_PROBE = HEAD + """
from axiom.extensions.builtins.connect.identity_link import link_identity, resolve_principal
link_identity('slack', 'U999', '@sam')
emit({'ben': resolve_principal('slack', 'U123'), 'sam': resolve_principal('slack', 'U999')})
"""

SESSIONS_POP = HEAD + """
from axiom.memory.session import SessionMetadata, _save_metadata
_save_metadata(SessionMetadata(session_id='session://aaaaaaaa-0000-0000-0000-000000000001',
    name='proj-2026', principal_id='@ben', created_at='2026-01-01T00:00:00+00:00', last_active_at='2026-01-01T00:00:00+00:00', cwd_hint=''))
"""
SESSIONS_PROBE = HEAD + """
import pathlib
from axiom.memory.session import list_sessions, find_by_name
on_disk = [p.name for p in (S / 'sessions').glob('aaaaaaaa*.json')] if (S / 'sessions').is_dir() else []
emit({'listed': [m.name for m in list_sessions()], 'by_name': bool(find_by_name('proj-2026')), 'on_disk': bool(on_disk)})
"""

EXT_POP = HEAD + """
d = S / 'extensions' / 'myext'; d.mkdir(parents=True, exist_ok=True)
(d / 'axiom-extension.toml').write_text('[extension]\\nname = "myext"\\nversion = "0.1.0"\\ndescription = "x"\\nlicense = "Apache-2.0"\\nowner = "me"\\naeos_version = "0.1.0"\\n')
"""
EXT_PROBE = HEAD + """
from axiom.extensions.discovery import discover_extensions
emit({'names': [e.name for e in discover_extensions(S / 'extensions')]})
"""


ARTIFACTS: tuple[Artifact, ...] = (
    Artifact("identity-json", "identity/identity.json", "file", IDENTITY_POP, IDENTITY_FLOW,
             check_identity, security=True,
             tags=("identity",)),
    Artifact("identity-private-key", "identity/private.pem", "file", IDENTITY_POP, IDENTITY_SIGN,
             lambda r, c, p, d: None, security=True, tags=("identity",)),
    Artifact("agents-consent", "agents_consent.json", "file", CONSENT_POP, CONSENT_PROBE,
             check_consent, security=True, tags=("consent",),
             wrong_schema=b'["not", "an", "object"]'),
    Artifact("tool-permissions", "tool_permissions.json", "file", PERMS_POP, PERMS_PROBE,
             check_permissions, security=True, tags=("consent",)),
    Artifact("approvals", "orchestrator/approvals.json", "file", APPROVALS_POP, APPROVALS_PROBE,
             security=True, tags=("approval",), wrong_schema=b'["x"]'),
    Artifact("approval-tokens", "orchestrator/approval_tokens.json", "file", TOKENS_POP, TOKENS_PROBE,
             security=True, tags=("approval",), wrong_schema=b'["x"]'),
    Artifact("site-rules", "authority/site_rules.toml", "file", SITE_RULES_POP, SITE_RULES_PROBE,
             check_site_rules, security=True, tags=("authority",),
             wrong_schema=b'rule = 5\n'),
    Artifact("settings", "settings.toml", "file", SETTINGS_POP, SETTINGS_PROBE,
             check_settings, security=True, tags=("settings",),
             wrong_schema=b'raci = "locked"\n'),
    Artifact("vault-index", "secrets/foreign-credentials.json", "file", VAULT_POP, VAULT_PROBE,
             check_vault_index, security=True, tags=("vault",), wrong_schema=b'["x"]'),
    Artifact("action-ledger", "audit/actions.jsonl", "file", LEDGER_POP, LEDGER_PROBE,
             check_ledger, tags=("ledger",), wrong_schema=b'"not a record"\n',
             empty_is_valid=True),
    Artifact("memory-node-key", "memory/node.key", "file", MEMORY_POP, MEMORY_PROBE,
             check_node_key, security=True, tags=("memory", "identity")),
    Artifact("memory-audit", "memory/audit.jsonl", "file", MEMORY_POP, AUDIT_PROBE,
             check_audit, security=True, tags=("memory",), wrong_schema=b'"not an entry"\n',
             empty_is_valid=True),
    Artifact("memory-dir", "memory", "dir", MEMORY_POP, MEMORY_PROBE,
             check_memory_dir,
             security=True, tags=("memory", "identity")),
    Artifact("user-commands-dir", "commands", "dir", COMMANDS_POP, COMMANDS_PROBE,
             check_commands, loss_matters=True, tags=("user",)),
    Artifact("nudges", "nudges.json", "file", NUDGES_POP, NUDGES_PROBE,
             loss_matters=False, tags=("cache",), wrong_schema=b'{"a": 1}'),
    Artifact("node-toml", "node.toml", "file", NODE_TOML_POP, NODE_TOML_PROBE,
             check_node_toml, loss_matters=False, tags=("node",), wrong_schema=b'node = "x"\n'),
    Artifact("connectors-enabled", "connectors/enabled.json", "file", CONNECTORS_POP,
             CONNECTORS_PROBE, check_connectors, tags=("connectors",)),
    Artifact("federation-declined", "federation_declined.json", "file", DECLINED_POP,
             DECLINED_PROBE, check_declined, loss_matters=False, tags=("cache",)),
    Artifact("identity-links", "connectors/identity-links.json", "file", LINKS_POP, LINKS_PROBE,
             check_links, security=True, tags=("identity",), wrong_schema=b'["x"]'),
    Artifact("version-directives", "policy/version_directives.jsonl", "file", DIRECTIVES_POP,
             DIRECTIVES_PROBE, check_version_directives, security=True, tags=("policy",),
             wrong_schema=b'"not a record"\n', empty_is_valid=True),
    Artifact("session-file", "sessions/aaaaaaaa-0000-0000-0000-000000000001.json", "file",
             SESSIONS_POP, SESSIONS_PROBE, check_sessions, tags=("sessions",)),
    Artifact("sessions-dir", "sessions", "dir", SESSIONS_POP, SESSIONS_PROBE, check_sessions,
             tags=("sessions",)),
    Artifact("extension-manifest", "extensions/myext/axiom-extension.toml", "file", EXT_POP,
             EXT_PROBE, loss_matters=False, hint="myext", tags=("extensions",)),
    Artifact("extension-dir", "extensions/myext", "dir", EXT_POP, EXT_PROBE, loss_matters=False,
             hint="myext", tags=("extensions",)),
)

BY_ID = {a.id: a for a in ARTIFACTS}


def populate(art: Artifact, chaos: ChaosRoot):
    """Run the artifact's real writer inside the chaos env; return its emitted value."""
    res = run_probe(chaos, art.populate)
    assert res.outcome == "ok", f"populate {art.id} failed: {res.exc_type} {res.exc_msg}\n{res.traceback}"
    assert chaos.path(art.rel).exists() or art.kind == "dir", f"{art.id}: {art.rel} not created"
    return res.value
