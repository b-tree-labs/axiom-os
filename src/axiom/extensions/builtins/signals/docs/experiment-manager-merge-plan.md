# Experiment Manager PRD: Merge Scenario Implementation Plan

**Target Document**: `docs/prds/prd-experiment-manager.md`  
**Current Status**: v2.0.0 (published, in external review)  
**Merge Target**: v2.1.0 (incorporate Sense signals + reviewer feedback)  
**Timeline**: Week of February 25, 2026  

---

## Phase 1: Gather Sense Signals (2-3 days)

### 1.1 Meeting Notes
**Collect**: Recent meetings discussing Experiment Manager features/feedback

**Sources**:
- Meeting notes from last 2 weeks (Feb 11-25)
- Search for: "Experiment Manager", "experiment-manager-prd", related initiatives
- Extract: Action items, decisions, concerns

**How to Process**:
```bash
# Find all meeting notes mentioning Experiment Manager
find docs/meeting-notes -name "*.md" -exec grep -l "Experiment Manager" {} \;

# Use notes.py extractor
python -c "
from tools.pipelines.sense.extractors.notes import NotesExtractor
extractor = NotesExtractor()
extraction = extractor.extract('docs/meeting-notes/2026-02-25-team-sync.md')
"
```

**Expected Signals**: 2-4 action items (missing features, integration concerns, performance)

### 1.2 Voice Memos / Transcripts
**Collect**: Recorded feedback from stakeholders

**Sources**:
- Voice memos from PIs, engineers, users
- Transcribed Teams/Zoom recordings of Experiment Manager discussions
- Whisper transcripts (if available)

**How to Process**:
```bash
# Process voice memos
python tools/pipelines/sense/extractors/voice.py \
    --input ~/voice-memos/experiment-manager/ \
    --output signals/voice-memos-extraction.json
```

**Expected Signals**: 1-3 voice-derived signals (often blockers/concerns, confidence 0.80-0.85)

### 1.3 GitLab Discussion
**Collect**: Issues, MRs, discussions related to Experiment Manager

**Sources**:
- Open issues tagged with "experiment-manager"
- Recent MR discussions
- Issue #XXX related to scheduler/tracking/compliance

**How to Process**:
```bash
# Export GitLab data
python tools/gitlab_tracker_export.py \
    --project-id 18 \
    --labels "experiment-manager" \
    --output signals/gitlab-export.json

# Extract signals
python -c "
from tools.pipelines.sense.extractors.gitlab_diff import GitLabExtractor
extractor = GitLabExtractor()
extraction = extractor.extract('signals/gitlab-export.json')
"
```

**Expected Signals**: 3-5 decision/action items (feature requests, spec questions, confidence 0.88-0.93)

### 1.4 Teams Chat
**Collect**: Recent discussions in teams channels

**Sources**:
- #experiment-manager channel
- #example-consumer (team discussions)
- Direct messages with feedback

**How to Process**:
```bash
# Extract from Teams chat export
python -c "
from tools.pipelines.sense.extractors.teams_chat import TeamsChatExtractor
extractor = TeamsChatExtractor()
extraction = extractor.extract('signals/teams-export.json')
"
```

**Expected Signals**: 2-3 informal signals (early ideas, concerns, confidence 0.75-0.90)

### 1.5 Freetext Input
**Collect**: Any ad-hoc feedback not captured elsewhere

**Process**: Create structured input file:
```json
{
  "source": "freetext",
  "timestamp": "2026-02-25T10:00:00Z",
  "entries": [
    {
      "author": "Dr. Liu",
      "text": "We need better handling of experiment pause/resume states",
      "initiative": "experiment-manager-prd",
      "signal_type": "action_item"
    },
    {
      "author": "Jake Chen",
      "text": "Performance is a concern at 10k+ simultaneous experiments",
      "initiative": "experiment-manager-prd", 
      "signal_type": "blocker"
    }
  ]
}
```

**Expected Signals**: 1-2 additional signals

### Summary of Phase 1
- **Total Expected Signals**: 10-15 across all sources
- **Confidence Distribution**: 0.75-0.95 (voice lower, GitLab higher)
- **Signal Types**: Mix of action_items, decisions, blockers
- **Output**: `signals/experiment-manager-synthesis-2026-02-25.json`

---

## Phase 2: Synthesize Signals (1 day)

### 2.1 Run Sense Synthesizer
```python
from tools.pipelines.sense.models import Signal
from tools.pipelines.sense.synthesizer import Synthesizer
import json
from pathlib import Path

# Load all extracted signals
all_signals = []
for signal_file in Path("signals").glob("*.json"):
    with open(signal_file) as f:
        data = json.load(f)
        if "signals" in data:
            all_signals.extend([Signal.from_dict(s) for s in data["signals"]])

# Synthesize
synthesizer = Synthesizer()
changelog = synthesizer.synthesize(
    signals=all_signals,
    date="2026-02-25",
    include_all=True,
)

# Save for review
with open("signals/experiment-manager-changelog-2026-02-25.md", "w") as f:
    f.write(changelog.to_markdown())

print(f"✓ Synthesized {len(changelog.entries)} changelog entries")
for entry in changelog.entries:
    print(f"  - [{entry.signal_type}] {entry.detail}")
```

### 2.2 Output: Changelog
**File**: `signals/experiment-manager-changelog-2026-02-25.md`

**Sample Structure**:
```markdown
# Experiment Manager PRD - Signal Synthesis (Feb 25, 2026)

## Grouped by Initiative: experiment-manager-prd

### Action Items (4 entries)
- Missing pause/resume state handling (from team meeting)
- Better error handling for job failures (from GitLab issue #478)
- Compliance audit logging (from PI voice memo)
- Integration with scheduler improvements (from Teams discussion)

### Decisions (3 entries)
- Performance target: support 10k simultaneous (from PI voice)
- API versioning strategy needed (from GitLab discussion)
- Admin panel vs. CLI for configuration (team decision)

### Blockers (2 entries)
- Performance scaling concern at high concurrency (from voice)
- Unclear scheduling interaction with Bubble Flow (from Teams)

## Summary
- Total signals: 12
- Confidence average: 0.88
- People involved: 7
- Action items requiring changes: 4
```

### 2.3 Human Review Step
**Task**: Review changelog entries and map to PRD sections

**For Each Entry**:
- [ ] Which section of experiment-manager-prd.md does this affect?
- [ ] Is this a new requirement, clarification, or performance concern?
- [ ] Should it be incorporated as-is, or needs refining?
- [ ] Confidence level: keep as-is or adjust?

**Output**: Reviewed changelog with mapping to PRD sections

---

## Phase 3: Extract External Changes (1-2 days)

### 3.1 Download SharePoint Version
```bash
# Download current Experiment Manager PRD from SharePoint
# (Requires SharePoint provider setup)
python -c "
from tools.docflow.providers.sharepoint import SharePointProvider
provider = SharePointProvider(site_url='...', creds_path='...')
doc_bytes = provider.fetch_document(
    'experiment-manager-prd.docx'
)
with open('external/experiment-manager-prd-sp.docx', 'wb') as f:
    f.write(doc_bytes)
"
```

### 3.2 Extract Changes & Comments
```python
from tools.pipelines.sense.extractors.docflow_review import DocFlowReviewExtractor
from pathlib import Path

extractor = DocFlowReviewExtractor()
extraction = extractor.extract(Path("external/experiment-manager-prd-sp.docx"))

# Print all changes
for change in extraction.changes:
    print(f"{change.change_type.value:12} | {change.author:15} | {change.section}")
    print(f"  {change.comment_text or change.new_text}")
    print()
```

### 3.3 Expected Changes
Based on document age (2 weeks in review), expect:
- **Comments**: 3-5 from different reviewers
- **Tracked Changes**: 2-3 specific text modifications
- **Content Drift**: 1-2 sections with informal notes

**Sample**:
```
Comment      | Dr. Rachel Park | Requirements: Performance
  "How do you handle 100+ concurrent experiment state updates?"

Tracked      | Prof. Tim Lee   | Architecture: Job Submission
  OLD: "Experiments submitted via REST API"
  NEW: "Experiments submitted via REST API v2 with priority queue support"

Drift        | [Internal]      | Integration Points
  Added informal note about scheduler interaction needs
```

### 3.4 Output: `DivergenceReport`
```json
{
  "prd_id": "experiment-manager-prd",
  "external_changes": 6,
  "md_changed_since_sync": false,
  "external_changed_since_sync": true,
  "requires_merge": true,
  "merge_strategy": "external_wins"  // Only external changed
}
```

---

## Phase 4: Identify Convergences (1 day)

### 4.1 Cross-Source Analysis
Map where Sense signals and external changes overlap:

**Example 1: Performance Scaling**
- **Sense**: PI voice memo says "performance concern at 10k experiments"
- **External**: Dr. Park comment asks "how do you handle 100+ concurrent updates?"
- **Convergence**: Performance is high-priority concern
- **Signal Strength**: ⭐⭐⭐ (two independent sources)

**Example 2: Scheduler Integration**
- **Sense**: GitLab issue mentions scheduler interaction
- **External**: Internal drift note says same thing
- **Convergence**: Scheduler integration needs clarification
- **Signal Strength**: ⭐⭐ (both identify gap)

**Example 3: State Management**
- **Sense**: Team meeting action item on pause/resume states
- **External**: None mentioned
- **Status**: Sense-only concern
- **Signal Strength**: ⭐ (single source, still valid)

### 4.2 Create Convergence Map
```markdown
# Convergences: Experiment Manager PRD

| Issue | Sense Signal | External Change | Confidence | Priority |
|-------|--------------|-----------------|------------|----------|
| Performance Scaling | PI voice (0.85) | Park comment | 0.90 | HIGH |
| Scheduler Integration | GitLab issue (0.92) | Internal note | 0.85 | MEDIUM |
| State Management | Team meeting (0.95) | - | 0.95 | HIGH |
| API Versioning | GitLab discussion (0.88) | Lee tracked change | 0.92 | MEDIUM |
```

---

## Phase 5: Semantic Merge (1-2 days)

### 5.1 Merge Workflow
```bash
# Run the merge test scenario adapted for Experiment Manager
python -m pytest \
    tests/integration/test_docflow_merge_scenario.py \
    -k "TestCompleteSenseToMergePipeline" \
    -v
```

### 5.2 Manual Merge Review
For each section of experiment-manager-prd.md:

**Section 1: Executive Summary**
- ✓ No changes needed (still accurate)

**Section 2: Requirements**
- [ ] Add state management requirement (from Sense: sig_meeting_001)
- [ ] Clarify scheduler interaction (from Sense + External convergence)
- [ ] Add performance requirement: support 100+ concurrent (from Sense + External)

**Section 3: Design Approach**
- [ ] Update diagram to show scheduler integration
- [ ] Add section on performance considerations

**Section 4: Integration Points**
- [ ] Expand scheduler integration details
- [ ] Add API versioning notes (from Tracked change)

**Section 5: Success Metrics**
- [ ] Update to include concurrent experiment count (Performance)
- [ ] Add reliability metric (error handling)

**Section 6: Open Questions**
- [ ] Move scheduler integration from open to resolved
- [ ] Add: "How should we handle priority queueing?"

### 5.3 Document Merge Decisions
```json
{
  "merge_decisions": [
    {
      "section": "R2: Requirements - State Management",
      "resolution": "add_requirement",
      "sources": ["sig_meeting_001"],
      "new_content": "R2.5: Experiment State Transitions\nThe system shall support pause, resume, and cancel state transitions..."
    },
    {
      "section": "Integration Points",
      "resolution": "combine",
      "sources": ["sig_gitlab_002", "external_drift"],
      "rationale": "Both Sense (GitLab) and external review identify scheduler integration gap"
    },
    {
      "section": "Success Metrics",
      "resolution": "combine",
      "sources": ["sig_voice_003", "external_comment"],
      "new_content": "Shall support 100+ concurrent experiment state updates with < 500ms latency"
    }
  ]
}
```

### 5.4 Create Merged Document
```bash
# Create merged markdown with all decisions incorporated
python -c "
import json
from pathlib import Path

# Load baseline
baseline_md = Path('docs/prds/prd-experiment-manager.md').read_text()

# Load merge decisions
with open('signals/merge-decisions.json') as f:
    decisions = json.load(f)

# Apply each decision to baseline
merged_md = baseline_md
for decision in decisions['merge_decisions']:
    # Apply change based on decision.resolution
    # (In real system: this would be more sophisticated)
    pass

# Save merged version
Path('docs/prds/prd_experiment-manager-v2.1.0.md').write_text(merged_md)
print('✓ Merged document created')
"
```

---

## Phase 6: Version Bump & Publish (1 day)

### 6.1 Count Commits
```bash
cd /path/to/consumer-repo

# Count commits since last version
git rev-list v2.0.0..HEAD --count
# Expected: 5-8 commits (depending on recent work)
# Rule: 2-5 → minor bump (v2.0.0 → v2.1.0) ✓
```

### 6.2 Update Cover Page
```markdown
**Experiment Manager Module PRD**

**[published] v2.1.0** | February 25, 2026

**Module:** Experiment Manager (scheduling, tracking, lifecycle)  
**Related Modules:** Job Scheduler (upstream), Dashboard (display), Agent Manager  
**Parent Document:** Neutron OS Executive PRD
```

### 6.3 Publish
```bash
# Publish with all the merge decisions incorporated
python tools/docflow/scripts/publish.py experiment-manager-prd --no-upload

# Output should show:
# ✓ Version: v2.1.0
# ✓ Commit: <new_merge_commit>
# ✓ Generated: .neut/generated/experiment-manager-prd.docx
```

### 6.4 Upload to SharePoint
```bash
# Upload the merged version back to SharePoint
python tools/docflow/scripts/publish.py experiment-manager-prd --upload
```

---

## Phase 7: Audit & Documentation (1 day)

### 7.1 Generate Audit Trail
```json
{
  "merge_date": "2026-02-25",
  "target_prd": "experiment-manager-prd",
  "source_version": "v2.0.0",
  "target_version": "v2.1.0",
  "signals_incorporated": 12,
  "external_changes_incorporated": 6,
  "convergences_identified": 2,
  "decisions_made": 8,
  "sections_modified": 5,
  "commit_sha": "abc123def456",
  "published_at": "2026-02-25T18:00:00Z"
}
```

### 7.2 Stakeholder Notification
- [ ] Notify reviewers: "Your feedback incorporated in v2.1.0"
- [ ] Notify Sense signal originators: "Your signal was synthesized"
- [ ] Update project tracking: Mark review as "merged"
- [ ] Document lessons learned

---

## Timeline Summary

| Phase | Duration | Deliverable |
|-------|----------|-------------|
| 1. Gather Signals | 2-3 days | signals/experiment-manager-synthesis-*.json |
| 2. Synthesize | 1 day | signals/experiment-manager-changelog-*.md |
| 3. Extract External | 1-2 days | external/experiment-manager-prd-sp.docx + changes |
| 4. Identify Convergences | 1 day | convergence-map.md |
| 5. Semantic Merge | 1-2 days | merged markdown + decisions |
| 6. Publish | 1 day | v2.1.0 published to SharePoint |
| 7. Audit & Docs | 1 day | audit trail + stakeholder notification |
| **Total** | **~1 week** | **v2.1.0 published** |

---

## Success Criteria

✅ **Merge Complete When**:
- [ ] All 12-15 Sense signals processed and mapped
- [ ] All 5-6 external changes extracted
- [ ] 2+ convergences identified and prioritized
- [ ] 8+ merge decisions documented with rationale
- [ ] Merged markdown passes syntax validation
- [ ] Version bumped to v2.1.0
- [ ] Document published to SharePoint
- [ ] Audit trail generated and archived
- [ ] Stakeholders notified

---

## Troubleshooting

### Issue: "No Sense signals found"
**Solution**: Check signal sources
- Did meeting notes get captured? (Check notes/ directory)
- Are GitLab issues tagged correctly? (Check labels)
- Did you export Teams chat? (Check teams/ directory)

### Issue: "Merge conflicts detected"
**Solution**: Manual resolution required
- Review conflicting section in both versions
- Determine if it's a genuine conflict or misdetection
- Create manual decision in merge-decisions.json
- Document rationale

### Issue: "Version not bumping"
**Solution**: Check commit count
```bash
git rev-list v2.0.0..HEAD --count  # Should be ≥ 2
```
If < 2, either create dummy commit or manually set version.

---

## References

- **Test Infrastructure**: `tests/integration/test_docflow_merge_scenario.py`
- **Merge Guide**: `docs/spec-merge-scenario-guide.md`
- **Signal Models**: `tools/pipelines/sense/models.py`
- **Synthesizer**: `tools/pipelines/sense/synthesizer.py`
- **Publish Script**: `tools/docflow/scripts/publish.py`

---

**Created**: February 25, 2026  
**Status**: Ready for Implementation  
**Next Step**: Execute Phase 1 (Gather Sense Signals)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
