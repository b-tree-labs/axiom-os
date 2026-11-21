// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0
//
// "Sealed and packaged" on the receipts page (#1814's read, GET /api/v1/receipts/sealed): what was
// sealed or packaged, when, by whom and a link to the log; the digest one step away (DC-17). The
// rows are the server's, rendered verbatim; nothing here composes or judges them.

import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Surface } from "./App";
import { Sealed, fetchSealed, logHref } from "./Sealed";
import type { SealedRow } from "./types";

const SEAL: SealedRow = {
  what: "sealed",
  at: "2026-11-13T14:05:00+00:00",
  by: "@lead:site-a",
  ref: "anchor-1",
  logbook: "example_log",
  site: "site-a",
  link: "/log/example_log/records/seal-9",
  summary: "Sealed example_log at site-a",
  detail: { site: "site-a", logbook: "example_log", seal: "seal-9", seq: 9, from_seq: 1, to_seq: 8, digest: "ab12cd34ef" },
};
const PACKAGE: SealedRow = {
  what: "packaged",
  at: "2026-11-13T15:10:00+00:00",
  by: "@member:site-a",
  ref: "export-7",
  logbook: "example_log",
  site: "site-a",
  link: "/log/example_log/exports/export-7",
  summary: "Packaged example_log at site-a",
  detail: { site: "site-a", logbook: "example_log", counts: { chain: 9, selected: 4 }, digest: "99ff00ee" },
};

const html = (node: React.ReactElement) => renderToStaticMarkup(node);
/** What the page shows before anything is opened: the markup with every closed <details> body cut. */
const atRest = (s: string) => s.replace(/<details\b[^>]*>(<summary[^>]*>.*?<\/summary>).*?<\/details>/gs, "$1");

describe("Sealed and packaged (#1814's read on the receipts page)", () => {
  it("each row says what was sealed or packaged, when, by whom, with a link to the log", () => {
    const out = html(<Sealed state={{ rows: [PACKAGE, SEAL], unreachableSince: null }} />);
    expect(out).toContain("Packaged example_log at site-a");
    expect(out).toContain("Sealed example_log at site-a");
    expect(out).toContain("@member:site-a");
    expect(out).toContain("@lead:site-a");
    expect(out).toContain('dateTime="2026-11-13T14:05:00+00:00"');
    expect(out).toContain('href="#/log/example_log/seal-9"');
    expect(out).toContain('href="#/log/example_log"');
    expect(out.indexOf("Packaged")).toBeLessThan(out.indexOf("Sealed example_log")); // the server's order
  });

  it("the digest is one step away, never in the row at rest (DC-17)", () => {
    const out = html(<Sealed state={{ rows: [SEAL, PACKAGE], unreachableSince: null }} />);
    expect(out).toContain("ab12cd34ef");
    expect(out).toContain("99ff00ee");
    const rest = atRest(out);
    expect(rest).not.toContain("ab12cd34ef");
    expect(rest).not.toContain("99ff00ee");
    expect(rest).not.toContain("anchor-1");
  });

  it("says 'sealed' and 'packaged', never 'signed' (DC-21)", () => {
    const out = html(<Sealed state={{ rows: [SEAL, PACKAGE], unreachableSince: null }} />);
    expect(out).not.toMatch(/\bsign/i);
  });

  it("empty is one honest sentence and the way to make a row", () => {
    const out = html(<Sealed state={{ rows: [], unreachableSince: null }} />);
    expect(out).toContain("Nothing has been sealed or packaged yet");
    expect(out).toContain('href="#/log"');
  });

  it("a failed read says so and invents no rows; before the first read nothing is claimed", () => {
    const failed = html(<Sealed state={{ rows: null, unreachableSince: "2026-11-13T15:00:00Z" }} />);
    expect(failed).toContain("could not be read");
    expect(failed).not.toContain("Nothing has been sealed");
    const loading = html(<Sealed state={{ rows: null, unreachableSince: null }} />);
    expect(loading).not.toContain("Nothing has been sealed");
  });

  it("a row's log link is the app's route to the log, or to the sealing entry in it", () => {
    expect(logHref(SEAL)).toBe("#/log/example_log/seal-9");
    expect(logHref(PACKAGE)).toBe("#/log/example_log");
    expect(logHref({ ...SEAL, link: null })).toBe("#/log/example_log");
    expect(logHref({ ...SEAL, link: null, logbook: null })).toBe("#/log");
  });

  it("reads the route the server serves, same-origin, and gives null when it cannot", async () => {
    const seen: [string, RequestInit | undefined][] = [];
    const ok = (async (u: string, init?: RequestInit) => {
      seen.push([u, init]);
      return new Response(JSON.stringify({ rows: [SEAL] }));
    }) as unknown as typeof fetch;
    expect(await fetchSealed(ok)).toEqual([SEAL]);
    expect(seen[0][0]).toBe("/api/v1/receipts/sealed");
    expect(seen[0][1]?.credentials).toBe("same-origin");
    const refused = (async () => new Response("no", { status: 500 })) as unknown as typeof fetch;
    expect(await fetchSealed(refused)).toBeNull();
    const down = (async () => {
      throw new Error("down");
    }) as unknown as typeof fetch;
    expect(await fetchSealed(down)).toBeNull();
  });

  it("is one more view in the page's own toggle, beside Decide, Feed and Board", () => {
    const base = {
      state: { data: null, unreachableSince: null },
      briefState: { brief: null, unreachableSince: null },
      entries: [],
      openedAt: "2026-11-13T14:00:00Z",
      sealedState: { rows: [SEAL], unreachableSince: null },
    };
    const out = html(<Surface {...base} view="sealed" />);
    expect(out).toMatch(/role="tab"[^>]*>Sealed and packaged</);
    expect(out).toContain("Sealed example_log at site-a");
    const docket = html(<Surface {...base} view="docket" />);
    expect(docket).not.toContain("Sealed example_log at site-a");
  });
});
