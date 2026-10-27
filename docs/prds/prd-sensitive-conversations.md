# PRD: Sensitive conversations: handling mental-health-adjacent chats safely, as an optional pack

**Product / Feature:** An optional domain pack that detects conversations touching mental health, crisis, legal or medical advice, responds safely and without overreaching, and stores nothing by default

**Owner:** Ben Booth   •   **Status:** Draft   •   **Last updated:** 2026-10-05

**Related:** [prd-regulated-data-regimes](prd-regulated-data-regimes.md), [ADR-158](../adrs/adr-158-export-control-health-privacy-and-personal-data-are-regimes-of-one-mechanism.md), [ADR-159](../adrs/adr-159-deletion-is-finished-only-when-it-is-proven.md), [ADR-160](../adrs/adr-160-the-policy-tier-of-a-system-prompt-is-immutable-and-enforced-in-code.md), [ADR-152](../adrs/adr-152-the-semantic-classifier-advises-and-labels-lists-and-agreements-decide.md)

---

## 1) Elevator pitch

People tell chat assistants about their mental health, whether or not the assistant was built for it. An organization that deploys an Axiom-based chat cannot assume this will not happen, and cannot treat it as an ordinary question. This pack lets a deployment recognize such a conversation, respond in a safe and honest way that does not pretend to be care, keep no more of it than the deployment decides, and prove that it handled it as promised. It is not a therapy product and does not make one.

## 2) Problem / Opportunity

- **It will happen in any open-ended chat.** A person under stress says so, to a tool they are already using. Without a policy, the response is whatever the model happens to produce, and the transcript lands in the same logs as any other question.
- **The content is special.** A statement about mental health is health data under GDPR (a special category, Article 9) and protected health information under HIPAA when a covered entity or its business associate holds it. It calls for stricter handling than ordinary chat and often for no retention at all.
- **There is a safety duty, and it cannot be left to a prompt.** When a person may be in danger, the right response must be given every time, with resources that fit where they are. ADR-160 sets out why that has to be enforced in code.
- **The law is moving and is restrictive.** In 2025 several US states enacted limits on AI used for mental-health purposes. Reported as of the time of research: Illinois (HB 1806, effective August 1, 2025) prohibits AI from directly engaging in therapeutic communication in therapy settings or making therapeutic decisions without a licensed professional; Nevada (AB 406, effective July 1, 2025) prohibits a chatbot from providing, or claiming to provide, mental or behavioral health services; Utah (HB 452, effective May 7, 2025) regulates mental-health chatbots, requires disclosures and limits the use of personal information; California (SB 243) regulates companion chatbots; New York has also acted. Penalties reported run from $2,500 to $15,000 a day per violation. These are secondary summaries and the landscape changes quickly, so every claim here needs current legal review before a customer relies on it.
- **The opportunity.** A deployment that handles this well, by detecting it, responding safely, disclosing honestly, retaining nothing by default and being able to show how it behaved, avoids the worst outcomes and gives a customer a defensible position. A deployment that handles it by accident has neither.

## 2a) The same lane covers other advice-seeking conversations

People also bring legal and medical questions to a chat assistant and often assume it is confidential. For legal matters the assumption can be costly: a federal court has held that a person's own chats with a public AI platform are not attorney-client privileged, because no attorney-client relationship exists with the platform, and that sharing the output with counsel afterward does not make it privileged (*United States v. Heppner*, S.D.N.Y., February 10, 2026, as reported; to be confirmed by counsel). A chat log can also be requested in litigation. So for these classes the pack adds a disclosure, not a refusal: the assistant is not a lawyer or a clinician, the conversation is not privileged or confidential in the legal sense, and information should not be entered that the person needs to keep privileged. The wording is configuration per jurisdiction and deployment. Where a deployment offers legal-privileged work to lawyers, that is a different, declared regime (see the privileged-communications profile in [prd-regulated-data-regimes](prd-regulated-data-regimes.md)), not this lane.

## 3) Principles (the invariants every phase must keep)

1. **Not a clinician.** The assistant never diagnoses, treats, recommends medication or presents itself as therapy. A verifier enforces this (ADR-160).
2. **Detection is deterministic first.** Crisis and self-harm cues come from lists and patterns that are tested. A model may advise a stricter response, never a looser one.
3. **The crisis response cannot be overridden.** It is part of the policy tier, appears whenever the policy requires it, and holds against prompt tricks.
4. **Honest disclosure.** The person is told they are talking to an AI, that it is not a clinician, and where to find a person.
5. **Resources fit the place.** The crisis resources are configuration by jurisdiction and language, and the deployment verifies them. The pack ships a US default (the 988 Suicide and Crisis Lifeline) and expects customers to supply their own.
6. **Store nothing by default.** No trace text, no memory write, no cache and no analytics content for a conversation in this lane. A deployment may choose otherwise, and the choice is recorded.
7. **A person can be reached.** Where a deployment has staff or a partner service, the pack can hand off to them with the person's consent and the minimum of information.
8. **Deployments decide whether to host it.** A deployment may configure the pack to decline such conversations and direct the person to resources instead.

## 4) Goals & Success Metrics

- **Primary goal:** in a conversation that touches mental health, the response is safe, honest and consistent, and the conversation is retained only as the deployment decided.
- **Success metrics:**
  - On a curated test set of crisis-indicating messages, including indirect and multilingual phrasings, the required resources appear in 100 percent of responses, and the test set grows from real misses.
  - On the override battery (ADR-160), zero attempts remove the crisis response or the disclosure.
  - Zero responses contain a diagnosis or treatment recommendation, verified by the post-generation verifier on the test set and in sampled production.
  - With the default storage rule, the verification sweep finds zero conversation text in traces, memory and caches.
  - A purge of a conversation produces a receipt (ADR-159) within the regime's clock.
  - False positives are measured and reported, because an over-triggering pack makes a normal assistant unusable.

## 5) Key Users / Personas

- **A person in distress:** needs a calm, honest reply and a way to reach a human. May be a minor.
- **The deployer's compliance or safety lead:** decides whether to host such conversations, sets the resources and the retention, and answers for the behavior.
- **A support or clinical partner:** receives a handoff, with consent and minimal information.
- **An auditor:** asks how the deployment behaved without reading anyone's private words.

## 6) Scope: capabilities by phase

**Phase 0: the policy.**
1. The regime profile for sensitive conversations: content class, handling rule (store nothing), required disclosure, and the verifier rules (no diagnosis, no treatment advice, resources present when triggered).
2. Locale-keyed crisis resources as configuration, with a US default.

**Phase 1: detection and response.**
1. A deterministic detector provider for crisis and self-harm cues and for statements of mental-health condition, with a published false-positive and false-negative rate on a test set.
2. The crisis response and disclosure in the immutable policy tier, with the post-generation verifier.
3. A "decline and redirect" mode for deployments that do not host these conversations.

**Phase 2: retention, handoff and proof.**
1. The conversation-lane storage rule enforced at traces, memory, caches and analytics.
2. A consent-based handoff hook that sends the minimum to a configured human or service.
3. Counts-only audit: that the lane triggered, the response given and the handoff outcome, with no conversation text.
4. Purge integration, so a conversation can be destroyed on request with a receipt.

**Phase 3: hardening.**
1. The override and indirect-phrasing battery, with a negative control.
2. Age handling configuration for deployments that may serve minors.
3. A review cadence for the jurisdiction map, since the law is changing.

## 7) Non-goals

- Providing therapy, counselling, diagnosis or treatment, or implying that the assistant does.
- Replacing a professional or an emergency service.
- Guaranteeing any outcome. The pack reduces risk and records behavior.
- Deciding for a customer that they may legally host such conversations. That needs their counsel.

## 8) Non-Functional / Constraints

- **Safety first:** a verifier failure produces the safe response, never silence and never the unchecked answer.
- **Latency:** the crisis check adds under 20 ms; the response may be a fixed, localized text so it does not wait on a model.
- **Privacy:** the lane's logs hold counts and identifiers only.
- **Localization:** resources and phrasing are configuration per locale; detection is tested per language the deployment supports.
- **Optionality:** the pack is off unless a deployment enables it, and its absence is itself a documented posture.

## 9) Open decisions for the owner

1. **Ship it, or ship it as a reference pack.** Given the 2025 state laws, I would ship it as a reference pack with a prominent statement that a customer's counsel must review it for their jurisdictions, and with decline-and-redirect as the default mode.
2. **Default for storage.** Proposed: store nothing, as in principle 6.
3. **Handoff.** Is a human handoff in scope for the first customers, or is decline-and-redirect enough?
4. **Minors.** Does any planned customer serve people under 18? That changes the defaults.
5. **Languages.** Which languages must detection cover first?
6. **Legal and medical-advice disclosures.** Should the first release include the not-a-lawyer, not-privileged disclosure for legal questions? I would, since the downside is waiver of a user's privilege.

## 10) Evidence behind this document

- The state laws summarized in section 2, from secondary reporting gathered 2026-10-05 (for example [eWeek on Illinois](https://www.eweek.com/news/illinois-bans-ai-mental-health-therapy/) and a [state-law roundup](https://www.wisnerbaum.com/ai-chatbot-lawsuit/state-chatbot-law/)); to be verified against the statutes.
- GDPR Article 9 (special categories of data) and the HIPAA definition of protected health information, which together make the conversation content regulated wherever they apply.
- ADR-152 and ADR-160, which fix how detection and the unbreakable response are built.

## 11) Acceptance & rollout

- **Sign-off:** the owner approves the principles and the default mode; counsel reviews the jurisdiction map; a clinical advisor reviews the crisis response text and the test set.
- **Rollout:** report-only first, logging what the lane would have done with counts only, then decline-and-redirect, then any hosted mode, each per deployment.
- **Rollback:** the pack is a setting. Turning it off returns the deployment to its prior behavior and removes no records.
- **Done when:** the battery passes with its negative control, a clinical advisor has reviewed the responses, and a deployer's counsel has signed off on the mode they chose.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
