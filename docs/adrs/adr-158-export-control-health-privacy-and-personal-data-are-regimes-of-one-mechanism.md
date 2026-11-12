# ADR-158: Export control, health privacy and personal-data protection are regimes of one mechanism, and their classifiers are plugins

**Status:** Draft (2026-10-05)
**Related:** [prd-regulated-data-regimes](../prds/prd-regulated-data-regimes.md), [prd-access-agreements](../prds/prd-access-agreements.md), [ADR-152](adr-152-the-semantic-classifier-advises-and-labels-lists-and-agreements-decide.md) (a classifier advises; lists and agreements decide), [spec-classification-boundary](../specs/spec-classification-boundary.md), [spec-ec-client-capability](../specs/spec-ec-client-capability.md), [ADR-114](adr-114-mcp-authority-enforcement.md), [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md) and [ADR-150](adr-150-an-attestation-record-is-kept-in-a-logbook.md) (attestation), [ADR-157](adr-157-the-serving-tier-bounds-what-any-one-call-can-cost.md), [ADR-159](adr-159-deletion-is-finished-only-when-it-is-proven.md), [ADR-160](adr-160-the-policy-tier-of-a-system-prompt-is-immutable-and-enforced-in-code.md)

## Context

Axiom's first regulated-data capability was export control. It grew as a control-list file, a content label, a per-client flag that says whether a host may receive controlled content, and a semantic classifier that advises. ADR-152 corrected the part that went wrong: a model's guess had withheld public content from clients, and the decision was moved back to lists, labels and agreements. The access-agreements PRD then generalized the unlock side: an export authorization, a vendor's NDA and a data-use agreement are one primitive.

The detection, labelling, handling and audit sides are still export-control-shaped. Future Axiom customers need more than export control. A health organization needs protected health information handled under HIPAA. An organization with people in Europe needs personal data handled under GDPR. Others need controlled unclassified information, payment data, or a vendor's proprietary material. If each is built as a copy of the export-control path, every customer gets its own lists, labels, gates, agreements, audit trail and deletion procedure, the copies drift, and the same mistakes (a classifier that decides, a trace that stores what it should not) get repeated once per regime.

The regimes differ in the ways that matter and agree in the ways that let one mechanism serve them. HIPAA requires documentation to be kept for six years from creation or last effective date (45 CFR 164.316(b)(2)). Export-control records are kept five years (15 CFR 762.6; 22 CFR 122.5). GDPR sets no fixed period but requires a controller to demonstrate compliance (Article 5(2)), to act on an erasure request within one month, extendable by two (Article 12(3)), and to tell recipients (Article 19). What they share is a content class, a way to recognize it, a set of people or processors who may handle it, rules about where it may go and what may be stored about it, an audit duty, a retention rule, and a way to end its life.

## Decision

A **regime** is a declared bundle, and Axiom ships one mechanism that runs any regime. A regime names:

1. **Content classes**: the labels it applies, such as `export-controlled`, `protected-health`, `personal-data`, `proprietary`.
2. **Detector providers**: the pluggable classifiers that recognize those classes (below).
3. **Agreements**: what a person or processor must hold to handle the class (an export authorization, a workforce training attestation, a business associate agreement, a data processing agreement, an NDA), expressed through the access-agreements primitive.
4. **Handling rules**: where the content may be processed (locally only, or by a processor that holds a named agreement), what may be stored about it (traces, memory, caches, analytics, exports), and how much any one call may return (minimum necessary).
5. **Audit**: what is recorded about its handling, kept how long.
6. **Retention and end of life**: how long it is kept and how it is destroyed, through verified purge (ADR-159).
7. **Clocks**: the response times the regime imposes.

Regimes ship as **profiles**, reference configurations for export control, health privacy (HIPAA), personal-data protection (GDPR), privileged legal communications, controlled unclassified information and proprietary material. A profile cites the regulation text and date it was derived from. The core knows nothing about any one regime. A customer enables, edits or adds regimes by configuration, and every profile states that software supplies controls and that compliance also needs organizational measures such as signed agreements, a risk analysis and policy.

**Detector providers are plugins behind one interface.** A provider takes content and context and returns findings: a label, an optional span, a confidence and the evidence. There are three kinds. *Deterministic* providers use lists, identifier patterns and checksum validators (the eighteen identifier categories of the health profile, locale-specific national identifiers, the export-control terms). *Statistical* providers use models. *External* providers are outside services. The decision rule is ADR-152's, generalized: **labels from lists and agreements decide, and a statistical or external provider may only advise.** An advisory finding can raise a flag for a person or route a turn to a stricter lane; it cannot on its own withhold content or release it. Each provider's calibration, stated confidence against how often it was right, is measured and published with the provider. A domain adds a classifier by registering a provider, with no change to the core.

**Labels have three sources, because not every regime can be detected.** A label comes from *detection* (a list or a pattern recognizes the content: protected identifiers, controlled terms), from *declaration* (a person or a record states it: a matter, an engagement or a data agreement says this material is privileged or proprietary), or from *inheritance* (content derived from labelled content, such as an embedding, a summary or a cached answer, carries its source's label). Legal privilege is the clear case for declaration. It attaches to a confidential communication between a lawyer and a client for legal advice, so it depends on who is talking and why, not on how the text looks, and a classifier can only suggest that something may be privileged. The label is set by the matter and its participants, a person can confirm a suggestion, and the same list-and-agreement rule decides. Because privilege belongs to the client, the platform has no way to waive it: sharing across a federation, support access, analytics and model training are all denied for privileged content unless the holder allows them.

**One pipeline, applied at four points.** Content is labelled at ingest. A request is checked before it runs, so a controlled request is refused before the query is paid for (ADR-157). A result is checked at the sink where it leaves. A write is checked where it would persist: traces, memory, caches and analytics apply the regime's storage rule, which for some classes is "store nothing." More than one regime can label the same content; the strictest handling in the union applies.

**Processors default to denied.** Any model, service or vendor that would see regime content must be local, or must hold the agreement the regime names. Without one, the content is not sent. This is the rule that keeps a cloud classifier off the path of restricted content.

**Regimes federate.** A regime profile, its list versions and its labels travel with content between nodes; a receiving node applies the stricter of its own and the arriving handling. Decision records name the profile versions so a counterparty can verify which rules governed an exchange.

**The advisory seat is one engine.** Statistical detection across all regimes is served by the platform's typed-decision engine (spec-governed-interaction C9a): local, calibrated per seat, trained on the deployment's own labelled outcomes, and advisory everywhere by construction.

**Conflicts between regimes are resolved by explicit precedence.** A retention duty or legal hold beats an erasure request, and the refusal states which obligation applies. Stricter handling beats looser. Every override is recorded.

**A change to a list or a profile can act on content already held.** A new list version triggers re-labelling and, where policy says so, a purge (ADR-159).

## Options considered

**Copy the export-control path for each regime.** Fastest for the first customer and worst for the second. It produces drift, and it repeats the per-regime mistakes. It lost on duplication.

**One universal "sensitive" flag.** Simple, and wrong: the regimes differ in which agreements unlock the content, where it may be processed, what is stored, and how long records are kept. A single flag cannot carry those differences.

**Let a model decide what is regulated.** A model is not deterministic, cannot be audited as a rule, and caused the failure ADR-152 corrected. It remains useful as an adviser.

**Buy a data-loss-prevention product and call it done.** A product can be a detector provider, and a good one. It cannot be the control, because the decision and the handling rules must be ours to state, test and audit.

## Consequences

The first customer-visible change is vocabulary: what is called the export-control path in code and configuration becomes one regime among several. Existing export-control behavior does not change, and its current files become the profile's lists.

Detector calibration becomes a published, tested property of each provider, which is more work than shipping a list and is what makes an advisory finding trustworthy.

The processor rule makes agreements load-bearing for architecture, not only for access: a deployment without a business associate agreement or a data processing agreement cannot use a cloud model on that class of content, by construction.

Handling rules reach into places that never had a policy, notably the request trace, the memory ledger and the answer cache. Those writes must go through the regime check.

Follow-up work, sequenced in the PRD: the regime registry and profile format; the detector provider interface with the deterministic health and personal-data providers; the handling-rule enforcement points; the profile documents mapping each requirement to a control and its evidence; and verified purge (ADR-159). The system-prompt tier (ADR-160) is how a regime's refusals and disclosures are made to hold.
