// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0
//
// Amber-must-sell microcopy (PRD R9): non-green states teach what green
// costs, face-up. Keyed (kind, status) with per-status fallbacks. Strings
// live here, beside the surface, and the can-fail suite asserts presence.

import type { ReceiptStatus } from "@axiom/appkit";

const BY_KIND_STATUS: Record<string, Partial<Record<ReceiptStatus, string>>> = {
  service_health: {
    unproven:
      "UNPROVEN means we refuse to guess. Healthy was claimed without a " +
      "measured latency — green requires latency_ms per service.",
  },
  backup: {
    unproven:
      "A backup was reported without an artifact we can cite. Green " +
      "requires the dump's path, size, and creation time.",
  },
};

const BY_STATUS: Partial<Record<ReceiptStatus, string>> = {
  unproven:
    "UNPROVEN means claimed without evidence — not failed. Green requires " +
    "the missing evidence, named in the row.",
  stale:
    "No report within 3× the declared cadence. Staleness is computed, " +
    "never assumed away — there is no last-known-green.",
  failed: "The evidence contradicts the claim. The receipt above says why.",
  unknown: "No judgment exists for this kind yet. Absence is not health.",
};

export function microcopy(kind: string, status: ReceiptStatus): string | undefined {
  return BY_KIND_STATUS[kind]?.[status] ?? BY_STATUS[status];
}
