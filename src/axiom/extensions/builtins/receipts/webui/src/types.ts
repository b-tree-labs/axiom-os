// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0
//
// Verbatim types for GET /api/v1/fleet/status (fleet/view.py). The client
// holds NO status logic (spec §2): these are transcription, not judgment.

import type { ReceiptStatus } from "@axiom/appkit";

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
