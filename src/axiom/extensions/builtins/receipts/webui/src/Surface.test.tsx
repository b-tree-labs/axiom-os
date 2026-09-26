// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0
//
// The can-fail suite (spec §5): honest rendering against fixtures of every
// state, via static markup so assertions are about what is actually in the
// DOM. Includes the healthy control — the suite can pass as well as fail.

import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  App,
  Surface,
  Today,
  decideCase,
  fetchCaseDetail,
  mergeCaseDetail,
} from "./App";
import { diffPolls } from "./Board";
import type { FleetStatus } from "./types";
import { briefUnits, type BriefCase } from "@axiom/appkit";

const kind = (status: string, over: object = {}) => ({
  status: status as never,
  evidence: `evidence for ${status}`,
  received_at: "2026-09-23T12:00:00+00:00",
  signature_state: "unverified",
  cadence_seconds: 900,
  ...over,
});

const FIXTURE: FleetStatus = {
  generated_at: "2026-09-23T12:05:00+00:00",
  nodes: [
    {
      node_id: "node-a",
      site: "site-a",
      display_name: null,
      profile: "server",
      rollup: "failed",
      kinds: {
        backup: kind("green", { evidence: "artifact /b/x.dump, size_bytes=42" }),
        heartbeat: kind("stale"),
        service_health: kind("unproven"),
        canary: kind("failed"),
        versions: kind("unknown"),
      },
    },
  ],
};

const HEALTHY: FleetStatus = {
  generated_at: "2026-09-23T12:05:00+00:00",
  nodes: [
    {
      node_id: "node-b",
      site: "site-a",
      display_name: null,
      profile: "desktop",
      rollup: "green",
      kinds: { heartbeat: kind("green"), backup: kind("green") },
    },
  ],
};

const BRIEF = {
  generated_at: "2026-09-24T12:00:00+00:00",
  site: null,
  focus: null,
  needs_you: [
    {
      entity_kind: "node",
      entity_id: "node-a",
      claim_kind: "backup",
      status: "failed",
      evidence: "runner artifact missing",
      next_action: "check the artifact, then run backup with validation",
    },
  ],
  trust_deltas: [],
  quiet_line: "4 claims verified quietly.",
  counts: {
    green: 4,
    non_green: 1,
    needs_you_shown: 1,
    needs_you_overflow: 0,
    deltas: 0,
    cases: 1,
    cases_overflow: 0,
  },
  not_yet_wired: ["agent sessions", "decision roles", "schedules", "twins"],
  cases: [
    {
      case_id: "c-ab12cd34",
      site: "site-a",
      entity_kind: "node",
      entity_id: "node-a",
      title: "node-a · backup — claim contradicted",
      severity: "failed",
      items: [
        {
          entity_kind: "node",
          entity_id: "node-a",
          claim_kind: "backup",
          status: "failed",
          evidence: "runner artifact missing",
          next_action: "check the artifact, then run backup with validation",
        },
      ],
      proposal: ["check the artifact, then run backup with validation"],
    },
  ],
};

function board(data: FleetStatus | null, unreachableSince: string | null = null) {
  return renderToStaticMarkup(
    <Surface
      state={{ data, unreachableSince }}
      briefState={{ brief: BRIEF, unreachableSince: null }}
      entries={[]}
      view="board"
      openedAt="2026-09-23T12:00:00Z"
    />,
  );
}

const textOnly = (s: string) => s.replace(/<[^>]+>/g, " ");

describe("honest rendering", () => {
  it("UNPROVEN and FAILED are distinguishable with color stripped (R1)", () => {
    const text = textOnly(board(FIXTURE));
    expect(text).toContain("UNPROVEN");
    expect(text).toContain("FAILED");
    expect(text).toContain("?");
    expect(text).toContain("✕");
  });

  it("signature_state unverified shows as a face-up qualifier (R4)", () => {
    expect(board(FIXTURE)).toContain("signature: unverified");
  });

  it("fetch failure renders the unreachable banner and greys the board (R3)", () => {
    const out = board(FIXTURE, "2026-09-23T12:06:00+00:00");
    expect(out).toContain("Console unreachable since 2026-09-23T12:06:00+00:00");
    expect(out).toContain("rcpt-greyed");
  });

  it("with no data and no failure there is no stale-as-fresh anything", () => {
    const out = board(null);
    expect(out).toContain("Loading first poll");
    expect(out).not.toContain("axk-node");
  });

  it("a GREEN row shows its evidence text in the DOM (receipts face-up)", () => {
    expect(board(FIXTURE)).toContain("size_bytes=42");
  });

  it("amber rows carry microcopy naming what green costs (R9)", () => {
    expect(board(FIXTURE)).toContain("green requires latency_ms");
  });

  it("the board is evidence only — what to DO lives on the case (R13)", () => {
    // The wording used to exist twice: once in Python for the brief and
    // once hand-maintained as JSX for this board, and the two drifted.
    // The server composes it once now, and the case is where it is shown
    // — because the case is also where it can be carried out.
    const out = board(FIXTURE);
    expect(out).not.toContain("axk-evrow-action");
    expect(out).not.toContain("systemctl");
    // The evidence and the honest-taxonomy microcopy still render here.
    expect(out).toContain("axk-evrow-evidence");
    expect(out).toContain("axk-evrow-micro");
  });

  it("healthy control renders all-green — the suite can pass, not only fail", () => {
    const out = board(HEALTHY);
    expect(out).toContain("axk-pill-green");
    expect(out).not.toContain("axk-pill-failed");
    expect(out).not.toContain("axk-pill-unproven");
  });
});

describe("the brief projections render the payload verbatim (spec §1d)", () => {
  it("home opens with the brief: the CASE, its next action, the end mark", () => {
    // Today used to list claims while Decide listed cases — two
    // projections of one composer disagreeing about the unit, and two
    // different numbers on one screen. It lists cases now.
    const out = renderToStaticMarkup(
      <Today state={{ brief: BRIEF, unreachableSince: null }} />,
    );
    expect(out).toContain(BRIEF.cases![0].title);
    expect(out).toContain("check the artifact");
    expect(out).toContain("1 need");
    expect(out).toContain("— that&#x27;s everything —");
    expect(out).toContain("decision roles"); // plain names, never internal ids
    expect(out).not.toContain("seat");
    // The claim-level evidence is on the case page, one click in, not in
    // the opening message.
    expect(out).not.toContain("counterexample: runner artifact missing");
  });

  it("the Decide tab renders CASES — the decision units — with Discuss wired", () => {
    const out = renderToStaticMarkup(
      <Surface
        state={{ data: null, unreachableSince: null }}
        briefState={{ brief: BRIEF, unreachableSince: null }}
        entries={[]}
        view="docket"
        openedAt="2026-09-23T12:00:00Z"
        onAskCase={() => {}}
      />,
    );
    // The strip says what is WAITING, and says it once — the quiet
    // line beside it already carries how many checks passed.
    expect(out).toContain("waiting on you");
    expect(out).not.toContain("VERIFIED");
    expect(out).toContain("axk-case"); // CaseCard, not bare rows
    expect(out).toContain("node-a · backup — claim contradicted");
    // The chip used to read "1 claim · receipts attached" on every card:
    // a symptom count the cause chain has already collapsed, next to a
    // phrase that was true every time it rendered. Precedent is what is
    // left, because precedent is the part that changes a decision.
    expect(out).not.toContain("receipts attached");
    // "Ask" became "Discuss", and the button now says what there is to
    // discuss. The fixture's case carries no composed offer, so nothing
    // renders — which is the rule, not a gap.
    expect(out).not.toContain(">Ask<"); // wired handler renders; nothing else does
    expect(out).not.toContain("Approve"); // no executable proposal yet — no button
  });

  it("an unloaded brief renders the loading state, never an invented one", () => {
    const out = renderToStaticMarkup(
      <Today state={{ brief: null, unreachableSince: null }} />,
    );
    expect(out).toContain("Loading first poll");
    expect(out).not.toContain("axk-brief-end");
  });
});

describe("the wired base app (chat + identity present, not faked)", () => {
  it("with the engine wired, the shell offers New chat and the chat home", () => {
    const out = renderToStaticMarkup(<App />);
    expect(out).toContain("New chat");
    expect(out).toContain('href="#/decide"');
    expect(out).not.toContain("Select an account"); // single-tenant scope wired
    expect(out).toContain("Loading first poll"); // the brief slot, pre-poll
  });
});

describe("feed derivation is presentation-only (spec §1b)", () => {
  it("diffs consecutive polls into transitions with the as-of stamp", () => {
    const before: FleetStatus = {
      ...FIXTURE,
      nodes: [
        { ...FIXTURE.nodes[0], kinds: { ...FIXTURE.nodes[0].kinds, canary: kind("green") } },
      ],
    };
    const entries = diffPolls(before, FIXTURE);
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ kind: "canary", from: "green", to: "failed" });
    expect(entries[0].asOf).toBe(FIXTURE.generated_at);
  });

  it("first poll yields no entries — nothing is invented", () => {
    expect(diffPolls(null, FIXTURE)).toHaveLength(0);
  });
});

describe("opening a case consults the per-case endpoint", () => {
  const ROW: BriefCase = BRIEF.cases![0] as BriefCase;

  it("asks for THIS case by id, url-encoded", async () => {
    const seen: string[] = [];
    const stub = (async (url: string) => {
      seen.push(url);
      return { ok: true, json: async () => ({ case: { ...ROW, blast: null } }) };
    }) as unknown as typeof fetch;
    await fetchCaseDetail("c-ab12cd34", stub);
    expect(seen).toEqual(["/api/v1/receipts/case/c-ab12cd34"]);
  });

  it("unwraps the envelope the endpoint actually returns", async () => {
    const stub = (async () => ({
      ok: true,
      json: async () => ({ case: { ...ROW, blast: { systems: ["api"], schedules: [], people: [], sources: ["1 service(s) this node reports"], declared: true, summary: "1 system" } } }),
    })) as unknown as typeof fetch;
    const detail = await fetchCaseDetail("c-ab12cd34", stub);
    expect(detail?.blast?.summary).toBe("1 system");
  });

  it("a refusal or a network failure leaves the reader with the row, not a blank", async () => {
    const refused = (async () => ({ ok: false, json: async () => ({}) })) as unknown as typeof fetch;
    expect(await fetchCaseDetail("c-ab12cd34", refused)).toBeNull();
    const broken = (async () => {
      throw new Error("offline");
    }) as unknown as typeof fetch;
    expect(await fetchCaseDetail("c-ab12cd34", broken)).toBeNull();
    expect(mergeCaseDetail(ROW, null)).toBe(ROW);
  });

  it("the detail adds reach WITHOUT dropping what the row already said", () => {
    const detail = {
      ...ROW,
      proposal: undefined as never,
      blast: { systems: ["api"], schedules: [], people: ["@node:site"], sources: ["1 service(s) this node reports"], declared: true, summary: "1 system · 1 person" },
    } as BriefCase;
    const merged = mergeCaseDetail(ROW, detail);
    expect(merged.blast?.summary).toBe("1 system · 1 person");
    expect(merged.proposal).toEqual(ROW.proposal); // the row's words survive
  });

  it("a detail for a DIFFERENT case is ignored", () => {
    const other = { ...ROW, case_id: "c-99999999", blast: null } as BriefCase;
    expect(mergeCaseDetail(ROW, other)).toBe(ROW);
  });
});

describe("deciding a case records it and shows what was recorded", () => {
  const ROW: BriefCase = BRIEF.cases![0] as BriefCase;

  it("posts the choice and NEVER the decider", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    const stub = (async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      return { ok: true, json: async () => ({ case: { ...ROW, options: null } }) };
    }) as unknown as typeof fetch;
    await decideCase("c-ab12cd34", "hold", { note: "checking it", fetchImpl: stub });
    const [url, init] = calls[0];
    expect(url).toBe("/api/v1/receipts/case/c-ab12cd34/decide");
    expect(init?.method).toBe("POST");
    const body = JSON.parse(String(init?.body));
    expect(body).toEqual({ chosen: "hold", note: "checking it" });
    // The decider is the SESSION's, never the client's claim about it.
    expect(Object.keys(body)).not.toContain("decider");
  });

  it("returns the case AS RECORDED, by re-reading it after the write", async () => {
    const urls: string[] = [];
    const stub = (async (url: string) => {
      urls.push(url);
      return {
        ok: true,
        json: async () => ({
          case: { ...ROW, options: null, verdict: { chosen: "hold", decider: "@ben:site-a" } },
        }),
      };
    }) as unknown as typeof fetch;
    const after = await decideCase("c-ab12cd34", "hold", { fetchImpl: stub });
    expect(urls).toEqual([
      "/api/v1/receipts/case/c-ab12cd34/decide",
      "/api/v1/receipts/case/c-ab12cd34",
    ]);
    expect(after?.verdict?.chosen).toBe("hold");
    expect(after?.options).toBeNull(); // decided: nothing further to decide here
  });

  it("a refused write leaves the case undecided rather than faking it", async () => {
    const refused = (async () => ({ ok: false, json: async () => ({}) })) as unknown as typeof fetch;
    expect(await decideCase("c-ab12cd34", "hold", { fetchImpl: refused })).toBeNull();
    const offline = (async () => {
      throw new Error("offline");
    }) as unknown as typeof fetch;
    expect(await decideCase("c-ab12cd34", "hold", { fetchImpl: offline })).toBeNull();
  });
});

describe("the opening screen and the rail count the same thing", () => {
  const brief = BRIEF;

  it("Today headlines the case count, not the claim count", () => {
    const out = renderToStaticMarkup(<Today state={{ brief, unreachableSince: null }} />);
    // BRIEF has 1 case made of 1 claim, so both agree here; the guard is
    // that Today reads the same helper the rail does.
    expect(briefUnits(brief).unit).toBe("case");
    expect(out).toContain(`${briefUnits(brief).needs} need`);
  });

  it("Today lists the case, not the claims inside it", () => {
    const out = renderToStaticMarkup(<Today state={{ brief, unreachableSince: null }} />);
    expect(out).toContain("axk-case-title");
  });

  it("a case opened from Today is handed to Decide by id", () => {
    const seen: string[] = [];
    const out = renderToStaticMarkup(
      <Today
        state={{ brief, unreachableSince: null }}
        onOpenCase={(c) => seen.push(c.case_id)}
      />,
    );
    // The affordance renders only where a handler backs it.
    expect(out).toContain("axk-case-open");
    const without = renderToStaticMarkup(<Today state={{ brief, unreachableSince: null }} />);
    expect(without).not.toContain("axk-case-open");
  });
});
