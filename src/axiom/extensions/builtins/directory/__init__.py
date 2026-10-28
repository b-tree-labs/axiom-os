# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``directory`` — group and membership resolution as a pluggable seam (ADR-103).

The missing edge between an authenticated principal (``auth``, ADR-075) and an
authorization graph (``authz``/OpenFGA, ADR-083). OpenFGA answers questions
about tuples it has been given and discovers nothing on its own; this is what
supplies the facts.

Phase 1: the port, the day-one adapters, group→role mapping, and claims-first
resolution with its degradation rules. Phase 2: the reconciler that projects
membership into relationship tuples as a *diff* (removals are the point, per
ADR-086) and the recently-revoked fast path that makes a revocation take effect
before the token carrying it expires.
"""

from axiom.extensions.builtins.directory.binding import subject_from_membership
from axiom.extensions.builtins.directory.mapping import GroupRoleMap
from axiom.extensions.builtins.directory.protocol import (
    DeltaPage,
    DirectoryCapability,
    DirectoryProvider,
    Group,
    GroupRef,
    PrincipalRef,
)
from axiom.extensions.builtins.directory.providers import (
    available_providers,
    get_provider,
    register_provider,
)
from axiom.extensions.builtins.directory.reconcile import (
    MembershipReconciler,
    ReconcileResult,
    RelationTuple,
    StaleProjectionError,
    TupleStore,
)
from axiom.extensions.builtins.directory.resolution import (
    Membership,
    MembershipResolver,
)
from axiom.extensions.builtins.directory.revoked import JsonFileRevokedSet, RevokedSet
from axiom.extensions.builtins.directory.stores import JsonFileTupleStore, resolve_tuple_store
from axiom.extensions.builtins.directory.sync import GroupSyncResult, SyncReport, run_sync

__all__ = [
    "DeltaPage",
    "GroupSyncResult",
    "JsonFileRevokedSet",
    "JsonFileTupleStore",
    "SyncReport",
    "resolve_tuple_store",
    "run_sync",
    "DirectoryCapability",
    "DirectoryProvider",
    "Group",
    "GroupRef",
    "GroupRoleMap",
    "Membership",
    "MembershipReconciler",
    "MembershipResolver",
    "PrincipalRef",
    "ReconcileResult",
    "RelationTuple",
    "RevokedSet",
    "StaleProjectionError",
    "TupleStore",
    "available_providers",
    "get_provider",
    "register_provider",
    "subject_from_membership",
]
