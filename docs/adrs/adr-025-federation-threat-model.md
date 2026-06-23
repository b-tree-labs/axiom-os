# ADR-025: Federation Threat Model (Formal)

**Status:** Proposed
**Date:** 2026-04-15
**Authors:** Benjamin Booth, Claude
**Related:** ADR-021 (earlier lightweight threat notes — this ADR supersedes), ADR-016 / 022 / 023 / 024, `spec-classification-boundary.md`, `prd-federation.md §17`.

---

## Purpose

Axiom federation at the 10k–100k-node scale across universities,
agencies, and industry consortiums faces a substantial and
adversarial threat surface. ADR-021 sketched the top-level
threats; this ADR formalizes the model so there is a single
authoritative document for security review.

For each threat: **adversary**, **capabilities**, **goals**,
**attack shape**, **impact if successful**, **mitigations** (with
citations to the ADRs that implement them), **residual risk**,
and **detection strategy**.

The mitigations listed here are NOT new designs. They are
pointers to decisions already made in ADR-022/023/024 and
`spec-classification-boundary.md`. This ADR is the map from
threat to mitigation.

**Not a certification.** This is a design-level threat model;
per-deployment accreditation (ATO, IATT, SOC 2, etc.) is
separate, involves controls frameworks (NIST 800-53, CNSSI
1253), and is out of scope here.

---

## 1. Adversary Taxonomy

| Adversary | Typical Capabilities | Typical Goals |
|-----------|---------------------|---------------|
| **Outsider** | Can send arbitrary network traffic; cannot read network traffic protected by TLS/SSH | Reach federation resources, deny service, exfiltrate public data |
| **Network-local attacker** | Controls a LAN / WiFi / upstream router; can MITM unprotected traffic | Intercept credentials, impersonate peers, modify in-flight data |
| **Malicious peer** | Has a legitimate identity AND federation membership | Abuse access, escalate privilege, exfiltrate data they can see, poison knowledge corpus |
| **Compromised peer** | Legitimate member whose host or keys were taken over | Same as malicious peer, but discovery/recovery differs |
| **Insider with partial privilege** | Legitimate identity with some privilege (member, not admin) | Escalate to admin, exfiltrate cross-scope data, pivot |
| **Nation-state / supply-chain** | Can compromise PyPI, GitHub CI, transitive deps, cryptographic libraries | Backdoor the platform itself; affects every deployment |
| **Cleared-but-curious** (classified contexts) | Has the required clearance but not need-to-know | Access compartmentalized content outside their scope |
| **Unauthorized foreign national** (export-control) | Has federation membership but wrong nationality | Access ITAR/EAR-restricted content |
| **Physical-access attacker** | Can steal hardware, boot from media, extract keys at rest | Get root identity; impersonate an identity permanently |

---

## 2. Threat Catalog

### 2.1 Identity & Key Management

#### T-ID1: Silent key substitution (MITM during identity fetch)

- **Adversary:** network-local attacker on the SSH path between
  two peers at identity-binding time.
- **Goal:** insert their own key as the bound identity for a
  claimed peer.
- **Attack:** intercept the SSH connection during
  `axi nodes add`, substitute `axi federation status --json`
  response with attacker's pubkey.
- **Impact:** victim peer persistently trusts attacker's key as
  the legitimate peer identity; every subsequent verification
  succeeds for the attacker.
- **Mitigations:**
  - SSH itself resists MITM via host-key verification (ADR-016
    §9 trust bootstrap).
  - Fingerprint is emitted on bind for out-of-band confirmation
    (memory: human-in-the-loop confirmation before trust)
    (ADR-022 TOFU flow).
  - TOFU refusal on silent key change — if this peer was
    previously bound with a different key, loud refusal
    (ADR-022 + this session's TOFU-on-transport fix).
- **Residual risk:** first-time bind with a compromised SSH
  host key plus skipped OOB confirmation. Mitigated by
  operator discipline; ADR-023 §4 multi-source attestation at
  join closes this further when the peer joins a federation.
- **Detection:** fingerprint mismatch on OOB comparison;
  revocation channel if compromise discovered later.

#### T-ID2: Root-key compromise

- **Adversary:** physical access, exploit against a root-key
  custodian, insider.
- **Goal:** produce arbitrary federation signatures under the
  legitimate root identity.
- **Impact if single-key root:** federation-wide compromise. Any
  message, manifest, or intermediate can be forged.
- **Mitigations:**
  - Threshold-signed roots (ADR-024 §1, FROST) — no single
    compromise reconstructs the key.
  - Root keys rotate periodically with bidirectional
    attestation (ADR-024 §5).
  - Classified / air-gapped deployments keep root custody
    offline (`spec-classification-boundary.md §S8`).
- **Residual risk:** threshold compromise (k-of-n signers
  colluding or simultaneously breached). Mitigated operationally
  by geographic/organizational diversity of signers.
- **Detection:** unusual signing activity, manifests appearing
  out-of-schedule, cross-check against out-of-band operator
  logs.

#### T-ID3: Forged intermediate signing key

- **Adversary:** attacker with brief access to an intermediate
  key (leaked log, stolen key file, compromised signing host).
- **Goal:** sign malicious manifests or security directives.
- **Impact:** up to 7 days of arbitrary federation operations
  signed as the federation.
- **Mitigations:**
  - Intermediate TTL = 7 days (ADR-024 §2) bounds exposure.
  - Emergency intermediate revocation via root quorum
    (ADR-024 §4).
  - Per-intermediate monotonic sequence numbers detect
    replayed operations from a since-revoked intermediate.
- **Residual risk:** 7-day window is the worst case. Short TTL
  is the tradeoff against operational cost.
- **Detection:** revocation channel + out-of-schedule
  intermediate rotation signals operator of an incident.

#### T-ID4: Clearance or affiliation forgery

- **Adversary:** compromised/rogue authority (agency, registrar,
  HR office) that signs attestations.
- **Goal:** claim clearance / affiliation / nationality the
  principal does not actually have.
- **Impact:** unauthorized access to classified, export-
  controlled, or org-proprietary content.
- **Mitigations:**
  - Multi-authority cross-signing for high-value attestations
    (classified clearance requires two independent signatures
    per DoD practice; not-day-1 but design allows).
  - Attestation TTL (short: 1 year typical for clearance)
    forces re-issuance; if original authority is rotated out
    during the window, renewal signals the compromise.
  - `spec-classification-boundary.md §S9` revocation
    propagation — revoked attestations refused federation-wide
    on policy-timed propagation.
- **Residual risk:** single-authority compromise between
  re-adjudication cycles. Mitigated by operational controls at
  the authority.
- **Detection:** revocation channel events; anomalous access
  patterns for newly-cleared principals.

---

### 2.2 Membership & Manifest Channel

#### T-MB1: Manifest replay / rollback

- **Adversary:** network-local attacker with access to prior
  manifests; malicious peer serving stale manifests.
- **Goal:** reintroduce a previously-expelled member, or
  suppress knowledge of a newly-admitted member.
- **Mitigations:**
  - Monotonic sequence numbers per manifest — peers refuse
    sequence ≤ last-accepted (ADR-022 §4, ADR-024 §3).
  - Hard-expiry TTL — stale manifests refused regardless of
    sequence (ADR-024 §3).
  - Content-addressed hashes + signed-by-intermediate check
    detect tampering.
- **Residual risk:** gap between soft-expire and hard-expire
  when a root outage happens; 24h window of stale-but-valid
  manifest. Detection via warning logs at soft expiry.
- **Detection:** manifest signature mismatch; intermediate
  expired; sequence regression.

#### T-MB2: Eclipse attack on joining node

- **Adversary:** network-level attacker controlling the
  joining node's only visible peers.
- **Goal:** present a false view of federation membership
  (fake admission, spoofed root, invisible real federation).
- **Mitigations:**
  - Multi-source attestation at join (ADR-023 §4) — three
    independent signatures (invite-giver + federation root +
    self-chosen additional member) must converge on same
    manifest sequence/hash.
  - Probation state for new members (ADR-023 §2) — even a
    successful eclipse cannot immediately write to federation
    state.
- **Residual risk:** attacker who controls invite-giver AND
  federation root AND at least one member. At that scale, the
  federation is already compromised, not eclipsed.
- **Detection:** manifest-hash divergence between attestation
  sources; joining-node abort on mismatch; operator escalation.

#### T-MB3: Sybil injection

- **Adversary:** malicious operator spinning up many synthetic
  nodes to influence federation state.
- **Goal:** outvote legitimate members on governance, poison
  knowledge corpus via fake corroboration, exhaust federation
  resources.
- **Mitigations:**
  - Admission requires a signed invite + inviter approval
    (ADR-016 §9).
  - Probation window before new members can vote/contribute
    (ADR-023 §2).
  - Cross-institutional federations anchor to institutional
    identity authorities (ADR-022 §1) — Sybil creation is
    bounded by what the institution would endorse.
  - Corroboration-based knowledge promotion (prd-federation
    §8.6) weights independent-source evidence — not source-
    count, making duplicate-origin claims transparent.
- **Residual risk:** open federations that accept self-serve
  membership are inherently Sybil-susceptible; explicit
  tradeoff in ADR-023 lifecycle §2. Mitigation in those
  contexts is capability-gating (unprivileged tier only).
- **Detection:** admission pattern analysis (many admissions
  from same inviter, similar naming, correlated IPs);
  corroboration-weight anomalies.

#### T-MB4: Membership grant abuse by compromised admitter

- **Adversary:** legitimate member whose host or key has been
  compromised, or rogue insider with admission authority.
- **Goal:** admit attacker's identities into the federation.
- **Mitigations:**
  - Admissions are logged + signed (audit trail).
  - Root quorum review — federation root signs (or co-signs
    via intermediate) every admission; unusual patterns are
    visible at the root level (ADR-024 manifest flow).
  - Probation window on admitted members (ADR-023 §2) —
    limited blast radius.
- **Residual risk:** a compromised admitter admitting other
  compromised identities is a coordinated multi-party compromise
  — mostly out of scope for protocol-level mitigation.
- **Detection:** anomalous admission rate from a specific
  admitter; operator review of admission audit logs.

---

### 2.3 Topology & Propagation

#### T-TP1: Hierarchical propagation poisoning

- **Adversary:** compromised intermediate node in a hierarchical
  federation.
- **Goal:** corrupt manifests flowing downstream to leaves.
- **Mitigations:**
  - Manifests are signed by the federation root (or intermediate
    attested by root), not by the intermediate tree node — a
    hierarchical-propagation node is a cache, not an authority
    (ADR-023 §3).
  - Leaves verify signatures against the root's public key;
    a compromised intermediate can drop or delay but cannot
    tamper.
- **Residual risk:** denial-of-availability downstream. Mitigated
  by peers falling back to pull directly from root on
  mismatch.
- **Detection:** signature mismatch; manifest sequence anomaly
  on leaves; heartbeat aggregation mismatches upstream.

#### T-TP2: Cross-bridge data smuggling

- **Adversary:** compromised or malicious bridge operator.
- **Goal:** smuggle content that would be refused by the more-
  restrictive side's rules (e.g. export-controlled content
  leaking to non-authorized federation).
- **Mitigations:**
  - Bridge enforces the more-restrictive side's rules
    deterministically (ADR-023 §5) — classification,
    export-control, proprietary.
  - Bridge operations are explicit, audited, and rate-
    controlled; opportunistic tunneling requires operator
    action.
  - Content stamps travel (`spec-classification-boundary.md
    §2.1`) — receiving side re-verifies stamp against its
    own policy.
- **Residual risk:** a bridge operator who deliberately
  bypasses deterministic checks is a protocol-level attacker
  (breaking the enforcement code). Detection is
  defense-in-depth: both sides log and cross-check.
- **Detection:** content-stamp violations on receiving side;
  bridge-node operation rate anomalies.

#### T-TP3: Coordinator election hijacking

- **Adversary:** sybil + collusion in flat-mesh or hub-spoke
  topologies.
- **Goal:** become the elected coordinator to gain routing
  visibility.
- **Mitigations:**
  - Coordinator is an optimization, not an authority — the
    mesh continues to function if coordinator fails
    (ADR-016 §8).
  - Coordinator selection weighted by node profile (validated
    classification — provider/coordinator profiles require
    sustained uptime and contribution history per ADR-023
    §6), not by a single-round vote.
- **Residual risk:** coordinator gets visibility into routing
  metadata, not content. Protocol-level content access still
  requires proper membership/clearance.
- **Detection:** profile-validation drift; unusual coordinator-
  role transitions.

---

### 2.4 Install / Upgrade Surface

Enumerated in `prd-federation.md §17`; summarized threats here:

#### T-IU1: Supply-chain compromise (PyPI / CI / transitive dep)

- **Adversary:** nation-state, registry attacker, or compromised
  dependency maintainer.
- **Goal:** inject malicious code that every Axiom deployment
  executes on update.
- **Mitigations:**
  - Wheel signing + Sigstore attestations (ADR-017 §3).
  - Clean-install validation test (`tests/install_path/`)
    catches certain regressions (branding-squatter class).
  - Package-name integrity test pins the distribution name
    against known-owned registry (v0.10.6 fix + regression
    test).
  - Emergency upgrade channel (ADR-024 §6) gets a security
    directive out within hours, limiting exposure window.
- **Residual risk:** the Axiom release pipeline itself is a
  single signing authority. Threshold-signed releases are a
  potential future mitigation.
- **Detection:** Sigstore log divergence; package-hash
  mismatch between two independent verifiers.

#### T-IU2: Downgrade attack on upgrade

- **Adversary:** network-local attacker during `axi update`.
- **Goal:** force a peer to downgrade to a vulnerable version.
- **Mitigations:**
  - `axi update` pulls from HTTPS-authenticated PyPI
    (certificate pinning acceptable but not implemented — see
    residual).
  - Peer version preflight (`MIN_PEER_VERSION_FOR_IDENTITY_
    BINDING`) — federation operations refuse peers below
    minimum, so a downgraded peer loses federation trust.
  - Emergency upgrade directive can mandate a minimum version
    federation-wide (ADR-024 §6).
- **Residual risk:** HTTPS CA compromise; pinned version in
  operator policy can be manipulated. Defense-in-depth via
  federation-root security directive.
- **Detection:** version-skew telemetry; peer-preflight refusal
  events.

#### T-IU3: Silent upgrade failure masking compromise

- **Adversary:** opportunistic — takes advantage of upgrade
  tooling that reports success while failing critical steps.
- **Goal:** leave a node on vulnerable code while the operator
  believes it's upgraded.
- **Mitigations:**
  - `axi update` fail-stop (v0.10.7) — any step failure aborts
    the upgrade and reports clearly.
  - Extension return-code propagation (v0.10.9) — non-zero
    subprocess returns reach the process exit code.
  - Install-path validation test exercises these paths from
    fresh PyPI.
- **Residual risk:** novel failure modes not exercised by the
  current test matrix. Mitigated by expanding the test
  matrix iteratively.
- **Detection:** mismatched `axi --version` across supposedly-
  upgraded cohort; federation security directive would flag
  down-level peers.

---

### 2.5 Classification & Export-Control

See `spec-classification-boundary.md §2–§4` for full model and
invariants. Threats here summarize the adversarial view.

#### T-CL1: Unauthorized cross-domain access (classified)

- **Adversary:** cleared-but-not-need-to-know; compartment-
  outside attacker.
- **Goal:** read classified content outside their access.
- **Mitigations:**
  - Content classification stamps checked deterministically on
    every access (`spec-classification-boundary §2.1`).
  - Federation domain floor — federation cannot handle content
    above its declared domain (§2.3).
  - Compartmentalization checks (§S11).
- **Residual risk:** implementation bugs in the stamp-check
  path. Mitigated by comprehensive tests + formal accreditation
  review for classified deployments.
- **Detection:** audit-log anomalies; access attempts refused
  are logged for review.

#### T-CL2: Spillage (accidental cross-domain exposure)

- **Adversary:** not strictly adversarial — mistake or
  misclassification.
- **Goal / harm:** classified content in an unclass channel.
- **Mitigations:**
  - Up-transfer is explicit (§S4) — no accidental mechanism.
  - Bonsai LM + classifier models flag candidate content for
    human review on ingest (model-mediated; gates are
    deterministic policy).
  - Spillage response protocol (§S10) — purge, no soft-delete
    at classified boundaries.
- **Residual risk:** detection latency; some spilled content
  may propagate before discovery.
- **Detection:** automated pattern-match for classification
  markings; human reports; periodic audits.

#### T-CL3: Foreign-national EAR/ITAR bypass

- **Adversary:** foreign national with federation membership
  but not export-control authorization.
- **Goal:** access ITAR/EAR-restricted content.
- **Mitigations:**
  - Nationality is a signed identity attestation (§2.2).
  - Deterministic export gate per-access (§S7).
  - Federation policy declares authorized-nationalities list;
    content stamps enumerate; check is per-access.
- **Residual risk:** identity-attestation forgery (T-ID4).
- **Detection:** audit-log review; export-compliance reporting.

---

### 2.6 Agent & Model-Mediated Surface

#### T-AG1: SKILLS.md tampering

- **Adversary:** insider, compromised repo, or bad-faith
  contributor.
- **Goal:** modify agent behavior to exfiltrate, mislead, or
  act outside declared scope.
- **Mitigations:**
  - `spec-security.md §2.3` — SKILLS.md is model-mediated
    shaping ONLY; it never grants capability. Deterministic
    gates (RACI, OpenFGA, crypto sigs) reject unauthorized
    actions regardless of SKILLS.md contents.
  - Blast radius: behavioral weirdness, not authorization
    bypass.
- **Residual risk:** the behavioral weirdness can still mislead
  operators into taking bad actions (e.g. approving an
  apparently-legitimate request that's actually malicious).
  Mitigated by operator training + audit logging of approvals.
- **Detection:** agent output anomalies; SKILLS.md diff review
  at PR time; operator reports of "this agent is acting odd."

#### T-AG2: LLM prompt injection

- **Adversary:** content (document, message, pasted data) that
  contains prompt-injection attempting to manipulate an agent's
  LLM.
- **Goal:** cause the agent to take unintended actions, leak
  data, bypass guidance.
- **Mitigations:**
  - Authorization is deterministic — prompt injection cannot
    grant capability (`spec-security.md §2.2`).
  - Agents escalate high-risk actions through RACI
    (deterministic approval gate).
  - Ingest content sanitization (e.g. PRESS content gate) flags
    candidate injections.
- **Residual risk:** subtle behavioral manipulation that doesn't
  trigger escalation but degrades agent usefulness. Mitigated
  by robust system prompts and output validation.
- **Detection:** pattern-match on known injection markers;
  LLM-as-judge evaluation of outputs; operator review.

#### T-AG3: Model substitution / inference poisoning

- **Adversary:** supply-chain attacker who replaces a model
  binary or serves a modified model at inference time.
- **Goal:** cause the agent to give wrong classification /
  summarization / classification outputs.
- **Mitigations:**
  - Model identity is signed + verified (ADR-012 provider
    identity).
  - For classified contexts, only accredited models are
    permitted and their identity is tightly controlled
    (`spec-classification-boundary.md §S12`).
  - Model outputs ARE ADVISORY at authorization boundaries —
    deterministic gate makes the real decision.
- **Residual risk:** subtle accuracy degradation over time;
  hard to detect without ground-truth evaluation.
- **Detection:** eval-suite regressions; cross-model
  consistency checks.

---

## 3. Summary: Defense-in-Depth Posture

The same adversary must bypass **all** of the following to
achieve federation-wide compromise:

```
Threshold-signed root        (ADR-024 §1)
→ Intermediate signing key    (ADR-024 §2, 7-day TTL)
→ Membership manifest check   (ADR-022 §4, monotonic + TTL)
→ Peer identity verification  (ADR-022 TOFU, transport-keyed)
→ Membership attestation      (ADR-023 §4, multi-source)
→ Classification stamp check  (spec-classification §2)
→ Export-control gate         (§S6/S7 deterministic per access)
→ RACI authorization gate     (code, not LLM)
```

Defense-in-depth is the design principle. No single gate is
load-bearing alone.

---

## 4. Open / Emerging Threats

Tracked but not fully mitigated; follow-up work:

- **Novel cryptographic attacks on Ed25519 / FROST** — would
  require re-keying the federation. Mitigation requires
  cryptographic agility in the protocol, which is today's
  design but not explicitly tested.
- **Side-channel attacks on signing hosts** — timing, power,
  cache attacks against root-quorum signers. Mitigation is
  operational (HSM, isolated signing environment).
- **Machine-learning backdoors** in the LLM itself — training-
  time attacks producing covertly biased models. Out of
  Axiom's mitigation scope; upstream model provider concern.
- **Governance attacks** — legitimate federation processes
  (admission rules, voting, policy changes) manipulated to
  achieve attacker goals. Mitigated by audit + operator
  review, not by protocol.

---

## 5. Relation to ADR-021

ADR-021 was a lightweight threat-sketch that accompanied the
initial federation ADR-016. This ADR (025) **supersedes** ADR-
021 in scope; ADR-021 remains as historical context. Future
amendments to the threat model happen here.

---

## Decisions Pending Ben Review

1. **Adversary coverage** — are there threat classes missing?
   Industry-regulatory (HIPAA, PCI, etc.) and international
   (GDPR data-locality) were not enumerated; may warrant
   addition.
2. **Residual-risk acceptance** — several threats list residual
   risks accepted with operational mitigations. Worth a
   separate "risk acceptance" document that operators sign?
3. **Standalone vs amendable** — this ADR is long; future
   threats likely. Maintain as a living doc (periodic
   amendments) or spawn ADR-025.1 / .2 / ... for each new
   threat batch?

---

## Related Documents

- ADR-021 — earlier lightweight threat notes (historical).
- ADR-022 — identity + membership data model (mitigations
  cited throughout).
- ADR-023 — topology, lifecycle, propagation, join handshake.
- ADR-024 — root availability, delegation, key hygiene,
  emergency upgrade channel.
- `spec-classification-boundary.md` — classification, export-
  control, cross-domain transfer scenarios and invariants.
- `spec-security.md §2` — deterministic vs model-mediated
  trust model; SKILLS.md tamper resistance.
- `prd-federation.md §17` — install/upgrade scenarios; source
  of T-IU threats.
- ADR-017 — release pipeline, supply-chain attestation; source
  of T-IU1 mitigations.
- ADR-012 — provider identity, model signing.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
