// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0

import { EvidenceRow, NodeCard, type EvidenceRowProps } from "@axiom/appkit";
import { microcopy } from "./microcopy";
import type { FeedEntry, FleetStatus, KindView } from "./types";

/** Age in whole seconds between the poll stamp and a report. Display
 * arithmetic only — staleness judgment stays with the server (spec §2). */
function ageSeconds(generatedAt: string, receivedAt: string): number {
  return Math.max(0, (Date.parse(generatedAt) - Date.parse(receivedAt)) / 1000);
}

function rowProps(generatedAt: string, kind: string, v: KindView): EvidenceRowProps {
  const qualifiers: string[] = [];
  if (v.signature_state && v.signature_state !== "verified") {
    qualifiers.push(`signature: ${v.signature_state}`);
  }
  return {
    kind,
    status: v.status,
    evidence: v.evidence,
    receivedAt: v.received_at,
    qualifiers,
    microcopy: v.status === "green" ? undefined : microcopy(kind, v.status),
    // No next action here. The Board is the forensic view of one poll;
    // what to DO about a condition is composed once by the server and
    // shown on the case, which is also where it can be carried out. A
    // second copy of the wording in TSX is how the two drifted apart.
    ageSeconds: ageSeconds(generatedAt, v.received_at),
    cadenceSeconds: v.cadence_seconds,
  };
}

/** The node-card grid (PRD R1) — a pure projection of one poll. */
export function Board(p: { data: FleetStatus }) {
  return (
    <div className="rcpt-board">
      {p.data.nodes.map((n) => (
        <NodeCard
          key={n.node_id}
          nodeId={n.display_name || n.node_id}
          site={n.site}
          rollup={n.rollup}
          lastContact={undefined}
          kinds={Object.entries(n.kinds)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([kind, v]) => rowProps(p.data.generated_at, kind, v))}
        />
      ))}
      {p.data.nodes.length === 0 && (
        <p className="rcpt-empty">No nodes in your scope. Enrollment is push-only: a node appears when its reporter first pushes.</p>
      )}
    </div>
  );
}

/** The change feed (PRD R8/R11 landing view): state transitions between
 * consecutive polls, newest first, each with the full evidence row and an
 * explicit as-of stamp. Empty is honest: "no transitions since you opened
 * this page" is the answer to "anything new?". */
export function Feed(p: { entries: FeedEntry[]; sinceLabel: string }) {
  if (p.entries.length === 0) {
    return (
      <p className="rcpt-empty">
        No state transitions since {p.sinceLabel}. Quiet, and proven quiet —
        the board is one click away.
      </p>
    );
  }
  return (
    <div className="rcpt-feed">
      {p.entries.map((e, i) => (
        <div className="rcpt-feed-entry" key={`${e.nodeId}-${e.kind}-${e.asOf}-${i}`}>
          <div className="rcpt-feed-meta">
            <b>{e.nodeId}</b> · {e.kind} moved {e.from.toUpperCase()} →{" "}
            {e.to.toUpperCase()} <time dateTime={e.asOf}>as of {e.asOf}</time>
          </div>
          <EvidenceRow {...rowProps(e.asOf, e.kind, e.view)} />
        </div>
      ))}
    </div>
  );
}

/** Presentation-only diff of consecutive polls (spec §1b). */
export function diffPolls(prev: FleetStatus | null, next: FleetStatus): FeedEntry[] {
  if (!prev) return [];
  const out: FeedEntry[] = [];
  const prevByNode = new Map(prev.nodes.map((n) => [n.node_id, n]));
  for (const node of next.nodes) {
    const before = prevByNode.get(node.node_id);
    if (!before) continue;
    for (const [kind, view] of Object.entries(node.kinds)) {
      const was = before.kinds[kind]?.status;
      if (was && was !== view.status) {
        out.push({
          asOf: next.generated_at,
          nodeId: node.display_name || node.node_id,
          kind,
          from: was,
          to: view.status,
          view,
        });
      }
    }
  }
  return out;
}
