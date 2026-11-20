// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0
//
// Verbatim types for GET /api/v1/fleet/status (fleet/view.py). The client
// holds NO status logic (spec §2): these are transcription, not judgment.

import type { Environment, ReceiptStatus } from "@axiom/appkit";

export interface KindView {
  status: ReceiptStatus;
  evidence: string;
  received_at: string;
  signature_state: string;
  cadence_seconds: number;
}

export interface NodeView {
  node_id: string;
  site: string;
  display_name: string | null;
  profile: string | null;
  rollup: ReceiptStatus;
  kinds: Record<string, KindView>;
}

export interface FleetStatus {
  generated_at: string;
  nodes: NodeView[];
}

/** A state transition observed between two polls — presentation-only
 * derivation (spec §1b): the statuses themselves are server-verbatim. */
export interface FeedEntry {
  asOf: string; // the poll's generated_at
  nodeId: string;
  kind: string;
  from: ReceiptStatus;
  to: ReceiptStatus;
  view: KindView;
}

/**
 * The agent-activity payload from `/api/v1/fleet/agents`.
 *
 * A list of ENVIRONMENTS rather than a flat roster, because agent activity
 * only means anything against the install that produced it — and because the
 * list is already narrowed to what this viewer's credential permits, so the
 * client never holds one it has to remember to hide.
 */
export interface AgentActivity {
  environments: Environment[];
}

/** One row of GET /api/v1/receipts/sealed (attest/receipts.py `listed`, #1814): a seal's anchor
 * or an evidence package, from the action ledger. Verbatim; the digest is in `detail`. */
export interface SealedRow {
  what: "sealed" | "packaged" | string;
  at: string | null;
  by: string | null;
  ref: string | null;
  logbook: string | null;
  site: string | null;
  link: string | null;
  summary: string;
  detail: Record<string, unknown>;
}

export interface SealedState {
  rows: SealedRow[] | null;
  unreachableSince: string | null;
}
