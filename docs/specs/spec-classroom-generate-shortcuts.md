# Classroom Generate Shortcuts — Extension-Driven Quick Actions in Open WebUI

**Status:** Draft
**Date:** 2026-04-16
**Owner:** Ben Booth
**Related:** prd-classroom.md §5.7 (Open WebUI integration), `docs/working/classroom-user-journeys.md`, `feedback_open_webui_generate_shortcuts` memory, `project_extension_aware_rag_and_bonsai` memory.

---

## 1. Concept

Students and instructors should never stare at a blank prompt
wondering "what can I do?" Axiom extensions publish **generate
shortcuts** — contextual quick-action buttons that appear in
Open WebUI at three touchpoints and inject preformed prompts
that guide structured outcomes.

Design inspiration: Genspark AI's contextual action tiles below
the prompt bar. Axiom's version is extension-driven (not
hard-coded), role-gated, and changes based on course, phase,
and conversation state.

**Three touchpoints, three Open WebUI extension points:**

```
┌─────────────────────────────────────────────┐
│  TOUCHPOINT 1: Starter Tiles                │
│  (visible on empty/new chat)                │
│  Open WebUI: model-level prompt suggestions │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐    │
│  │/research  │ │/quiz-prep│ │ Review   │    │
│  │ a topic   │ │ for exam │ │ lecture  │    │
│  └──────────┘ └──────────┘ └──────────┘    │
├─────────────────────────────────────────────┤
│  [prompt input field]                       │
├─────────────────────────────────────────────┤
│  TOUCHPOINT 2: Message Actions              │
│  (buttons on each assistant message)        │
│  Open WebUI: Action Functions               │
│  [✓ Check citations] [↕ Explain simpler]    │
│  [📤 Submit work]    [🚩 Flag instructor]   │
├─────────────────────────────────────────────┤
│  TOUCHPOINT 3: Follow-up Suggestions        │
│  (clickable next-step prompts after reply)  │
│  Open WebUI: task-model-generated from      │
│  structured "Next steps" in our response    │
│  "Dig deeper into [sub-topic]"              │
│  "Compare this with [related concept]"      │
│  "Run a research loop on the contradiction" │
└─────────────────────────────────────────────┘
```

---

## 2. Touchpoint 1: Starter Tiles (Pre-Conversation)

**Mechanism:** Open WebUI's per-model prompt suggestions,
configured via admin API at classroom provisioning time.

**When visible:** student or instructor opens a new chat (no
messages yet). Tiles replace the empty-state placeholder.

**How configured:** `axi classroom create` writes starter tiles
to Open WebUI via its admin REST API:

```python
# During WF-1 enrollment, for each student's model config:
PUT /api/models/{model_id}
{
  "meta": {
    "suggestion_prompts": [
      {
        "title": ["🔬", "Research a topic"],
        "content": "/research "
      },
      {
        "title": ["📝", "Prep for quiz"],
        "content": "/quiz-prep "
      },
      {
        "title": ["📖", "Review today's lecture"],
        "content": "Summarize the key concepts from today's lecture on $CURRENT_MODULE and identify what I should study further."
      },
      {
        "title": ["🔍", "Verify a claim"],
        "content": "/cite-check "
      }
    ]
  }
}
```

**Context-awareness:** different tiles per role and phase:

| Context | Student tiles | Instructor tiles |
|---------|--------------|------------------|
| **Onboarding** (WF-2) | "Complete my checklist", "Take baseline quiz", "Start my first research loop" | "Check cohort readiness", "Review a student's interview" |
| **Active learning** | "/research", "/quiz-prep", "Review lecture", "/cite-check" | "/cohort-status", "/check-in [student]", "Review SCAN alerts" |
| **Assessment week** | "Practice quiz on [objective]", "/submit [assignment]" | "/grade [assignment]", "View score distributions" |
| **Presentations** | "Rehearse my presentation", "Get peer feedback summary" | "Evaluate presentations", "Promote best findings" |
| **End of course** | "Reflect on my learning", "Download my harvest" | "Run course review", "Archive classroom" |

**Extension-driven:** domain extensions add their own tiles.
A domain consumer might add: "Search domain databases",
"Check regulatory compliance for [topic]". These appear
alongside the generic tiles when the consumer extension is
configured.

**Implementation:** a `ShortcutProvider` interface in the
classroom extension:

```python
class ShortcutProvider:
    """Extensions implement this to contribute starter tiles."""
    
    def get_starter_tiles(
        self,
        role: Literal["student", "instructor"],
        phase: str,  # "onboarding", "active", "assessment", "presenting", "completing"
        course_id: str,
        user_id: str,
    ) -> list[StarterTile]:
        ...

@dataclass
class StarterTile:
    icon: str
    title: str
    prompt_template: str  # may contain $PLACEHOLDERS
    priority: int = 0     # lower = leftmost
    visible_if: str = ""  # optional condition expression
```

Extensions register `ShortcutProvider` implementations in their
`axiom-extension.toml`. The classroom setup aggregates all
providers and writes the merged tile set to Open WebUI.

Tile refresh: when course phase changes (onboarding → active),
`axi classroom` updates the Open WebUI model config. Students
see new tiles on their next new-chat without page reload.

---

## 3. Touchpoint 2: Message Actions (In-Conversation)

**Mechanism:** Open WebUI Action Functions — Python classes
that register clickable buttons on each assistant message.

**When visible:** after every assistant response. Buttons appear
in the message toolbar.

**Core actions (always present):**

| Button | Icon | What it does | Agent | Priority |
|--------|------|-------------|-------|----------|
| Check citations | ✓ | Re-runs retrieval for every claim; annotates with source provenance; flags ungrounded claims | CURIO (Eval) | 10 |
| Explain differently | ↕ | Re-explains the response at a different audience level (user picks: simpler / more technical / ELI5) | AXI (Chat) | 20 |
| Continue research | 🔬 | Takes the response's key finding and launches a CURIO research loop iteration | CURIO (Eval) | 30 |
| Submit as work | 📤 | Packages the current conversation + findings into a graded submission for the active assignment | AXI (Loop) → WF-10 | 40 |
| Flag for instructor | 🚩 | Creates a help ticket with the current conversation as context; instructor sees it in dashboard | AXI (Loop) → WF-6 | 50 |

**Instructor-only actions** (visible only to instructor role):

| Button | Icon | What it does | Agent | Priority |
|--------|------|-------------|-------|----------|
| Promote finding | ⬆ | Proposes the finding in this message for course RAG promotion | CURIO → trust profile gate | 10 |
| Score this response | 📊 | Opens rubric-scoring interface for this student's response | AXI (Loop) → WF-4 | 20 |
| Send to student | 💬 | Sends a feedback message to the student whose session this is | AXI (I↔S interaction) | 30 |

**Extension-driven actions:** domain extensions register
additional Action Functions. A domain consumer might add a
domain-specific validation action (e.g. a scientific-data check).
These appear alongside core actions when the extension is loaded.

**Implementation shape:**

```python
class AxiomCiteCheckAction:
    """Check citations in the last assistant message."""
    
    class Valves(BaseModel):
        priority: int = 10
        axiom_backend_url: str = "http://localhost:8080"
    
    async def action(
        self,
        body: dict,
        __user__=None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> dict:
        # Extract the assistant message text
        messages = body.get("messages", [])
        last_assistant = next(
            (m for m in reversed(messages) if m["role"] == "assistant"),
            None,
        )
        if not last_assistant:
            return {"content": "No message to check."}
        
        # Call Axiom backend's cite-check endpoint
        await __event_emitter__({"type": "status", "data": {"description": "Checking citations..."}})
        result = await self._check_citations(last_assistant["content"])
        
        return {
            "content": result.annotated_text,
            "files": result.provenance_report if result.has_report else [],
        }
```

Action Functions are deployed to Open WebUI as part of
classroom provisioning (WF-1). They call back to the Axiom
backend for the real work — the Action Function is a thin UI
adapter.

---

## 4. Touchpoint 3: Follow-Up Suggestions (Post-Response)

**Mechanism:** Open WebUI auto-generates follow-up suggestions
via its task model. We influence the suggestions by including a
structured **"Next steps"** section in our Axiom backend
responses.

**How it works:** our Axiom Pipe Function (the OpenAI-compatible
API endpoint) appends to every response:

```markdown
---
**Next steps you might try:**
- Dig deeper into [specific sub-topic from the response]
- Compare [concept A] with [concept B] mentioned above
- Run `/research` on the contradiction between [source X] and [source Y]
- Practice with `/quiz-prep [relevant objective]`
```

Open WebUI's task model reads this section and generates
clickable follow-up buttons. The suggestions are contextual
because our backend wrote them from the actual response content.

**Context-awareness:** the Axiom backend tailors "Next steps"
based on:
- What the student just asked about (topic)
- What the response covered (concepts, citations)
- What the student hasn't done yet (uncompleted onboarding
  items, unsubmitted assignments)
- The course's current phase (active learning vs assessment
  week)
- What other students in the cohort are researching
  (cross-pollination suggestions)

**The follow-up section is model-mediated** (generated by our
LLM pipeline). The actual prompts it suggests may trigger
deterministic operations (like `/submit`). The generation of
the suggestion itself is advisory; the action it triggers
goes through the normal authorization stack.

---

## 5. Extension Registration Protocol

Every Axiom extension that wants to contribute shortcuts
registers them in its `axiom-extension.toml`:

```toml
[[shortcuts.starter_tiles]]
icon = "🔬"
title = "Research a topic"
prompt_template = "/research "
roles = ["student", "instructor"]
phases = ["active", "assessment"]
priority = 10

[[shortcuts.starter_tiles]]
icon = "📝"
title = "Prep for quiz"
prompt_template = "/quiz-prep "
roles = ["student"]
phases = ["active", "assessment"]
priority = 20

[[shortcuts.actions]]
name = "cite-check"
module = "axiom.extensions.builtins.classroom.actions.cite_check"
icon = "✓"
label = "Check citations"
roles = ["student", "instructor"]
priority = 10

[[shortcuts.actions]]
name = "promote-finding"
module = "axiom.extensions.builtins.classroom.actions.promote"
icon = "⬆"
label = "Promote finding"
roles = ["instructor"]
priority = 10
```

The classroom setup reads all registered extensions' shortcut
manifests and deploys the merged set to Open WebUI.

---

## 6. Analytics

Every shortcut interaction is a traceable event:

| Event | Data captured | Destination |
|-------|--------------|-------------|
| Starter tile clicked | tile_id, user_role, course_phase, timestamp | LangFuse |
| Action button clicked | action_id, message_id, user_role, timestamp | LangFuse |
| Follow-up suggestion clicked | suggestion_text, parent_message_id, timestamp | LangFuse |

This gives us: which shortcuts are used most? By whom? In what
phase? Do students who use `/research` more produce better
outcomes? Do instructors who use "Promote finding" create
richer course corpora over time?

Analytics feed the platform self-improvement cascade
(`project_platform_self_improvement_cascade`): patterns in
shortcut usage across cohorts inform default tile ordering and
which shortcuts ship with the next version.

---

## 7. Test Coverage

Per the three-axis interaction testing requirement:

| Shortcut | I↔S test | S↔S test | Standalone test |
|----------|----------|----------|-----------------|
| `/research` | Instructor sees student's loop state | Two students' loops cross-pollinate findings | Loop produces valid iteration with citations |
| `/cite-check` | — | — | Grounded claims pass; ungrounded flagged |
| `/submit` | Submission appears in instructor's grading queue; Canvas grade pushed | — | Submission packaged correctly |
| `/flag-instructor` | Instructor receives help ticket with conversation context | — | Ticket created with correct metadata |
| "Promote finding" | Instructor promotes → finding enters course RAG → students can retrieve | Student retrieves promoted finding | Promotion follows trust-profile gate |
| "Score this response" | Score reaches Canvas grade book | — | Rubric applied correctly |

---

## 8. Relation to Bonsai-Assisted Everything

Bonsai LM powers the **contextual intelligence** behind
shortcuts:

- Starter tiles are STATIC (configured at provisioning). Bonsai
  is not involved in showing them.
- Action Functions call Axiom's backend which MAY use Bonsai for
  the response (e.g. "Explain simpler" re-generates via LLM).
  The action TRIGGER is deterministic (button click); the
  RESPONSE is model-mediated.
- Follow-up suggestions are model-mediated end-to-end (our
  backend generates "Next steps" text; task model converts to
  buttons).

Bonsai's role is enrichment, not gating. If Bonsai/LLM is
down, starter tiles and action buttons still appear; actions
that need LLM (like re-explain) degrade to "service temporarily
unavailable" rather than silently failing.

---

## Related Documents

- `prd-classroom.md §5.7` — Open WebUI integration architecture
- `docs/working/classroom-user-journeys.md` — user flows per
  persona + interaction matrix
- [Open WebUI Action Functions](https://docs.openwebui.com/features/extensibility/plugin/functions/action/) — technical reference for Touchpoint 2
- [Open WebUI Follow-Up Prompts](https://docs.openwebui.com/features/chat-conversations/chat-features/follow-up-prompts/) — technical reference for Touchpoint 3
- `spec-security.md §2` — deterministic vs model-mediated
  placement for each touchpoint
- `project_extension_aware_rag_and_bonsai` — extension manifest
  protocol (shortcuts are a manifest entry type)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
