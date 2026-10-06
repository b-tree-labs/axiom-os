# PRD: Regulated-data regimes: one mechanism for export control, health privacy and personal data

**Product / Feature:** Regimes as configuration, pluggable detector classifiers, regime profiles for export control, HIPAA and GDPR, handling rules, verified purge, and retention

**Owner:** Ben Booth   •   **Status:** Draft   •   **Last updated:** 2026-10-05

**Related:** [ADR-158](../adrs/adr-158-export-control-health-privacy-and-personal-data-are-regimes-of-one-mechanism.md), [ADR-159](../adrs/adr-159-deletion-is-finished-only-when-it-is-proven.md), [ADR-160](../adrs/adr-160-the-policy-tier-of-a-system-prompt-is-immutable-and-enforced-in-code.md), [prd-access-agreements](prd-access-agreements.md), [prd-sensitive-conversations](prd-sensitive-conversations.md), [prd-gold-serving-safeguards](prd-gold-serving-safeguards.md), [spec-classification-boundary](../specs/spec-classification-boundary.md), [ADR-152](../adrs/adr-152-the-semantic-classifier-advises-and-labels-lists-and-agreements-decide.md)

---

## 1) Elevator pitch

Organizations that handle regulated data need the same few things whatever the regulation: to recognize the data, to say who and what may handle it, to keep it from leaking into places that should not hold it, to prove what happened, and to destroy it and show that it is gone. Axiom already does this for export control. This PRD makes that one instance of a general mechanism, adds profiles for HIPAA and GDPR so a future customer can configure Axiom toward compliance with either, adds a profile for privileged legal communications, makes the classifiers that recognize the data pluggable per domain, and adds verified deletion that ends in a receipt the data owner can rely on.

## 2) Problem / Opportunity

- **Export control is built as a special case.** Its control list, label, per-client flag and advisory classifier are specific to it. A customer who needs health or personal-data handling would get a copy, with its own lists, gates, audit and deletion, and the copies would drift.
- **Detection is not pluggable.** A health organization's identifiers, a European customer's national identifiers and an export-control term list are different problems. There is no interface a domain can implement to teach Axiom to recognize its own regulated content.
- **Storage rules do not exist where data persists.** In a running deployment surveyed on 2026-10-05, raw user questions and answers sit in a plain-text request trace (2,398 turns), and caches, a memory ledger and a chat front end's own history store hold text as well. None applies a rule for what may be stored about regulated content.
- **Deletion cannot be proven.** Axiom has logical tombstones for memory and no way to find every copy of a piece of content, delete them, and show that none remain. The AEOS specification lists the right to be forgotten as an open question.
- **Processors are not constrained.** Nothing stops a model or service that has no business associate agreement or data processing agreement from receiving regulated content.
- **Legal privilege is a different kind of regime and is easy to lose.** A law firm or a legal department using an AI platform risks waiving privilege by disclosing a communication to a third party, and a court has already held that a person's own chats with a public AI platform are not privileged (see section 10). Privileged content needs labelling by matter, segregation between clients, a processor rule that excludes any vendor not bound to confidentiality, no use for training, and a way to retrieve it when it escapes.
- **The opportunity** is that the hard parts, the agreements primitive, the signed logbook, the advisory-only classifier rule and the single serving door, already exist. What is missing is the generalization and the end of the data's life.

Research behind the retention and timing defaults, gathered 2026-10-05 and to be confirmed by counsel before being relied on: HIPAA requires required documentation to be retained six years from creation or last effective date ([45 CFR 164.316](https://syfert.com/cfr/sections/45-164.316.html)); export-control records are kept five years ([15 CFR 762.6](https://www.law.cornell.edu/cfr/text/15/762.6); 22 CFR 122.5 for defense articles); GDPR requires a response to a data-subject request within one month, extendable by two (Article 12(3)), erasure without undue delay with notice to recipients ([Articles 17 and 19](https://www.dataprotection.ie/individuals/know-your-rights/right-erasure-articles-17-19-gdpr)), and an ability to demonstrate compliance (Article 5(2)); NIST SP 800-88 Rev. 1 describes a certificate of sanitization recording media, method, tool and version, operator, verifier, date and disposition ([NIST](https://csrc.nist.gov/pubs/sp/800/88/r1/final)).

## 3) Principles (the invariants every phase must keep)

1. **Deterministic.** Lists, labels and agreements decide. A statistical or external classifier may advise and may never decide alone (ADR-152, ADR-158).
2. **One mechanism, many regimes.** A regime is configuration. Adding one does not change the core. Where regimes overlap, the strictest handling wins.
3. **Processors default to denied.** A model or service sees regime content only if it is local or holds the agreement the regime names.
4. **Nothing is stored that the regime says not to store.** The rule applies to traces, memory, caches, analytics and exports, at the point of the write.
5. **Holds beat erasure, and the refusal is explained.** A retention duty or legal hold blocks a purge, and the requester is told which obligation applies.
6. **A purge without a receipt did not happen.** The receipt is signed, content-free and honest about residual copies.
7. **Everything is configurable, with researched defaults.** Approval policy, retention, backup policy and clocks are settings, shipped with defaults that a customer can read and change.
8. **Some regimes label by declaration, not detection.** Privilege depends on who is communicating and why. A matter and its participants set the label; a classifier may suggest one and a person confirms it. The platform can never waive a holder's privilege.
9. **Modest claims.** Software supplies controls. A profile maps each requirement to a control and its evidence, and says what organizational work remains. Axiom does not claim that a deployment is compliant.

## 4) Goals & Success Metrics

- **Primary goal:** a customer can turn on the HIPAA or GDPR profile and have the technical controls that the profile maps to, with evidence for each, without Axiom changing its core.
- **Success metrics:**
  - A new regime is added by configuration and a provider, with zero core changes, demonstrated by shipping the third profile that way.
  - 100 percent of registered content stores implement the purge provider interface; the count of unregistered stores is reported and trends to zero.
  - On the canary battery, every planted regulated marker is purged and verified absent in every registered store, and a battery run that leaves one fails the build.
  - A purge's plan reports its expected duration, and the median purge completes inside the regime's clock.
  - Each shipped detector provider publishes a measured calibration (stated confidence against observed accuracy), and no advisory finding alone withholds or releases content, verified by test.
  - With a regime's storage rule set to "store nothing," zero regulated text is found in traces, memory or caches by the verification sweep.

## 5) Key Users / Personas

- **Compliance officer:** configures the regime, approves purges and holds, and answers an auditor.
- **Data owner:** the person or organization that put the content in; may request a purge and receives the receipt.
- **Data subject:** a person whose personal data is held; exercises access and erasure rights through the customer.
- **Administrator:** runs the deployment; sets detector providers, handling rules and backup policy.
- **Attorney and client (privilege holder):** the lawyer works on a matter; the client holds the privilege and alone can waive it.
- **Operator or clinician:** does the day's work and should meet the controls as clear messages, not obstacles.
- **Auditor:** reads signed records and receipts and the profile's requirement-to-control map.
- **Customer security reviewer:** decides whether a cloud model or vendor may be used, by reading the processor rule.

## 6) Scope: capabilities by phase

**Phase 0: the regime as configuration.**
1. A regime registry and profile format: content classes, detector providers, agreements, handling rules, audit, retention, clocks.
2. Export control re-expressed as the first profile, with no change in behavior.

**Phase 1: pluggable detectors.**
1. The detector provider interface (content and context in, labelled findings with confidence and evidence out), with deterministic, statistical and external kinds.
2. Deterministic providers for health identifiers (the eighteen identifier categories) and for personal data (names, contact details, locale-specific national identifiers with checksum validation).
3. A calibration harness that measures and publishes each provider's reliability.

**Phase 2: handling rules enforced where data persists.**
1. The four enforcement points: ingest, request, result, write.
2. Storage rules for traces, memory, caches, analytics and exports, including "store nothing."
3. The processor rule, tied to the agreements held.
4. Minimum-necessary limits on how much one call or export may return (shares machinery with the gold serving safeguards).

**Phase 3: verified purge.**
1. The purge provider interface and registry, and providers for the built-in stores.
2. Select, plan, approve, execute, verify and receipt, with approval policy as configuration.
3. Backup policies (crypto-shredding, expiry, re-scrub on restore), tombstones, holds, and the request clock.
4. The canary battery and scheduled verification sweeps.

**Phase 4: profiles and evidence as a product.**
1. HIPAA and GDPR profiles, each with a requirement-to-control-to-evidence map and a list of organizational measures that remain.
2. A privileged-communications profile: matter registry as the label source, per-matter segregation (no cross-matter retrieval or memory), per-matter keys so a matter can be crypto-shredded, a processor rule that requires a confidentiality and no-training commitment, access logging that supports a privilege log, and a clawback workflow that uses verified purge to recover a disclosed document across every store and index.
3. Reports an auditor can read: handling by regime, purge receipts, overdue requests, unregistered stores.
4. Continuously generated evidence packs mapped to NIST AI RMF, ISO/IEC 42001 and SOC 2 families, composed from decision records, calibration receipts, purge receipts and battery results, with an auditor identity that holds aggregate-only rights.

## 7) Non-goals

- Legal advice or a statement that any deployment is compliant. A profile is a starting configuration with its evidence.
- Replacing a data-loss-prevention product. One can be a detector provider.
- Certifying the platform against HIPAA, GDPR or any standard.
- Deciding a customer's lawful basis, retention schedule or agreements. Those are the customer's, supplied as configuration.

## 8) Non-Functional / Constraints

- **Performance:** a deterministic detector adds under 10 ms to a typical request; a refusal at the request check returns in under 50 ms.
- **Security:** receipts and policy configuration are signed; detector findings are content-free in logs; the processor rule is enforced in code and cannot be overridden by a client.
- **Locality:** every regime can run entirely on infrastructure the customer controls.
- **Extensibility:** providers, profiles and verifiers are extensions in the existing mechanism.
- **Domain-agnostic:** nothing in the core names a regulation; regimes and their wording live in profiles.

## 9) Open decisions for the owner

1. **Default approval for a purge.** Proposed: one approver by default, with two-person approval available per regime and per size of purge. Configurable either way.
2. **Default receipt retention.** Proposed: six years, the longest specific period found, configurable with a floor of three years and no option to keep none. Counsel should confirm for each customer.
3. **Default backup policy.** Proposed: expiry with the date stated on the receipt, with crypto-shredding offered as the stronger option for regimes that need it.
4. **Which statistical detector to ship first.** Proposed: the platform's own typed-decision engine (spec-governed-interaction C9a), after the deterministic providers and the calibration harness exist, so every advisory finding has a measured reliability from day one.
5. **Jurisdictions.** Which regimes and locales matter to the first customers? That orders Phase 4.
6. **Privilege.** Is the first legal customer a law firm (matter segregation is central) or a corporate legal department (privilege and work product over internal communications)? That decides the first label source and the segregation model.
7. **Ownership of the profiles.** A profile derived from a regulation needs a named maintainer and a review cadence, since regulations change.

## 10) Evidence behind this document

- The running-deployment survey of 2026-10-05: plain-text request trace of 2,398 turns; no purge tooling; one store (a chat front end's history) not inspectable; backups present.
- The research above, with the caveat that several figures come from secondary summaries and need counsel's confirmation.
- ADR-152, which already established that a model's guess must not decide.
- On privilege, from secondary reports gathered 2026-10-05 and to be confirmed by counsel: in *United States v. Heppner* (S.D.N.Y., February 10, 2026) Judge Rakoff held that a defendant's own conversations with a public AI platform were neither attorney-client privileged nor work product, because no attorney-client relationship exists with the platform, and that sending the output to counsel afterward did not make it privileged ([report](https://qz.com/ai-chatbot-attorney-client-privilege-ruling-heppner-041626)). ABA Formal Opinion 512 (July 29, 2024) applies the duty of confidentiality (Model Rule 1.6) to generative AI, treats self-learning tools as higher risk because they can retain and reuse inputs, and says informed client consent may be needed before confidential information is entered ([summary](https://www.americanbar.org/groups/litigation/resources/newsletters/ethics-professionalism/generative-ai-lawyers-part-2-maintaining-confidentiality/)). Federal Rule of Evidence 502(b) treats an inadvertent disclosure as not a waiver only if the holder took reasonable steps to prevent it and promptly took reasonable steps to rectify it ([overview](https://www.everlaw.com/blog/ediscovery-best-practices/federal-rule-of-evidence-502-privilege-protection/)); verified purge receipts are evidence of the second step and the processor rule of the first.

## 11) Acceptance & rollout

- **Sign-off:** the owner approves the regime format, the defaults in section 9 and the first two profiles. Counsel reviews each profile's requirement map before it is described to a customer.
- **Rollout:** Phase 0 changes no behavior. Each later phase ships behind a regime setting that is off by default and runs first in a report-only mode that logs what it would have done.
- **Rollback:** every phase is a setting, so rollback is a configuration change.
- **Done when:** the HIPAA and GDPR profiles are published with evidence maps, the canary battery has caught a deliberately broken provider, and a customer reviewer has traced one requirement from regulation text to control to evidence.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
