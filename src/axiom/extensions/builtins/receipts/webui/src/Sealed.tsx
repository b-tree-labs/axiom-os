// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0
//
// Sealed and packaged: the receipts page's view of #1814's read, GET /api/v1/receipts/sealed. Each
// row is what was sealed or packaged, when, by whom and a link to the log; the digest and the
// rest of the row's detail sit one step away, in a closed Details (DC-17). Rows are the server's,
// verbatim and in its order (newest first). Producing a package is "packaged", never "signed"
// (DC-21). No design image exists for this view: it follows the page's own pattern, one more
// view in the Receipts toggle drawn with the feed's bordered rows.

import type { SealedRow, SealedState } from "./types";

/** The read, same-origin; null when the node cannot answer (the view then says so). */
export async function fetchSealed(fetchImpl: typeof fetch = fetch): Promise<SealedRow[] | null> {
  try {
    const r = await fetchImpl("/api/v1/receipts/sealed", { credentials: "same-origin" });
    if (!r.ok) return null;
    const body = await r.json();
    return Array.isArray(body?.rows) ? (body.rows as SealedRow[]) : null;
  } catch {
    return null;
  }
}

/** The app's route to the row's log: the sealing entry in it for a seal, else the log. */
export function logHref(row: SealedRow): string {
  const entry = row.link ? /^\/log\/([^/]+)\/records\/([^/?#]+)/.exec(row.link) : null;
  if (entry) return `#/log/${entry[1]}/${entry[2]}`;
  return row.logbook ? `#/log/${encodeURIComponent(row.logbook)}` : "#/log";
}

function when(at: string | null): string {
  if (!at) return "";
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return at;
  const p2 = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
}

/** The detail in words, the digest first; everything the row carries stays reachable. */
function Detail(p: { row: SealedRow }) {
  const d = p.row.detail ?? {};
  const facts: [string, string][] = [];
  if (d.digest != null) facts.push([p.row.what === "sealed" ? "Anchor root" : "Manifest digest", String(d.digest)]);
  if (p.row.ref) facts.push([p.row.what === "sealed" ? "Anchor" : "Package", p.row.ref]);
  if (d.from_seq != null || d.to_seq != null) facts.push(["Entries covered", `${d.from_seq ?? "?"} to ${d.to_seq ?? "?"}`]);
  const counts = d.counts as { chain?: number; selected?: number } | undefined;
  if (counts && typeof counts.selected === "number") {
    facts.push(["Entries", typeof counts.chain === "number" ? `${counts.selected} of ${counts.chain}` : String(counts.selected)]);
  }
  if (p.row.site) facts.push(["Site", p.row.site]);
  return (
    <details className="rcpt-sealed-more">
      <summary>Details</summary>
      <dl>
        {facts.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

export function Sealed(p: { state: SealedState }) {
  const { rows, unreachableSince } = p.state;
  if (rows === null) {
    return unreachableSince ? (
      <p className="rcpt-empty" role="alert">
        Sealed and packaged could not be read since {unreachableSince}. Nothing is shown rather than an old list.
      </p>
    ) : (
      <p className="rcpt-empty">Reading what was sealed and packaged…</p>
    );
  }
  if (rows.length === 0) {
    return (
      <p className="rcpt-empty">
        Nothing has been sealed or packaged yet. A log&rsquo;s Close out and its evidence packages appear
        here. <a href="#/log">Open Log</a>
      </p>
    );
  }
  return (
    <ol className="rcpt-sealed" aria-label="Sealed and packaged">
      {rows.map((r, i) => (
        <li className="rcpt-feed-entry rcpt-sealed-row" key={`${r.what}-${r.ref ?? i}`}>
          <div className="rcpt-sealed-line">
            <b>{r.summary}</b>
            <span className="rcpt-sealed-who">{r.by ?? "the system"}</span>
            {r.at ? <time dateTime={r.at}>{when(r.at)}</time> : null}
            <a className="rcpt-sealed-log" href={logHref(r)}>
              Open the log
            </a>
          </div>
          <Detail row={r} />
        </li>
      ))}
    </ol>
  );
}
