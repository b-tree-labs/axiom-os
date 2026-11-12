# PRD: Capability Discovery

**Status:** Living
**Related:** spec-capability-telemetry, prd-evals, ADR-121, ADR-117

## 1. Elevator pitch

Someone's assistant should know what this system can do for them, and we should
be able to show it made them better at their work. Today neither holds: the
capability exists, the assistant does not reach for it, and we cannot prove the
difference either way.

## 2. Problem

**People do not use what they do not know exists**, and the failure is silent.
An assistant opens a shell to read node state the node can report, or greps
files for memory that is queryable. Nothing errors. Nobody notices there was a
better path, so nobody fixes it.

The measured baseline is unflattering and should stay visible: an assistant with
the platform configured throughout a long working session made over a hundred
tool calls and reached for the platform essentially never. **Discovery, not
capability, was the binding constraint.**

The second problem follows from the first. If we cannot see what an assistant
reaches for, we cannot claim the platform helps — and "our users love it" is not
a claim a careful person accepts about their own product.

## 3. Goals and success metrics

| Goal | Measure |
|---|---|
| An assistant learns what exists without being told | Share of sessions making a platform call; time to first call |
| Discovery works for each person, not each install | A newcomer on a mature node still receives a full block |
| The platform demonstrably helps | Paired comparison delta that can report **no difference** |
| Nothing is claimed that is not measured | Every headline number reproducible from the series |

**Explicit non-goal:** growth in tool-call volume. An assistant calling
everything once maximises that number while discovering nothing, and a metric a
degenerate strategy maximises is measuring the strategy.

## 4. Key users

- **A researcher or operator** using whichever assistant they already prefer.
  They should benefit from a file write, not from adopting our tooling first.
- **A student joining a shared node.** The hardest case: everything is already
  used by somebody else, and naive accounting would show them nothing.
- **Us**, deciding whether a change helped, before shipping it.

## 5. Capabilities

**5.1 Session-start discovery.** A short, generated, task-shaped block in the
instruction files every assistant already reads. Honest to the install, bounded
in cost, and it shrinks as capabilities are discovered — an empty block is the
end state, not a failure.

**5.2 Usage that is visible and comparable.** One content-free projection per
invocation across every surface, persisted so it can be measured over time
rather than observed in the instant.

**5.3 A definition of "discovered" that survives adversarial use.** Per
principal, repeat use, decayed, interactive surfaces only. Each rule exists
because its absence broke the loop in a specific, demonstrated way.

**5.4 The counterfactual.** The same task with the platform and without. This is
the only capability here that can answer whether any of the rest matters, and it
must be able to report that it does not.

## 6. What we will not claim

- That block shrinkage proves the block works. It is consistent with the block
  being ignored entirely.
- That grounding is correctness. An answer can be perfectly grounded in evidence
  that is itself wrong.
- That a number measured on our own nodes generalises to installs we do not run.

## 7. Open questions

- **How a "session" is identified** across harnesses that do not expose one.
- **Whether the reach-for gap can be detected** without inspecting content, which
  the privacy design forbids.
- **What the block should do when an install is fully discovered** — stay empty,
  or rotate to recently-added capabilities.
