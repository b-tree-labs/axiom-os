# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`/compare <providerA> <providerB> <prompt>`: one prompt, two models, side by side (ADR-168).

Both providers receive the same system prompt and the same messages, built once, so a difference in the
answers is a difference in the models and not in what they were shown. The session's active provider is
never touched, and the comparison is not added to the transcript as ordinary turns: it is recorded as one
marked entry in ``session.context["comparisons"]``.

The tier rule is the one ``/model`` enforces (``llm.tier_policy``), applied to BOTH providers before either
is called, against the stricter of what the session already holds and what this prompt classifies as. A
provider that may not receive the content refuses the whole comparison, with the reason: the other side is
not run alone, because that would send the content somewhere and print half a comparison.

``compare(...)`` is the function the command calls; an agent or an MCP tool can call it directly and read
the dataclass it returns. ``analyze_delta`` is deterministic and makes no model call.
"""

from __future__ import annotations

import copy
import dataclasses
import difflib
import json
import re
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from axiom.llm.placement import placement_of
from axiom.llm.tier_policy import CONTROLLED, admits, decide_switch

from .. import where
from ..command_registry import CliSpec, command
from ..follow_ups import CommandResult, FollowUp, pick, with_json_follow_ups

DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_TOKENS = 2000
#: An answer longer than this is cut in the printed form (never in ``--json``) unless ``--full``.
DISPLAY_LIMIT_CHARS = 4000

COMPARISONS_KEY = "comparisons"


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


# ---- results ------------------------------------------------------------------------------------


@dataclass
class Side:
    """What happened with one provider."""

    provider: str
    model: str = ""
    placement: str = ""  # LOCAL / CLOUD host / REMOTE host (a trailing ? marks a guess)
    status: str = "not-run"  # ok | error | timeout | refused | not-run
    text: str = ""
    error: str = ""
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None  # None = unpriced (no price known for this model)
    tier: str = ""


@dataclass
class Judgement:
    provider: str
    status: str = "not-run"  # ok | error | timeout | refused
    text: str = ""  # the judge model's opinion; NOT a computed fact
    error: str = ""
    placement: str = ""
    model: str = ""


@dataclass
class Delta:
    words_a: int
    words_b: int
    chars_a: int
    chars_b: int
    length_ratio: float | None  # words in B / words in A; None when A is empty
    similarity: float  # Jaccard of word 3-shingles, 0..1
    sentences_a: int
    sentences_b: int
    matched_sentences: int  # sentences of A with a near-identical sentence in B
    shared_terms: list[str]
    only_a_terms: list[str]
    only_b_terms: list[str]
    numbers_only_a: list[str]
    numbers_only_b: list[str]
    numeric_disagreements: list[dict[str, str]]  # {"context", "a", "b"}
    citations_a: list[str]
    citations_b: list[str]
    refusal_a: list[str]
    refusal_b: list[str]
    hedges_a: list[str]
    hedges_b: list[str]


@dataclass
class CompareResult:
    prompt: str
    ran: bool  # False when the comparison was refused before any call
    session_tier: str
    prompt_tier: str
    effective_tier: str
    a: Side
    b: Side
    refusals: list[dict[str, str]] = field(default_factory=list)  # {"provider", "reason"}
    delta: Delta | None = None
    judge: Judgement | None = None
    context_note: str = ""

    @property
    def comparable(self) -> bool:
        return self.a.status == "ok" and self.b.status == "ok"


# ---- the delta ----------------------------------------------------------------------------------

_STOP_WORDS = (
    "about above after again also because been before being between both could does doing down during "
    "each from further have having here herself himself into itself just more most myself once only other "
    "ours over same should some such than that their theirs them then there these they this those through "
    "under until very were what when where which while with would your yours will shall must can may might "
    "its it's the and for are but not you all any her his how our out she was who why"
)
_STOP = frozenset(_STOP_WORDS.split())
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")
_CITATION = re.compile(
    r"\[(?:C|S|Source\s*)?\d+(?:\s*,\s*\d+)*\]|https?://[^\s)\]>]+|\b10\.\d{4,9}/\S+"
)
_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?%?")
_LIST_PREFIX = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*•])\s+")
_REFUSAL = (
    "i can't",
    "i cannot",
    "i can not",
    "i'm unable",
    "i am unable",
    "i'm not able",
    "i am not able",
    "i won't",
    "i will not",
    "unable to help",
    "can't help with",
    "cannot assist",
    "can't assist",
    "i'm sorry, but",
    "i do not have access",
    "i don't have access",
)
_HEDGES = (
    "i think",
    "i believe",
    "i'm not sure",
    "i am not sure",
    "not certain",
    "it depends",
    "possibly",
    "perhaps",
    "probably",
    "might",
    "may be",
    "could be",
    "approximately",
    "roughly",
    "around",
    "as an ai",
    "i don't know",
    "i do not know",
)
_MAX_LISTED = 15


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text.lower())


def _shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    w = _words(text)
    if len(w) < n:
        return {tuple(w)} if w else set()
    return {tuple(w[i : i + n]) for i in range(len(w) - n + 1)}


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip() for p in parts if len(p.strip()) > 2]


def _key_terms(text: str) -> Counter:
    return Counter(w.lower().strip("'-") for w in _WORD.findall(text) if w.lower() not in _STOP)


def _top(terms: Counter, names: set[str]) -> list[str]:
    ranked = sorted(names, key=lambda t: (-terms[t], t))
    return ranked[:_MAX_LISTED]


def _norm_number(raw: str) -> str:
    pct = raw.endswith("%")
    body = raw.rstrip("%").replace(",", "")
    try:
        value = float(body)
    except ValueError:
        return raw
    text = f"{value:g}" if abs(value) < 1e15 else body
    return text + ("%" if pct else "")


def _numbers(text: str) -> list[tuple[str, str]]:
    """``(normalised value, context)`` for every number. The context is the (up to) three words before it,
    which is how a value in one answer is lined up with the value for the same thing in the other."""
    cleaned = _LIST_PREFIX.sub("", _CITATION.sub(" ", text))
    out: list[tuple[str, str]] = []
    for m in _NUMBER.finditer(cleaned):
        before = re.findall(r"[A-Za-z]+", cleaned[max(0, m.start() - 60) : m.start()].lower())
        before = [w for w in before if w not in _STOP][-2:]
        after = re.findall(r"[A-Za-z%°]+", cleaned[m.end() : m.end() + 20].lower())
        unit = after[0] if after and after[0] not in _STOP else ""
        out.append((_norm_number(m.group()), " ".join([*before, "#", unit]).strip()))
    return out


def _markers(text: str, phrases: tuple[str, ...]) -> list[str]:
    low = text.lower()
    return [p for p in phrases if re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", low)]


def analyze_delta(a_text: str, b_text: str) -> Delta:
    """What differs between two answers, computed without a model. Every figure is a count or a set taken
    from the two texts; none is an estimate."""
    wa, wb = _words(a_text), _words(b_text)
    sa, sb = _shingles(a_text), _shingles(b_text)
    union = sa | sb
    similarity = (len(sa & sb) / len(union)) if union else 1.0

    sents_a, sents_b = _sentences(a_text), _sentences(b_text)
    norm_b = [" ".join(_words(s)) for s in sents_b]
    matched = 0
    for s in sents_a:
        ns = " ".join(_words(s))
        if any(difflib.SequenceMatcher(None, ns, nb).ratio() >= 0.8 for nb in norm_b):
            matched += 1

    ta, tb = _key_terms(a_text), _key_terms(b_text)
    both = set(ta) & set(tb)
    merged = ta + tb

    na, nb_ = _numbers(a_text), _numbers(b_text)
    vals_a, vals_b = {v for v, _ in na}, {v for v, _ in nb_}
    disagreements: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    by_ctx_b: dict[str, set[str]] = {}
    for v, c in nb_:
        by_ctx_b.setdefault(c, set()).add(v)
    for v, c in na:
        if c in by_ctx_b and v not in by_ctx_b[c]:
            for other in sorted(by_ctx_b[c]):
                if other in vals_a:
                    continue  # B's value also appears in A: not a disagreement about this figure
                key = (c, v, other)
                if key not in seen:
                    seen.add(key)
                    disagreements.append(
                        {"context": c.replace("#", "_").strip(), "a": v, "b": other}
                    )

    # Second pass: values the context did not line up, matched by unit ("40 psi" vs "55 psi") in order.
    paired_a = {d["a"] for d in disagreements}
    paired_b = {d["b"] for d in disagreements}
    for unit in dict.fromkeys(c.rsplit("#", 1)[1].strip() for _, c in na if "#" in c):
        if not unit:
            continue
        ua = [v for v, c in na if c.endswith("# " + unit) and v not in vals_b and v not in paired_a]
        ub = [
            v for v, c in nb_ if c.endswith("# " + unit) and v not in vals_a and v not in paired_b
        ]
        for va, vb in zip(dict.fromkeys(ua), dict.fromkeys(ub), strict=False):
            disagreements.append({"context": f"in {unit}", "a": va, "b": vb})

    def cites(t: str) -> list[str]:
        return sorted(dict.fromkeys(m.group().strip(".,;") for m in _CITATION.finditer(t)))

    return Delta(
        words_a=len(wa),
        words_b=len(wb),
        chars_a=len(a_text),
        chars_b=len(b_text),
        length_ratio=(len(wb) / len(wa)) if wa else None,
        similarity=similarity,
        sentences_a=len(sents_a),
        sentences_b=len(sents_b),
        matched_sentences=matched,
        shared_terms=_top(merged, both),
        only_a_terms=_top(ta, set(ta) - set(tb)),
        only_b_terms=_top(tb, set(tb) - set(ta)),
        numbers_only_a=sorted(vals_a - vals_b)[:_MAX_LISTED],
        numbers_only_b=sorted(vals_b - vals_a)[:_MAX_LISTED],
        numeric_disagreements=disagreements[:_MAX_LISTED],
        citations_a=cites(a_text),
        citations_b=cites(b_text),
        refusal_a=_markers(a_text, _REFUSAL),
        refusal_b=_markers(b_text, _REFUSAL),
        hedges_a=_markers(a_text, _HEDGES),
        hedges_b=_markers(b_text, _HEDGES),
    )


# ---- running one provider -----------------------------------------------------------------------


def _cost(model: str, tokens_in: int | None, tokens_out: int | None) -> float | None:
    """USD from the price table, or None when no price is known for the model (never 0.0 for 'unknown')."""
    if not model or tokens_in is None or tokens_out is None:
        return None
    from axiom.extensions.builtins.chat import usage

    known = model in usage._COST_TABLE or any(
        model.startswith(k) or k.startswith(model) for k in usage._COST_TABLE
    )
    return usage.compute_cost(model, tokens_in, tokens_out) if known else None


def _default_caller(gateway: Any) -> Callable[..., Any]:
    """One call to ONE named provider through the gateway's own wire code. The session's override, the
    gateway's ``last_selected`` and its provider order are left exactly as they were."""

    def call(provider, messages, system, max_tokens):
        return gateway._call_provider_with_tools(provider, messages, system, None, max_tokens)

    return call


def _audit(session_id: str, prompt: str, tier: str, provider: str, reason: str) -> None:
    try:
        from axiom.infra.routing_audit import hash_query, log_routing_decision

        log_routing_decision(
            session_id=session_id,
            query_hash=hash_query(prompt),
            tier=tier,
            classifier="compare",
            provider=provider,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 - the audit is best-effort, as it is for every other call
        pass


def _run_one(gateway: Any, provider: Any, messages, system, max_tokens, caller, timeout) -> Side:
    side = Side(
        provider=provider.name,
        model=provider.model,
        placement=placement_of(provider).label(),
        tier=provider.routing_tier,
    )
    if getattr(provider, "requires_vpn", False):
        try:
            if not gateway._check_vpn(provider):
                side.status, side.error = "error", "VPN endpoint unreachable"
                return side
        except Exception:  # noqa: BLE001
            pass
    started = time.monotonic()

    def work():
        return caller(provider, messages, system, max_tokens)

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(work)
    try:
        resp = future.result(timeout=timeout)
    except FutureTimeout:
        side.status = "timeout"
        side.latency_ms = (time.monotonic() - started) * 1000
        side.error = f"no response within {timeout:g}s"
        return side
    except Exception as exc:  # noqa: BLE001 - the reason is reported, never swallowed
        side.status = "error"
        side.latency_ms = (time.monotonic() - started) * 1000
        try:
            from axiom.llm.gateway import _classify_http_error

            side.error = str(_classify_http_error(exc, provider=provider.name))
        except Exception:  # noqa: BLE001
            side.error = f"{type(exc).__name__}: {exc}"
        return side
    finally:
        pool.shutdown(wait=False)
    side.latency_ms = (time.monotonic() - started) * 1000
    if not getattr(resp, "success", True):
        side.status, side.error = (
            "error",
            getattr(resp, "error", None) or "provider reported failure",
        )
        return side
    side.status = "ok"
    side.text = getattr(resp, "text", "") or ""
    tin, tout = getattr(resp, "input_tokens", 0) or 0, getattr(resp, "output_tokens", 0) or 0
    if tin or tout:
        side.input_tokens, side.output_tokens = tin, tout
    side.cost_usd = _cost(provider.model, side.input_tokens, side.output_tokens)
    if side.status == "ok" and not side.text.strip():
        side.status, side.error = "error", "the provider returned an empty answer"
    return side


def _judge_prompt(prompt: str, a: Side, b: Side) -> str:
    return (
        "Two assistants answered the same question. Summarise the SUBSTANTIVE differences between "
        "their answers: claims one makes that the other does not, claims that contradict each other, "
        "differing figures, and differing levels of caution. Do not say which is correct unless the "
        "answers themselves give the evidence. Be brief.\n\n"
        f"QUESTION:\n{prompt}\n\nANSWER A ({a.provider}):\n{a.text}\n\nANSWER B ({b.provider}):\n{b.text}"
    )


# ---- the function the command, an agent or an MCP tool calls -------------------------------------


def classify_prompt(prompt: str, history: list | None = None, router: Any = None) -> str:
    """The tier this prompt classifies as, by the router a normal chat turn uses."""
    if router is None:
        from axiom.llm.router import QueryRouter

        router = QueryRouter()
    return router.classify(prompt, context=(history or [])[-10:]).tier.value


def _effective(session_tier: str, prompt_tier: str) -> str:
    return CONTROLLED if CONTROLLED in (session_tier, prompt_tier) else "any"


def compare(
    gateway: Any,
    provider_a: str,
    provider_b: str,
    prompt: str,
    *,
    messages: list[dict[str, Any]] | None = None,
    system: str = "",
    session_tier: str = "any",
    prompt_tier: str | None = None,
    judge: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    session_id: str = "",
    caller: Callable[..., Any] | None = None,
) -> CompareResult:
    """Send the same prompt, with the same context, to two named providers at once.

    ``messages`` is the conversation so far (the prompt is appended unless it is already the last user
    message) and ``system`` the system prompt, both built once by the caller. Nothing is mutated: not the
    gateway's override, not any session.
    """
    history = list(messages or [])
    if prompt_tier is None:
        prompt_tier = classify_prompt(prompt, history)
    effective = _effective(session_tier, prompt_tier)
    if not (history and history[-1].get("role") == "user" and history[-1].get("content") == prompt):
        history.append({"role": "user", "content": prompt})

    result = CompareResult(
        prompt=prompt,
        ran=False,
        session_tier=session_tier,
        prompt_tier=prompt_tier,
        effective_tier=effective,
        a=Side(provider_a),
        b=Side(provider_b),
    )
    providers = {p.name: p for p in getattr(gateway, "providers", [])}

    def refuse(name: str, reason: str) -> None:
        result.refusals.append({"provider": name, "reason": reason})
        _audit(session_id, prompt, effective, name, f"compare refused: {reason}")

    if provider_a == provider_b:
        refuse(provider_a, "the two providers are the same; name two different ones")
    for name in (provider_a, provider_b):
        p = providers.get(name)
        if p is None:
            known = ", ".join(providers) or "none configured"
            refuse(name, f"no provider named {name!r}. Configured: {known}.")
            continue
        if not p.is_usable:
            refuse(name, f"{name} has no API key set, so it cannot be called")
            continue
        decision = decide_switch(list(providers.values()), name, effective)
        if not decision.allowed:
            cause = (
                "this session already holds export_controlled content"
                if session_tier == CONTROLLED
                else "this prompt classifies as export_controlled"
            )
            refuse(
                name,
                f"{name} (tier {p.routing_tier}) may not receive this content: {cause}. "
                f"Providers that may: {', '.join(decision.admitted) or 'none is configured'}.",
            )
    for side, name in ((result.a, provider_a), (result.b, provider_b)):
        p = providers.get(name)
        if p is not None:
            side.model, side.placement, side.tier = p.model, placement_of(p).label(), p.routing_tier
        if any(r["provider"] == name for r in result.refusals):
            side.status = "refused"
    if result.refusals:
        return result  # nothing is sent anywhere

    call = caller or _default_caller(gateway)
    result.ran = True
    for name in (provider_a, provider_b):
        _audit(session_id, prompt, effective, name, "compare: sent")
    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(
            _run_one, gateway, providers[provider_a], history, system, max_tokens, call, timeout
        )
        fb = pool.submit(
            _run_one, gateway, providers[provider_b], history, system, max_tokens, call, timeout
        )
        result.a, result.b = fa.result(), fb.result()

    if result.comparable:
        result.delta = analyze_delta(result.a.text, result.b.text)
    if judge:
        result.judge = _run_judge(
            gateway, providers, judge, result, effective, session_id, call, timeout, max_tokens
        )
    return result


def _run_judge(gateway, providers, name, result, effective, session_id, call, timeout, max_tokens):
    j = Judgement(provider=name)
    p = providers.get(name)
    if p is None:
        j.status, j.error = "refused", f"no provider named {name!r}"
        return j
    j.model, j.placement = p.model, placement_of(p).label()
    if not result.comparable:
        j.status, j.error = "not-run", "there are not two answers to judge"
        return j
    if not p.is_usable:
        j.status, j.error = "refused", f"{name} has no API key set"
        _audit(session_id, result.prompt, effective, name, f"compare judge refused: {j.error}")
        return j
    decision = decide_switch(list(providers.values()), name, effective)
    if not decision.allowed:
        j.status, j.error = "refused", decision.reason
        _audit(session_id, result.prompt, effective, name, f"compare judge refused: {j.error}")
        return j
    _audit(session_id, result.prompt, effective, name, "compare judge: sent")
    side = _run_one(
        gateway,
        p,
        [{"role": "user", "content": _judge_prompt(result.prompt, result.a, result.b)}],
        "",
        max_tokens,
        call,
        timeout,
    )
    j.status, j.text, j.error = side.status, side.text, side.error
    return j


# ---- rendering ----------------------------------------------------------------------------------


def to_dict(result: CompareResult) -> dict[str, Any]:
    """The JSON form: full texts, nothing truncated."""
    return dataclasses.asdict(result)


def record(session: Any, result: CompareResult) -> None:
    """Keep the comparison in the session as ONE marked entry, outside the message transcript, so an
    export can include it and the model never reads it back as a turn."""
    ctx = getattr(session, "context", None)
    if ctx is None:
        return
    entry = to_dict(result)
    entry["kind"] = "comparison"
    entry["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    ctx.setdefault(COMPARISONS_KEY, []).append(entry)
    if result.ran:
        where.note_content_tier(session, result.prompt_tier)


def _fmt_side_header(label: str, s: Side) -> str:
    cost = "unpriced" if s.cost_usd is None else f"${s.cost_usd:.4f}"
    toks = (
        "tokens n/a" if s.input_tokens is None else f"{s.input_tokens} in / {s.output_tokens} out"
    )
    lat = "n/a" if s.latency_ms is None else f"{int(s.latency_ms)}ms"
    return f"{label}  {s.provider}  ({s.model}, {s.placement})  {lat}  {toks}  {cost}"


def _clip(text: str, full: bool, limit: int) -> str:
    if full or len(text) <= limit:
        return text
    return (
        text[:limit].rstrip()
        + f"\n[truncated: showing {limit} of {len(text)} characters; rerun with --full]"
    )


def _lst(items: list[str]) -> str:
    return ", ".join(items) if items else "none"


def render(result: CompareResult, *, full: bool = False, limit: int = DISPLAY_LIMIT_CHARS) -> str:
    out: list[str] = []
    if result.refusals:
        out.append("Comparison refused. Nothing was sent to either provider.")
        for r in result.refusals:
            out.append(f"  {r['provider']}: {r['reason']}")
        return "\n".join(out)
    out.append(
        f"Comparison  (content tier: {result.effective_tier}; the same context went to both)"
    )
    for label, s in (("A", result.a), ("B", result.b)):
        out.append("")
        out.append(_fmt_side_header(label, s))
        if s.status == "ok":
            out.extend("  " + ln for ln in _clip(s.text, full, limit).splitlines() or [""])
        else:
            what = "timed out" if s.status == "timeout" else "FAILED"
            out.append(f"  {s.provider} {what}: {s.error}")
    out.append("")
    if result.delta is None:
        bad = [s for s in (result.a, result.b) if s.status != "ok"]
        out.append(
            "No comparison: "
            + "; ".join(f"{s.provider} {s.status} ({s.error})" for s in bad)
            + ". The answer above is the only one received and is not compared with anything."
        )
    else:
        d = result.delta
        ratio = "n/a" if d.length_ratio is None else f"{d.length_ratio:.2f}"
        out.append("Delta  (computed from the two texts; no model involved)")
        out.append(f"  Length       A {d.words_a} words, B {d.words_b} words (B/A = {ratio})")
        out.append(
            f"  Overlap      {d.similarity:.0%} word-triple overlap; {d.matched_sentences} of "
            f"{d.sentences_a} sentences in A have a near-identical sentence in B"
        )
        out.append(f"  Shared terms {_lst(d.shared_terms)}")
        out.append(f"  Only in A    {_lst(d.only_a_terms)}")
        out.append(f"  Only in B    {_lst(d.only_b_terms)}")
        if d.numeric_disagreements:
            out.append("  Numbers that differ for the same thing:")
            for n in d.numeric_disagreements:
                out.append(f"    {n['context']}: A says {n['a']}, B says {n['b']}")
        else:
            out.append("  Numbers that differ for the same thing: none found")
        out.append(
            f"  Numbers only in A: {_lst(d.numbers_only_a)}; only in B: {_lst(d.numbers_only_b)}"
        )
        out.append(f"  Citations    A: {_lst(d.citations_a)}; B: {_lst(d.citations_b)}")
        out.append(f"  Refusal      A: {_lst(d.refusal_a)}; B: {_lst(d.refusal_b)}")
        out.append(f"  Hedging      A: {_lst(d.hedges_a)}; B: {_lst(d.hedges_b)}")
    if result.judge is not None:
        j = result.judge
        out.append("")
        out.append(
            f"Judge  {j.provider}  ({j.model}, {j.placement})  "
            "[model-generated opinion, not a computed fact]"
        )
        if j.status == "ok":
            out.extend("  " + ln for ln in _clip(j.text, full, limit).splitlines())
        else:
            out.append(f"  judge {j.status}: {j.error}")
    return "\n".join(out)


# ---- the slash command --------------------------------------------------------------------------


def _providers(agent: Any) -> list[Any]:
    return list(getattr(getattr(agent, "gateway", None), "providers", []) or [])


def _session_tier(agent: Any) -> str:
    return where.content_tier_of(getattr(agent, "session", None))


def _describe(p: Any, content_tier: str) -> str:
    text = f"{p.model} · {placement_of(p).label()}"
    if not p.is_usable:
        text += " · NOT USABLE: no API key"
    if not admits(p.routing_tier, content_tier):
        text += f" · REFUSED: session holds {content_tier} content"
    return text


def complete_args(agent: Any, args_so_far: list[str]) -> list[tuple[str, str]]:
    """First word: every configured provider. Second: the same minus the first. Then nothing."""
    words = [a for a in args_so_far if not a.startswith("--")]
    if len(words) >= 2:
        return []
    tier = _session_tier(agent)
    return [(p.name, _describe(p, tier)) for p in _providers(agent) if p.name not in words]


_FLAGS_WITH_VALUE = {"--judge", "--timeout"}
_FLAGS = {"--json", "--full"}


@dataclass
class Parsed:
    a: str = ""
    b: str = ""
    prompt: str = ""
    judge: str | None = None
    json: bool = False
    full: bool = False
    timeout: float = DEFAULT_TIMEOUT_S
    error: str = ""


def parse_line(line: str) -> Parsed:
    """``/compare A B [flags] prompt… [flags]``. Flags may sit before the prompt or after it."""
    out = Parsed()
    parts = line.split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    toks = [(m.group(), m.start(), m.end()) for m in re.finditer(r"\S+", rest)]

    def take_flag(i: int) -> int | None:
        w = toks[i][0]
        if w in _FLAGS:
            setattr(out, w[2:], True)
            return i + 1
        if w in _FLAGS_WITH_VALUE:
            if i + 1 >= len(toks):
                out.error = f"{w} needs a value"
                return None
            val = toks[i + 1][0]
            if w == "--judge":
                out.judge = val
            else:
                try:
                    out.timeout = float(val)
                except ValueError:
                    out.error = f"--timeout needs a number of seconds, got {val!r}"
                    return None
            return i + 2
        return i  # not a flag

    i, names = 0, []
    while i < len(toks) and len(names) < 2:
        if toks[i][0].startswith("--"):
            nxt = take_flag(i)
            if nxt is None:
                return out
            if nxt == i:
                out.error = f"unknown option {toks[i][0]}"
                return out
            i = nxt
        else:
            names.append(toks[i][0])
            i += 1
    while (
        i < len(toks) and toks[i][0] in _FLAGS | _FLAGS_WITH_VALUE
    ):  # flags between names and prompt
        nxt = take_flag(i)
        if nxt is None:
            return out
        i = nxt
    if i < len(toks) and toks[i][0].startswith("--"):
        out.error = f"unknown option {toks[i][0]}"
        return out
    end = len(toks)
    while end > i:  # trailing flags
        if toks[end - 1][0] in _FLAGS:
            take_flag(end - 1)
            end -= 1
        elif end - 2 >= i and toks[end - 2][0] in _FLAGS_WITH_VALUE:
            take_flag(end - 2)
            end -= 2
        else:
            break
    if len(names) < 2:
        out.error = "need two provider names"
        return out
    out.a, out.b = names
    if i < end:
        out.prompt = rest[toks[i][1] : toks[end - 1][2]]
    else:
        out.error = "need a prompt"
    return out


def build_context(agent: Any, prompt: str) -> tuple[list[dict[str, Any]], str, str, str]:
    """``(messages, system, prompt_tier, note)``: exactly what a normal turn would send, computed once, on
    a throwaway copy of the conversation so the real transcript is untouched. Honours ``/rag off`` because
    it goes through the agent's own system-prompt builder."""
    session = getattr(agent, "session", None)
    history = [
        {"role": m.role, "content": m.content}
        for m in getattr(session, "messages", [])
        if m.role in ("user", "assistant")
    ]
    note = ""
    system = ""
    messages = history + [{"role": "user", "content": prompt}]
    if hasattr(agent, "_build_system_prompt") and hasattr(agent, "_build_messages"):
        try:
            from ..agent import _active_scope

            scope = _active_scope(agent, None)
            scratch = copy.copy(scope.session)
            scratch.messages = list(scope.session.messages)
            scratch.context = dict(scope.session.context)
            scratch.add_message("user", prompt)
            tmp = dataclasses.replace(scope, session=scratch)
            system = agent._build_system_prompt(scope=tmp)
            messages = agent._build_messages(scope=tmp)
        except Exception as exc:  # noqa: BLE001
            note = (
                f"context fallback: the chat's system prompt could not be built "
                f"({type(exc).__name__}: {exc}); both providers got the conversation only"
            )
    else:
        note = "context: no system prompt (no chat agent behind this session)"
    router = getattr(agent, "_router", None)
    tier = classify_prompt(prompt, history, router)
    return messages, system, tier, note


def run_command(agent: Any, line: str) -> str:
    p = parse_line(line)
    usage = (
        "usage: /compare <providerA> <providerB> [--judge <provider>] [--json] [--full] <prompt>"
    )
    if p.error:
        return f"\n  {p.error}\n  {usage}\n"
    messages, system, prompt_tier, note = build_context(agent, p.prompt)
    session = getattr(agent, "session", None)
    result = compare(
        agent.gateway,
        p.a,
        p.b,
        p.prompt,
        messages=messages,
        system=system,
        session_tier=_session_tier(agent),
        prompt_tier=prompt_tier,
        judge=p.judge,
        timeout=p.timeout,
        session_id=getattr(session, "session_id", "") or "",
    )
    result.context_note = note
    record(session, result)
    if p.json:
        return json.dumps(to_dict(result), indent=2, default=str)
    text = render(result, full=p.full)
    if note:
        text += f"\n\n  note: {note}"
    return "\n" + "\n".join("  " + ln if ln else ln for ln in text.splitlines()) + "\n"


FOLLOW_UPS = {
    "switch": FollowUp("/model {name}", "carry on with {name}"),
    "export": FollowUp("/export", "write the comparison out with the transcript"),
    "providers": FollowUp("/model", "list the providers and where each one runs"),
    "where": FollowUp("/where", "see which provider this session may use and why"),
}


@command(
    "/compare",
    "Send one prompt to two configured models at once and compare the answers",
    "Model & routing",
    usage="/compare <A> <B> <prompt>",
    mutates=True,  # it spends tokens and records a comparison in the session
    complete=complete_args,
    follow_ups=FOLLOW_UPS,
    cli=CliSpec(
        ("--compare",), line="/compare {values}", applies="headless", metavar=("A", "B", "PROMPT"), json=True,
        help="send one prompt to two configured models, print the comparison and exit",
    ),
)
def compare_command(ctx, args):
    line = ctx.line or "/compare " + " ".join(args)
    out = run_command(ctx.agent, line)
    parsed = parse_line(line)
    if parsed.error:
        return out
    if "refus" in out.lower() or "not admitted" in out.lower():
        keys: list = ["providers", "where"]
    else:
        keys = [("switch", {"name": parsed.a}), ("switch", {"name": parsed.b}), "export"]
    follow = pick("/compare", *keys)
    if parsed.json:
        try:
            return json.dumps(with_json_follow_ups(json.loads(out), follow), indent=2, default=str)
        except ValueError:
            return out
    return CommandResult(out, follow)
