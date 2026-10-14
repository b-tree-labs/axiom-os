# Stakeholder Interview Guide

> **Last Updated:** December 2025  
> **Purpose:** Guide conversations to validate assumptions and discover user needs

---

## Key Stakeholders to Interview

| Who | Role | Why Talk to Them | Priority |
|-----|------|------------------|----------|
| a nuclear engineering researcher | PartnerLab Operations | Validate ZOC logging details, upload process, operator pain points | **P1** |
| a nuclear engineering researcher | Project Lead / PrivateCloud | VPN requirements, VM deployment, strategic direction | **P1** |
| System Operators | Daily users | What data views save time? What's missing? | **P1** |
| **the isotope coordinator** | **PartnerLab Director, Isotope Coordinator** | **Current isotope workflow, hospital relationships, pain points** | **P0** |
| **Houston Hospital Contact(s)** | **Isotope Customers** | **Ordering pain, willingness to pay, volume, requirements** | **P0** |
| M E 390G Instructor | Nuclear Engineering Lab course (uses facility directly) | How could DT enhance lab curriculum? | **P2** |
| M E 361E / 336P Instructors | System Operations & Concepts courses | Simulator integration for system physics education | **P2** |
| Graduate Students | Current users | What's frustrating? What features are unused? | **P2** |
| physics-code Development Team | Code V&V | What validation data format would be most useful? | **P2** |
| PrivateCloud Support | Infrastructure | Constraints, costs, real-time streaming options | **P3** |
| Other facility Facilities | Potential pilots, isotope expansion | Interest level, similar challenges, isotope capability | **P3** |

> **Note:** Isotope production stakeholders (P0) added based on discovery that Houston cancer clinics currently call the isotope coordinator directly for isotope orders. This represents the highest-impact commercialization opportunity.

---

## Interview Framework

### Opening (5 min)
- Thank them for their time
- Explain purpose: "We're trying to make the digital twin more useful"
- Clarify: "No wrong answers - honest feedback helps most"
- Ask permission to take notes

### Context Questions (5 min)
- "Tell me about your role and how you interact with system data"
- "How long have you been in this role?"
- "What tools do you currently use for [their task]?"

### Problem Exploration (15 min)
- "Walk me through the last time you needed historical system data"
- "What was painful about that experience?"
- "How often does this happen?"
- "What would you do differently if you could?"

### Current State (10 min)
- "Have you used the digital twin web portal?"
- "What brought you there? What were you trying to do?"
- "What worked well? What was frustrating?"
- "What features have you never used? Why?"

### Ideal State (10 min)
- "If you could wave a magic wand, what would this tool do?"
- "What questions do you wish you could ask the data but can't today?"
- "What would make you recommend this to a colleague?"

### Closing (5 min)
- "Is there anything I should have asked but didn't?"
- "Who else should I talk to?"
- "Can I follow up if I have more questions?"

---

## Role-Specific Questions

### For System Operators
- "How do you currently log shift events?"
- "What happens when you need to look up something from last month?"
- "How do you prepare for regulatory body inspections?"
- "What data do you wish you had easy access to?"

### For Researchers
- "What data do you need for your current research?"
- "How do you currently get validation data for simulations?"
- "What would make a dataset citable/trustworthy for publication?"
- "What's missing from current physics-code outputs?"

### For Students
- "How are you learning system physics concepts?"
- "What's confusing about system behavior?"
- "Have you used the simulator? What did you think?"
- "What would help you feel more confident?"

### For Facility Management
- "How much time do you spend on compliance documentation?"
- "What's your biggest worry about the next inspection?"
- "What would an ideal compliance report look like?"
- "How do you currently detect off-normal conditions?"

### For Code Developers
- "What validation data do you currently use for physics-code?"
- "What's missing from available benchmark data?"
- "What format would be most useful for V&V?"
- "How do you handle uncertainty in comparisons?"

### For the isotope coordinator (Isotope Coordination) - **P0 PRIORITY**
- "Walk me through what happens when a hospital calls for isotopes"
- "How often do these requests come in? Weekly? Monthly?"
- "Who typically handles the simulation work? How is it assigned?"
- "How long does it typically take from call to delivery?"
- "What isotopes does PartnerLab produce? Which are most requested?"
- "Are there requests you turn down due to time/coordination burden?"
- "What's the current relationship with Houston hospitals? Contracts or ad-hoc?"
- "If a system could automate the ops package generation, would that help?"
- "What would make you confident enough to delegate isotope coordination to a system?"

### For Hospital Isotope Customers - **P0 PRIORITY**
- "How do you currently order isotopes? From which suppliers?"
- "Walk me through the last time you ordered from UT/PartnerLab"
- "What's frustrating about the current process?"
- "How predictable do you need isotope availability to be?"
- "What's the cost of not having isotopes when you need them? (delayed procedures)"
- "What would you pay for guaranteed 24-hour turnaround?"
- "Would you use a web portal to place and track orders instead of phone calls?"
- "What documentation do you need for compliance on your end?"

---

## Technical Verification Questions

These questions help verify our understanding of the system:

### For a nuclear engineering researcher (PartnerLab)
- [ ] Confirm ZOC Terminal is the logging software name
- [ ] How does data get from ZOC to Box? Manual or automated?
- [ ] What's the typical delay from system event to Box upload?
- [ ] Are there data types not currently captured that should be?

### For a nuclear engineering researcher (PrivateCloud)
- [ ] How is the Flask app deployed? (dev server, gunicorn, nginx?)
- [ ] What triggers the VPN requirement? PrivateCloud policy or choice?
- [ ] What's the monthly infrastructure cost (compute + storage)?
- [ ] Are there constraints on real-time data streaming?

---

## Interview Notes Template

```markdown
## Interview: [Name]
**Date:** 
**Role:** 
**Duration:** 

### Key Quotes
- 

### Pain Points Mentioned
1. 
2. 
3. 

### Feature Requests
1. 
2. 

### Surprises / Unexpected Insights
- 

### Follow-up Items
- [ ] 

### Validation of Assumptions
| Assumption | Confirmed? | Notes |
|------------|------------|-------|
| | | |
```

---

## Synthesis Framework

After completing interviews, synthesize findings:

### Pattern Recognition
- What pain points came up multiple times?
- What features were requested by multiple users?
- What assumptions were invalidated?

### Priority Matrix
| Finding | Frequency | Severity | Actionability |
|---------|-----------|----------|---------------|
| | | | |

### Persona Updates
- Did we miss any user types?
- Should priorities change based on findings?
- Are our "why" chains accurate?

### Roadmap Implications
- What should we build sooner?
- What should we deprioritize?
- What new initiatives emerged?

---

## Interview Schedule Tracker

| Stakeholder | Requested | Scheduled | Completed | Notes Link |
|-------------|-----------|-----------|-----------|------------|
| **the isotope coordinator (isotopes)** | [ ] | | [ ] | **P0** |
| **Hospital Contact 1** | [ ] | | [ ] | **P0** |
| **Hospital Contact 2** | [ ] | | [ ] | **P0** |
| a nuclear engineering researcher | [ ] | | [ ] | |
| a nuclear engineering researcher | [ ] | | [ ] | |
| Operator 1 | [ ] | | [ ] | |
| Operator 2 | [ ] | | [ ] | |
| NE Instructor | [ ] | | [ ] | |
| Grad Student 1 | [ ] | | [ ] | |
| Grad Student 2 | [ ] | | [ ] | |
| physics-code Developer | [ ] | | [ ] | |

---

## Quick Reference: The 5 Key Questions

If you only have 5 minutes, ask these:

1. **"Walk me through the last time you needed historical system data - what was painful?"**

2. **"What questions do you wish you could ask the data but can't today?"**

3. **"How often do you access the web portal? What brings you there?"**

4. **"If this tool didn't exist, what would you do instead?"**

5. **"What would make you recommend this to a colleague?"**
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
