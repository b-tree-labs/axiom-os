// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0
//
// The Receipts surface inside appkit's DefaultApp (design v21, founder-
// accepted): home is the daily thread OPENING with the brief (BriefToday);
// Decide is the docket (BriefDocket) with the feed and the board one tab
// deep as forensics. Everything renders the server-composed payload
// verbatim — one composer, three projections (spec §1d) — and the chat
// verb stays DefaultApp's honest no-engine state until the node's chat
// engine is wired (never faked).

import React from "react";
import {
  BriefDocket,
  BriefToday,
  CaseDetail,
  ChatInterface,
  DefaultApp,
  createAxiomChatEngine,
  briefUnits,
  useDefaultAppChat,
  useGateUser,
  type BriefCase,
  type BriefPayload,
  type EntityProfile,
} from "@axiom/appkit";
import { Board, Feed, diffPolls } from "./Board";
import type { FeedEntry, FleetStatus } from "./types";

const POLL_BASE_MS = 45_000;

declare global {
  interface Window {
    __AXIOM_BRAND__?: { product_name: string; accent: string };
  }
}

export interface FetchState {
  data: FleetStatus | null;
  unreachableSince: string | null;
}

export interface BriefState {
  brief: BriefPayload | null;
  unreachableSince: string | null;
}

function Unreachable(p: { since: string }) {
  return (
    <div className="rcpt-unreachable" role="alert">
      Console unreachable since {p.since}. Nothing below is fresh; statuses
      shown are the last successful poll, greyed.
    </div>
  );
}

/** The daily thread's opening message: the brief, verbatim. */
export function Today(p: {
  state: BriefState;
  onOpenCase?: (c: BriefCase) => void;
  onDecideCase?: (c: BriefCase, chosen: string) => void;
}) {
  const { brief, unreachableSince } = p.state;
  return (
    <div className="rcpt-surface">
      {unreachableSince && <Unreachable since={unreachableSince} />}
      <div className={unreachableSince ? "rcpt-greyed" : undefined}>
        {brief === null ? (
          !unreachableSince && <p className="rcpt-empty">Loading first poll…</p>
        ) : (
          <BriefToday
            brief={brief}
            onOpenCase={p.onOpenCase}
            onDecideCase={p.onDecideCase}
          />
        )}
      </div>
    </div>
  );
}

/** Home = the chat thread OPENING with the brief (the accepted design):
 * the real ChatInterface (centered) with today's brief as the centered
 * content above the composer. */
export function ChatHome(p: {
  state: BriefState;
  onOpenCase?: (c: BriefCase) => void;
  onDecideCase?: (c: BriefCase, chosen: string) => void;
}) {
  return (
    <ChatInterface
      presentation="centered"
      home
      greeting="Today"
      selectedAccountId="axiom"
      centeredExtras={
        <Today
          state={p.state}
          onOpenCase={p.onOpenCase}
          onDecideCase={p.onDecideCase}
        />
      }
    />
  );
}

/** The docket row is a SUMMARY. Reach ("what else this touches") and the
 * live verdict are composed per case, on demand, because computing reach
 * means reading declared edges for that one entity — work the list must
 * not do for every row. So opening a case is a second request, and a page
 * that skips it silently drops everything the endpoint exists to add.
 *
 * Returns null when the endpoint cannot answer; the caller then keeps
 * showing the row it already has rather than emptying the page.
 */
export async function fetchCaseDetail(
  caseId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<BriefCase | null> {
  try {
    const r = await fetchImpl(`/api/v1/receipts/case/${encodeURIComponent(caseId)}`, {
      credentials: "same-origin",
    });
    if (!r.ok) return null;
    const body = await r.json();
    const detail = body?.case ?? body;
    return detail && detail.case_id ? (detail as BriefCase) : null;
  } catch {
    return null;
  }
}

/** The detail wins where it speaks, and the row survives where it does not.
 * A detail that arrived without reach must not blank a row that had it. */
export function mergeCaseDetail(row: BriefCase, detail: BriefCase | null): BriefCase {
  if (!detail || detail.case_id !== row.case_id) return row;
  const merged = { ...row } as unknown as Record<string, unknown>;
  for (const [k, v] of Object.entries(detail)) {
    if (v !== null && v !== undefined) merged[k] = v;
  }
  return merged as unknown as BriefCase;
}

/** Record a decision about a case.
 *
 * The decider is NOT sent — the server takes it from the session, so a
 * decision cannot be attributed to someone who did not make it. The
 * body carries only the choice (and a note, once the surface collects
 * one).
 *
 * Returns the case as the server sees it AFTER the write, so the page
 * shows the decision that was actually recorded rather than the one the
 * button believed it was making. ``null`` means the write did not
 * happen and the caller leaves the case undecided on screen.
 */
export async function decideCase(
  caseId: string,
  chosen: string,
  opts: { note?: string; fetchImpl?: typeof fetch } = {},
): Promise<BriefCase | null> {
  const doFetch = opts.fetchImpl ?? fetch;
  try {
    const r = await doFetch(`/api/v1/receipts/case/${encodeURIComponent(caseId)}/decide`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ chosen, note: opts.note ?? "" }),
    });
    if (!r.ok) return null;
  } catch {
    return null;
  }
  return fetchCaseDetail(caseId, doFetch);
}

/** What the node knows about one named thing. null = nothing recorded,
 * which the modal states; the surface never invents a profile. */
export async function loadEntity(
  kind: string,
  id: string,
  fetchImpl: typeof fetch = fetch,
): Promise<EntityProfile | null> {
  try {
    const r = await fetchImpl(
      `/api/v1/receipts/entity/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`,
      { credentials: "same-origin" },
    );
    if (!r.ok) return null;
    const body = await r.json();
    return (body?.entity as EntityProfile) ?? null;
  } catch {
    return null;
  }
}

/** Ask on a case opens the chat PANEL beside the case (the Field Hand
 * pattern) — the case detail stays on screen; asking never navigates
 * away from what you were deciding about. */
function useAskInPanel() {
  const chat = useDefaultAppChat();
  return React.useCallback(
    (c: BriefCase) => {
      const seed =
        `About case ${c.case_id} — ${c.title}. ` +
        `Claims: ${c.items.map((i) => `${i.claim_kind} is ${i.status}`).join("; ")}. `;
      chat?.openPanel(seed);
    },
    [chat],
  );
}

/** The Decide surface: docket first; feed and board are the forensic
 * drill-down, one tab deep. Fully prop-driven for the can-fail suite. */
export function Surface(p: {
  state: FetchState;
  briefState: BriefState;
  entries: FeedEntry[];
  view: "docket" | "feed" | "board";
  onView?: (v: "docket" | "feed" | "board") => void;
  openedAt: string;
  /** override Ask (default: open the chat panel beside the case) */
  onAskCase?: (c: BriefCase) => void;
  /** test seam: how a case's detail is fetched when one is opened. */
  loadCase?: (caseId: string) => Promise<BriefCase | null>;
  /** test seam: how a decision is recorded. */
  decide?: (caseId: string, chosen: string) => Promise<BriefCase | null>;
  /** called after a decision is recorded, so the docket can refresh. */
  onDecided?: () => void;
  /** a case another screen asked to open (the opening screen hands one
   * over when a card there is clicked). Surface still owns which case is
   * open — this is a request, not a second source of truth. */
  openCaseId?: string | null;
  /** fired once the request above has been honoured, so it can be cleared
   * and the same case can be handed over again later. */
  onOpened?: () => void;
}) {
  const { data, unreachableSince } = p.state;
  const [openCase, setOpenCase] = React.useState<BriefCase | null>(null);
  const load = p.loadCase ?? ((id: string) => fetchCaseDetail(id));
  // Opening a case asks the server for the per-case composition (reach,
  // live verdict). Until it answers, the row already in hand is shown —
  // never a spinner over information the reader already had.
  React.useEffect(() => {
    if (!openCase) return;
    let live = true;
    void load(openCase.case_id).then((detail) => {
      if (!live || !detail) return;
      setOpenCase((current) =>
        current && current.case_id === detail.case_id ? mergeCaseDetail(current, detail) : current,
      );
    });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openCase?.case_id]);
  // A case handed over from another screen. Looked up in the brief we
  // already have, so the hand-off costs no extra request; the detail
  // fetch above then fills in reach and the live verdict as usual.
  React.useEffect(() => {
    if (!p.openCaseId) return;
    const found = (p.briefState.brief?.cases ?? []).find(
      (c) => c.case_id === p.openCaseId,
    );
    if (found) setOpenCase(found);
    p.onOpened?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.openCaseId, p.briefState.brief]);
  const askInPanel = useAskInPanel();
  const onAsk = p.onAskCase ?? askInPanel;
  const recordDecision = p.decide ?? ((id: string, chosen: string) => decideCase(id, chosen));
  // A decision replaces the case on screen with the case AS RECORDED. A
  // write that did not happen changes nothing: the case stays undecided
  // and its buttons stay live, rather than showing a decision the server
  // never took.
  const onDecide = React.useCallback(
    (c: BriefCase, chosen: string) => {
      void recordDecision(c.case_id, chosen).then((decided) => {
        if (!decided) return;
        setOpenCase((current) =>
          current && current.case_id === decided.case_id ? decided : current,
        );
        p.onDecided?.();
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [p.decide, p.onDecided],
  );
  const labels = { docket: "Decide", feed: "Feed", board: "Board" } as const;
  return (
    <div className="rcpt-surface">
      <div className="rcpt-toggle" role="tablist" aria-label="Receipts view">
        {(["docket", "feed", "board"] as const).map((v) => (
          <button
            key={v}
            role="tab"
            aria-selected={p.view === v}
            className={p.view === v ? "on" : undefined}
            onClick={() => p.onView?.(v)}
          >
            {labels[v]}
          </button>
        ))}
      </div>
      {unreachableSince && <Unreachable since={unreachableSince} />}
      <div className={unreachableSince ? "rcpt-greyed" : undefined}>
        {p.view === "docket" ? (
          p.briefState.brief === null ? (
            !p.briefState.unreachableSince && (
              <p className="rcpt-empty">Loading first poll…</p>
            )
          ) : openCase ? (
            <CaseDetail
              case={openCase}
              onBack={() => setOpenCase(null)}
              onAsk={onAsk}
              onDecide={onDecide}
              loadEntity={(k, i) => loadEntity(k, i)}
            />
          ) : (
            <BriefDocket
              brief={p.briefState.brief}
              onOpenCase={setOpenCase}
              onAskCase={onAsk}
              onDecideCase={onDecide}
              loadEntity={(k, i) => loadEntity(k, i)}
            />
          )
        ) : data === null ? (
          !unreachableSince && <p className="rcpt-empty">Loading first poll…</p>
        ) : p.view === "feed" ? (
          <Feed entries={p.entries} sinceLabel={p.openedAt} />
        ) : (
          <Board data={data} />
        )}
      </div>
    </div>
  );
}

/** Polling container inside the DefaultApp frame. */
export function App() {
  const [state, setState] = React.useState<FetchState>({ data: null, unreachableSince: null });
  const [briefState, setBriefState] = React.useState<BriefState>({
    brief: null,
    unreachableSince: null,
  });
  const [entries, setEntries] = React.useState<FeedEntry[]>([]);
  const [view, setView] = React.useState<"docket" | "feed" | "board">("docket");
  const openedAt = React.useRef(new Date().toISOString()).current;
  const latest = React.useRef<FleetStatus | null>(null);

  // One place fetches the brief: the poll, and anything that changes it.
  // A decision removes a case from the brief (decided cases are not what
  // needs you), so the screen the decision was taken on has to re-read.
  const refreshBrief = React.useCallback(async () => {
    try {
      const r = await fetch("/api/v1/receipts/today", { credentials: "same-origin" });
      if (!r.ok) throw new Error(String(r.status));
      const body = (await r.json()) as { brief: BriefPayload };
      setBriefState({ brief: body.brief, unreachableSince: null });
    } catch {
      setBriefState((cur) => ({
        brief: cur.brief,
        unreachableSince: cur.unreachableSince ?? new Date().toISOString(),
      }));
    }
  }, []);

  // A decision taken anywhere records the same way and has the same
  // consequence, so both screens call this rather than each owning a
  // copy of "post, then work out what changed".
  const decideFromAnywhere = React.useCallback(
    (c: BriefCase, chosen: string) => {
      void decideCase(c.case_id, chosen).then((decided) => {
        if (decided) void refreshBrief();
      });
    },
    [refreshBrief],
  );

  React.useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const r = await fetch("/api/v1/fleet/status", { credentials: "same-origin" });
        if (!r.ok) throw new Error(String(r.status));
        const next = (await r.json()) as FleetStatus;
        if (!live) return;
        const changes = diffPolls(latest.current, next);
        latest.current = next;
        if (changes.length) setEntries((cur) => [...changes, ...cur].slice(0, 200));
        setState({ data: next, unreachableSince: null });
      } catch {
        if (!live) return;
        setState((cur) => ({
          data: cur.data,
          unreachableSince: cur.unreachableSince ?? new Date().toISOString(),
        }));
      }
      await refreshBrief();
      timer = setTimeout(tick, POLL_BASE_MS + Math.random() * 10_000);
    };
    tick();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, []);

  const brand = typeof window !== "undefined" ? window.__AXIOM_BRAND__ : undefined;
  // The rail badge counts what the brief counts. It used to read
  // counts.cases while Today headlined counts.non_green, so one screen
  // showed two different numbers for the same situation.
  const needs = briefState.brief ? briefUnits(briefState.brief).needs : 0;
  const [handOff, setHandOff] = React.useState<string | null>(null);

  // The node's ONE chat agent over /api/v1/chat (C2), and the gate's
  // logged-in identity (/gate/me) for the rail card + authed chat loads.
  const engine = React.useMemo(() => createAxiomChatEngine({}), []);
  const gateUser = useGateUser();

  return (
    <DefaultApp
      logo={<b>{brand?.product_name ?? "Axiom"}</b>}
      engine={engine}
      user={gateUser ?? undefined}
      // Only wired verbs ride the rail (founder rule: no placeholders):
      // Library returns when the library primitive has real content here.
      verbs={{ omit: ["library"] }}
      accountId="axiom"
      badges={needs > 0 ? { decide: needs } : undefined}
      home={
        <ChatHome
          state={briefState}
          onDecideCase={decideFromAnywhere}
          onOpenCase={(c) => {
            // Hand the case to Decide and go there. DefaultApp reads the
            // active verb from the hash, so navigation is a hash change
            // rather than a new prop on somebody else's component.
            setHandOff(c.case_id);
            setView("docket");
            if (typeof window !== "undefined") window.location.hash = "#/decide";
          }}
        />
      }
      views={{
        decide: (
          <Surface
            state={state}
            briefState={briefState}
            entries={entries}
            view={view}
            onView={setView}
            openedAt={openedAt}
            openCaseId={handOff}
            onOpened={() => setHandOff(null)}
            onDecided={refreshBrief}
          />
        ),
      }}
    />
  );
}
