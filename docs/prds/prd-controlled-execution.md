# PRD: Controlled execution, running a controlled code only through an authorized person

**Product / Feature:** Controlled execution: a platform feature for running any controlled code, at any site, on any scheduler, only through a person who holds the privilege to run it.

**Owner:** Axiom platform   •   **Status:** Draft   •   **Last updated:** 2026-11-09

**Related:** `axiom.governance.controlled_code` (the registry and its doctrine), [ADR-175](../adrs/adr-175-export-control-is-enforced-at-the-code-not-read-from-result-text.md) (export control is enforced where the code is held and run), [ADR-158](../adrs/adr-158-export-control-health-privacy-and-personal-data-are-regimes-of-one-mechanism.md) (regimes of one mechanism), [ADR-152](../adrs/adr-152-the-semantic-classifier-advises-and-labels-lists-and-agreements-decide.md) (lists and agreements decide), [prd-access-agreements](prd-access-agreements.md), [prd-compute-decomposition](prd-compute-decomposition.md) and [ADR-040](../adrs/adr-040-compute-decomposition.md) (compute decomposition and the HPC submit-as-leaf phase), [ADR-045](../adrs/adr-045-raci-evolution.md) (RACI tiers), [ADR-086](../adrs/adr-086-authenticated-delegation-token-exchange.md) (delegation token exchange), [ADR-142](../adrs/adr-142-attestation-is-the-record-of-accountable-human-acts.md), [ADR-144](../adrs/adr-144-a-person-confirms-exactly-what-is-recorded.md) and [ADR-150](../adrs/adr-150-an-attestation-record-is-kept-in-a-logbook.md) (attestation), [prd-attestation](prd-attestation.md)

**First consumer:** a consumer layer whose physics extensions wrap controlled codes and declare an HPC target. Its needs are specified in its own repository.

---

## 1) Elevator pitch

A person who has no access to a controlled code, or an AI agent working for them, asks for a run of it. A person who does hold the privilege to run that code reviews exactly that request, approves it, and the platform runs it on the site's cluster under that person's identity. The results come back to the requester as ordinary data, and every step is on the record.

## 2) The rule this PRD builds on

The owner stated the rule on 2026-11-09. This PRD treats it as settled and builds on it.

1. **Export control attaches to two things only.** The first is access to a controlled code's bits: its source and its compiled binaries. The second is the execution of a controlled code.
2. **Everything around the code is not export controlled.** Software that calls or depends on a controlled code, the data sent to it, and the results it returns are not export controlled. Such material may still be access controlled for other reasons (proprietary value, personal information), which is a separate and lighter regime (ADR-158).
3. **Every run goes through an authorized person.** Anyone who wants a job that runs a controlled code on a high-performance cluster must have the request go through a person who has access to the code and the privilege to run it.
4. **Axiom manages this generically.** Executing a controlled binary is a platform feature. It is not a feature of any one consumer, code or scheduler.

This is consistent with the doctrine already written in `axiom.governance.controlled_code` and with ADR-175: the registry declares, it never enforces, and enforcement belongs where the code is held and run. ADR-175 names two such places: filesystem permissions for a local run and "the job broker's authorized-persons check" for a managed run. One consequence follows directly from the rule. A code adapter that only wraps or calls a controlled code is ordinary software, unless it contains the code's bits. A site may still keep such an adapter private for other reasons.

## 3) Problem and opportunity

- **The job broker the doctrine relies on does not exist.** The registry's docstring and ADR-175 both place managed-run enforcement in a job broker that checks the authorized-persons list. On main there is no broker, no HPC executor, and nothing that resolves an `authorized_persons_ref`. The doctrine is correct and the enforcement point it names is missing. Until it exists, the only control on a managed run is whatever a person does by hand.
- **People who need results cannot get them without becoming a holder.** A student, a collaborator or an analyst who needs the output of a controlled code today has two options. They can obtain access to the code, which is slow, may be impossible for them, and widens the set of holders for no reason. Or they can ask an authorized colleague informally, by email or chat, which leaves no record binding what was asked to what was run.
- **Agents make this sharper.** An AI agent can now prepare an input, pick resources and ask for a run. Without a platform path, the agent either stops or is tempted toward a shortcut. The platform needs a path on which an agent can do all the work that is not controlled and none of the work that is.
- **Each consumer would otherwise build its own.** A consumer layer that wraps controlled codes declares a `controlled` control class and an HPC target, and expects the platform's executors to enforce it. If every consumer builds its own approval and submission path, the copies drift and each one has to be reviewed by the site's export-control office separately.
- **The opportunity.** Most of the parts already exist: the controlled-code registry, the approval queue and its plan-then-apply digest binding, the approval doorbell on interactive channels, confirmation of exactly what is recorded, attestation logbooks, delegation tokens that name both subject and actor, the RACI tiers, and the compute dispatch primitive with its signed receipts. Controlled execution is mostly a composition of these, plus a broker and an HPC executor.

## 4) Personas

| Persona | What they need to do | What they never do |
|---|---|---|
| **Requester without access** | Prepare inputs, choose a code and version, ask for a run, follow its status, receive results. Technical level ranges from student to senior analyst. | Hold, see, copy or run the code's bits. |
| **Authorized runner** | See pending requests for the codes they may run, review exactly what each will do, approve or refuse with a reason, and have the run happen under their identity. A practitioner with a cluster account. | Approve a run that differs from what they reviewed. |
| **Site export-control approver** | Declare which codes are controlled at the site and on what basis, and decide who may hold and who may run each one. An export-control officer or delegate; may not be technical. | Need to read inputs or results to do this. |
| **Auditor** | Reconstruct, for any run or any period, who asked, who approved, what exactly ran, where, under whose identity, and where the results went. Internal compliance or an outside reviewer. | Need any access to the code. |
| **AI agent acting for a requester** | Do the requester's uncontrolled work: draft inputs, file the request, track it, collect and present results. | Hold the bits, approve a request, or be the identity a run executes under. |

## 5) Goals and success metrics

**Primary goal:** every execution of a controlled code that goes through Axiom is a request that an authorized person approved, bound to the exact request, run under that person's identity, and recorded end to end. No other path through Axiom runs a controlled code.

| ID | Metric | Target |
|---|---|---|
| M1 | Submissions of a controlled code that carry an approval whose digest matches the submitted request | 100%, enforced in code and asserted by a conformance test |
| M2 | Controlled-code executions whose run identity is an AI agent or a service principal without a named person | 0, asserted by a conformance test |
| M3 | Requesters who obtained read access to a controlled code's bits in order to get results | 0 for requests completed through this path |
| M4 | Runs an auditor can reconstruct from the record alone (request, approval, submission, job, results, delivery) | 100% in an audit drill |
| M5 | Refusals that state a reason and a next step a person can act on | 100%, asserted per refusal path in tests |
| M6 | Median time from a filed request to an approval decision, at a site with at least two authorized runners | Tracked from the first pilot; a target is set after the first month of data |

## 6) Requirements

Each requirement says which existing mechanism it rides. Where it says "new", nothing on main does the job today.

**R1. Registry of controlled codes and authorized persons, per site.**
A site declares its controlled codes in the existing controlled-code registry and declares, per code, two separate lists: the persons who may hold the bits and the persons who may run the code. The registry already carries `AccessRule.authorized_persons_ref`; this requirement makes that reference resolvable. The lists are not a new store. A person is on a list because they hold the access agreement the site requires for that code, recorded as an attestation (prd-access-agreements, ADR-142), with its grantor, date and expiry. The registry's three standings stay as they are: a code whose standing is `UNKNOWN` is not treated as `NOT_CONTROLLED` (see Q2). Acceptance: given a site, a code and a version, the platform can answer "who may run this now" and "who may hold this now" with the record behind each name.

**R2. A run request that never touches the bits.**
A requester, or an agent acting for one, files a run request naming: the code by its registry name, the version, the input files or references, the resources (cores, memory, accelerators, wall time), the target cluster and partition or queue, where results should be delivered, and a free-text purpose. The request is data. Filing it requires no access to the code, and the requester's session never resolves the code's install path. The platform computes a digest of the canonical request, in the same way `plan_apply.propose` hashes a plan subject today, and inputs are included by content hash so the digest covers them. A request may not carry a credential; the existing `propose` refusal of credential-shaped subjects applies. Acceptance: a requester with no rights on the code can file a valid request, and the request has a stable digest.

**R3. Review and approval by an authorized person, bound to the request digest.**
The request becomes a held action in the existing approval queue (`ApprovalGate`), using the plan-then-apply mechanism (`plan_apply.propose` and `apply`) rather than a new approval store. Only a person on the run list for that code and version at that site may approve it. The approver sees the request through the confirmation mechanism of ADR-144, so what they approve is byte for byte what is recorded, and the presentation carries the digest. The approval doorbell is the existing approval bridge (`notify_pending`), which already withholds parameters from a channel whose classification ceiling does not admit them. The approval is recorded as an attestation in a site logbook (ADR-142, ADR-150). An approval names the digest it approves and expires (see Q4). Any change to the request after approval, including a changed input file, yields a new digest and needs a new approval. Acceptance: the broker refuses a submission whose digest does not match an unexpired, unused approval by an authorized runner.

**R4. Execution by the authorized person's identity, through the job broker, on the HPC executor.**
New: a **job broker** and an **HPC executor**. The broker is the enforcement point the registry doctrine and ADR-175 already name. At submission it re-checks, at that moment and not from a cached answer: that the approval exists, matches the digest, is unexpired and unused (the five checks `plan_apply.apply` already performs); that the approver is still on the run list; and that the code's registry entry has not changed since approval. It then submits the job to the site's scheduler under the authorized runner's identity. Where a service process does the submitting, it does so with a delegation token whose subject is the runner and whose actor chain names the broker (ADR-086), so the record never shows a run that belongs to no person. The executor runs the code through the existing compute adapter interface (`axiom.compute.adapters.CodeAdapter`) and produces the existing signed dispatch receipt. Acceptance: every controlled submission is attributable to one named authorized person, and the broker is the only Axiom path that submits a controlled code.

**R5. Results returned to the requester as ordinary, uncontrolled data.**
Results are not export controlled, so they are not labelled export controlled and are not withheld at the sink for naming the code (ADR-175). They land like any other compute result: the signed receipt, the declared outputs, and a run record with timings and resources. Because results may leave and bits may not, the broker collects only the outputs the adapter declares, never the whole run directory, so a binary, a library or a source listing that happens to sit in the working directory cannot leave with them. Results may still carry an access-control label that the site or the requester sets for other reasons. Acceptance: the requester receives the declared outputs and the receipt, and a collection test shows that an executable placed in the run directory is not collected.

**R6. Refusal paths that give a reason and the next step.**
Every refusal says what was refused, why, and what a person can do next, using the existing chain envelope (`unmet` with the exact fix). The paths include: the code is not declared at this site (next step: ask the site export-control approver to declare it); the code is controlled and the requester asked for a direct run (next step: file a request; here is how); no authorized runner exists for this code and version (next step: who can add one); the approval does not match the request (next step: the request changed, re-approve); the approval expired or was used; the runner lost authorization since approving; the target cluster is not an authorized enclave for this code; the scheduler refused the job (the scheduler's own reason, passed through). Acceptance: a test exists for each refusal path asserting that a reason and a next step are present.

**R7. Access to the bits is governed separately from execution.**
Holding, copying, building and installing a controlled code are acts on the bits and are governed by the hold list from R1, not by the run-approval flow. A run approval never grants access to the bits, and access to the bits never by itself approves a run through the broker. Installing a package that contains a controlled code on a host that is not an authorized enclave for it is refused at install and refused again at run. Copy and build events that pass through Axiom tooling are recorded. Acceptance: no run-approval record can be used to read, copy or install the code, and an install on an unauthorized host is refused with a reason.

**R8. A complete audit trail.**
Each step leaves a record: request filed (by whom, on whose behalf, with which digest), approval or refusal (an attestation), submission (scheduler, job id, runner identity, actor chain), completion or failure, results collected (output digests) and results delivered (to whom). Human acts go to the attestation logbook; machine events go to the existing audit trail and the dispatch receipt. Retention follows the export-control profile of ADR-158. The record holds digests and references, not the code's bits. Acceptance: an auditor can answer "who caused this run, who allowed it, what exactly ran, and where did the results go" from the record alone.

**R9. No AI agent ever holds or runs the bits.**
An agent may draft inputs, file a request on a person's behalf (with a delegation token naming that person as subject, ADR-086), track it, and collect and present results. An agent may not approve a request, may not be the identity a run executes under, and may not be on a hold or run list. Approving a controlled run is pinned at the always-approve RACI tier and is excluded from earned graduation (ADR-045, ADR-135), in the same way `code.commit` is pinned today. The broker's submission credentials are never exposed to an agent's context. Acceptance: a conformance test shows that an approval or a submission attempted by an agent principal is refused.

**R10. When no authorized person is available.**
A request that no authorized runner has answered stays queued. It never falls back to an agent, a service identity, or a person who is not on the run list. After a configurable interval the doorbell rings the next authorized runner for that code at the site, then the site export-control approver, through the existing notification channels. The requester sees who has been asked and when. A request that is not decided before its expiry is closed with a reason and can be refiled in one step. There is no automatic break-glass path. Acceptance: with no authorized runner available, a request waits, escalates, and expires with a reason, and no run occurs.

**R11. Generic over any controlled binary and any scheduler.**
Axiom names no controlled code and no site. A code enters through its registry entry and its compute adapter. A scheduler enters through a new scheduler interface on the HPC executor: submit, poll, cancel and collect. The first scheduler provider is a batch scheduler of the kind most academic clusters run; a second provider is required before the interface is called stable. A local run on an authorized host goes through the same request and approval path with a local executor. Acceptance: two different codes and two scheduler providers pass the same conformance suite with no change to the broker.

**R12. Authorization is checked at every step, not once.**
Run-list membership, the code's registry entry and the approval's validity are checked when the request is approved and again when the broker submits. A revoked agreement, a removed runner or a changed declaration between those two moments refuses the submission. A run already queued on the scheduler when authorization is revoked is reported to the runner and the site approver, who decide whether to cancel it (see Q6). Acceptance: revoking a runner between approval and submission refuses the submission with a reason.

## 7) Non-goals

- **Deciding what is controlled.** That is the site's export-control office's call. Axiom records the determination and its basis; it does not make one.
- **Controlling data, inputs or results under export control.** The rule says they are not controlled. This PRD adds no export-control label to them and no classification of their text (ADR-175).
- **Building the enclave.** Cluster accounts, filesystem permissions on the code's install, network isolation and the cluster's own controls belong to the site. Axiom submits into them and does not replace them.
- **Vetting people.** Whatever the site does to decide that a person may hold or run a code (training, citizenship or license checks) happens in the site's own process. Axiom records the outcome as an agreement.
- **License management** for codes whose license terms are separate from export control.
- **Tightly coupled decomposition.** Splitting a controlled run across federation peers is out of scope; compute decomposition's own boundaries apply (ADR-040 D11), and a controlled chunk is never sent to a peer that is not an authorized enclave.

## 8) Phasing

- **Phase 1, the first slice: request, approve, record.** No HPC submission. R1 (resolving the run list from agreements), R2, R3, R6 for the paths that apply, R8 for the steps that exist, R9 for filing and approval, and R12 at approval time. After approval, the authorized runner runs the job by hand as they do today, then records the job id and the output digests against the approval in one step. This already replaces the informal email or chat request with a record that binds what was asked to what was approved, and it proves the approval binding before any automation is trusted with submission.
- **Phase 2: broker and HPC executor.** R4, R5, R11 with the first scheduler provider, R12 at submission, and the rest of R6 and R8. The broker becomes the only Axiom path that submits a controlled code.
- **Phase 3: availability, bits and breadth.** R10 escalation, R7 install and copy governance, the second scheduler provider, and a request filed on one node and executed at the node that holds the code, where the request and results cross nodes as ordinary data and the approval and run stay at the code's site.

## 9) Open questions, with recommended defaults

| Q | Question | Recommended default |
|---|---|---|
| Q1 | Whose identity does the scheduler see: the runner's own cluster account, or a site service account that carries the runner's attribution? | The runner's own account where the cluster allows it, because that is the identity the site has already vetted. A service account only where the site requires it, always with the delegation token naming the runner as subject. Needs an ADR. |
| Q2 | How does the broker treat a code whose standing is `UNKNOWN` on an enclave target? | Refuse submission through the broker with the next step "ask the site export-control approver to declare it". Ordinary compute paths that do not go through the broker are unaffected. |
| Q3 | May an authorized person approve their own request? | Yes by default, because they are the person the rule requires. A site may require a second authorized person for all or some codes. |
| Q4 | How long does an approval stay valid? | Seven days, single use, configurable per site. Longer than the fifteen-minute plan default because cluster queues and runner availability are slow. |
| Q5 | Should recurring runs, such as nightly verification of a controlled code on a cluster runner, have standing approvals? | Yes, as a standing approval that an authorized runner signs for a named template with fixed code, version and resources, where only declared inputs vary, with a short expiry and renewal. Not in Phase 1. Needs an ADR. |
| Q6 | What happens to a queued or running job when the runner's authorization is revoked? | Notify the runner and the site approver and let them decide; do not cancel automatically, since a cancellation can lose work and is itself an act someone should own. |
| Q7 | Where does the hold list live relative to the run list? | One registry entry per code with two lists, both derived from access agreements, so a site can see who holds and who runs a code in one place. |
| Q8 | Are request inputs ever sensitive enough to withhold from the approval doorbell? | Rely on the existing approval bridge, which withholds parameters from a channel whose ceiling does not admit their label and points to a terminal. Inputs are not export controlled, so this applies only to access-control labels. |

## 10) Decisions that will need ADRs

These are hard to reverse and should each get an ADR before the phase that depends on them. Numbers are taken with `python scripts/lint_adr_numbers.py --next` when each is drafted; none is drafted here.

1. **The execution model:** a controlled run is an approved request bound to its digest and executed under an authorized person's identity, never an agent's, and the broker is the only Axiom path that submits a controlled code. Before Phase 1.
2. **The source of truth for hold and run lists:** derived from access agreements recorded as attestations, rather than a separate roster store. Before Phase 1.
3. **The scheduler-side identity (Q1)** and how delegation tokens map onto cluster accounts. Before Phase 2.
4. **The result-collection boundary:** only declared outputs leave, so bits cannot leave with results. Before Phase 2.
5. **Standing approvals for recurring runs (Q5).** Before Phase 3.

## 11) Risks

| Risk | Mitigation |
|---|---|
| A site reads Phase 1 as enforcement, when submission is still manual. | Phase 1 surfaces say plainly that the runner submits by hand, and the record shows which runs were submitted by hand. |
| Results carry bits out by accident, for example a run directory that contains the executable. | R5 collects declared outputs only, with a test that plants an executable. |
| Approvals become a rubber stamp because the reviewer cannot read the request. | The ADR-144 presenter shows the request at a readable density, including resources and inputs by name and hash, and the reviewer can ask a question before deciding. |
| The approval path becomes the bottleneck and people route around it. | R10 escalation, M6 tracked from the first pilot, and Q5 standing approvals for routine recurring runs. |
| The registry docstring and ADR-175 describe a broker that does not exist yet, so readers assume enforcement is in place. | This PRD names the gap. When Phase 2 lands, the owning spec describes the broker as built. |

## 12) Acceptance and rollout

- **Sign-off:** the Axiom platform owner, and for each site, that site's export-control approver before the site enables Phase 2.
- **Rollout:** per site and per code. A site enables controlled execution for one code first, runs Phase 1 for a period, then enables broker submission.
- **Rollback:** disabling the broker for a site returns that site to manual submission with Phase 1 records intact. Nothing in rollback grants anyone access to bits.
- **Spec:** a `spec-controlled-execution.md` is written with Phase 1 and kept current with each phase, per the three-document rule.
