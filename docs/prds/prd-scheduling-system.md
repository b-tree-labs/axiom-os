# Product Requirements Document: Scheduling System

> **Implementation Status: 🔲 Not Started** — This PRD describes planned functionality. Implementation has not started.

**Module:** Cross-Cutting Scheduling Infrastructure
**Status:** Draft
**Last Updated:** January 22, 2026
**Stakeholder Input:** three nuclear engineering researchers  
**Related Modules:** [Experiment Manager](prd-experiment-manager.md), [System Ops Log](prd-system-ops-log.md), [Medical Isotope](prd-medical-isotope.md)
**Parent:** [Executive PRD](prd-executive.md)

---

## Executive Summary

The Scheduling System is a **cross-cutting concern** that provides unified time management across all Axiom modules. It handles system time allocation, facility bookings, staff assignments, maintenance windows, and regulatory inspection scheduling. Rather than each module implementing its own scheduling logic, this centralized system ensures consistency, prevents conflicts, and provides holistic visibility.

**Key Principle:** Scheduling is about **time slot allocation and conflict resolution**, not about the specifics of what happens in those slots. The Experiment Manager, System Ops Log, and other modules consume scheduling services but don't own them.

---

## System Architecture

```mermaid
flowchart TB
    subgraph Consumers["Schedule Consumers"]
        Exp[Experiment Manager]
        Ops[System Ops Log]
        Med[Medical Isotope]
        Train[Training Module]
    end
    
    subgraph Core["Scheduling Core"]
        Engine[Scheduling Engine]
        Cal[Calendar Service]
        Notify[Notification Service]
    end
    
    subgraph Storage["Data Layer"]
        Slots[(Time Slots)]
        Rules[(Scheduling Rules)]
        History[(Schedule History)]
    end
    
    subgraph Views["Schedule Views"]
        Console[Console View]
        Display[Facility Display]
        Week[Week View]
        Month[Month View]
        Gantt[Gantt Chart]
    end
    
    Exp --> Engine
    Ops --> Engine
    Med --> Engine
    Train --> Engine
    
    Engine --> Cal
    Engine --> Notify
    Engine --> Slots
    Engine --> Rules
    
    Cal --> Slots
    Notify --> History
    
    Slots --> Console
    Slots --> Display
    Slots --> Week
    Slots --> Month
    Slots --> Gantt
    
    style Consumers fill:#e3f2fd,color:#000000
    style Core fill:#e8f5e9,color:#000000
    style Storage fill:#fff3e0,color:#000000
    style Views fill:#f3e5f5,color:#000000
    linkStyle default stroke:#777777,stroke-width:3px
```

---

## User Journey: Multi-Module Scheduling

```mermaid
flowchart TD
    subgraph Request["Request Phase"]
        R1[Researcher Request]
        R2[Maintenance Request]
        R3[Training Request]
        R4[Medical Request]
    end
    
    subgraph Process["Processing"]
        P1[Check conflicts]
        P2[Apply priority rules]
        P3[Route for approval]
        P4[Notify stakeholders]
    end
    
    subgraph Approve["Approval"]
        A1[Manager review]
        A2[ROC approval]
        A3[Confirm slot]
        A4[Update calendars]
    end
    
    subgraph Execute["Execution"]
        E1[Display on monitors]
        E2[Console reminders]
        E3[Track actuals]
        E4[Log variances]
    end
    
    R1 --> P1
    R2 --> P1
    R3 --> P1
    R4 --> P1
    
    P1 --> P2 --> P3 --> P4
    P4 --> A1
    
    A1 --> A2 --> A3 --> A4
    
    A4 --> E1
    A4 --> E2
    E1 --> E3
    E2 --> E3
    E3 --> E4
    
    style Request fill:#1565c0,color:#fff
    style Process fill:#2e7d32,color:#fff
    style Approve fill:#e65100,color:#fff
    style Execute fill:#7b1fa2,color:#fff
    style R1 fill:#e3f2fd,color:#333
    style R2 fill:#e3f2fd,color:#333
    style R3 fill:#e3f2fd,color:#333
    style R4 fill:#e3f2fd,color:#333
    style P1 fill:#c8e6c9,color:#333
    style P2 fill:#c8e6c9,color:#333
    style P3 fill:#c8e6c9,color:#333
    style P4 fill:#c8e6c9,color:#333
    style A1 fill:#fff3e0,color:#333
    style A2 fill:#fff3e0,color:#333
    style A3 fill:#fff3e0,color:#333
    style A4 fill:#fff3e0,color:#333
    style E1 fill:#f3e5f5,color:#333
    style E2 fill:#f3e5f5,color:#333
    style E3 fill:#f3e5f5,color:#333
    style E4 fill:#f3e5f5,color:#333
    linkStyle default stroke:#777777,stroke-width:3px
```

---

## Resource Types & Constraints

### Schedulable Resources

| Resource Type | Examples | Constraints | Priority |
|--------------|----------|-------------|----------|
| **System Time** | Core operations, power levels | Safety limits, maintenance windows | Critical |
| **Facilities** | Beam ports, pneumatic rabbit, thermal column | One experiment at a time | High |
| **Staff** | SRO on console, HP coverage | Certification requirements, shift limits | Critical |
| **Equipment** | Detectors, hot cells, glove boxes | Calibration status, availability | Medium |
| **Spaces** | Labs, conference rooms, training areas | Capacity limits, safety requirements | Low |

### Scheduling Rules Engine

```mermaid
flowchart TD
    subgraph Rules["Scheduling Rules"]
        R1[Safety Rules]
        R2[Priority Rules]
        R3[Business Rules]
        R4[Compliance Rules]
    end
    
    subgraph Validation["Validation"]
        V1{Safety OK?}
        V2{Priority OK?}
        V3{Business OK?}
        V4{Compliance OK?}
    end
    
    subgraph Result["Result"]
        Accept[Schedule Approved]
        Reject[Conflict Detected]
        Suggest[Alternative Suggested]
    end
    
    R1 --> V1
    R2 --> V2
    R3 --> V3
    R4 --> V4
    
    V1 -->|No| Reject
    V1 -->|Yes| V2
    V2 -->|No| Suggest
    V2 -->|Yes| V3
    V3 -->|No| Suggest
    V3 -->|Yes| V4
    V4 -->|No| Reject
    V4 -->|Yes| Accept
    
    style Rules fill:#e3f2fd,color:#000000
    style Validation fill:#fff3e0,color:#000000
    style Result fill:#e8f5e9,color:#000000
    linkStyle default stroke:#777777,stroke-width:3px
```

---

## Integration Points

### Module Interfaces

Each module interacts with the Scheduling System through defined interfaces:

| Module | Provides to Scheduler | Receives from Scheduler |
|--------|---------------------|------------------------|
| **Experiment Manager** | Sample metadata, duration estimates, facility needs | Approved time slots, conflict notifications |
| **System Ops Log** | Maintenance windows, shift schedules | Upcoming events, staff assignments |
| **Medical Isotope** | Production requirements, shipping deadlines | Batch scheduling, resource allocation |
| **Training** | Requalification needs, course schedules | Available slots, compliance tracking |
| **Personnel** | Staff availability, certification status | Shift assignments, coverage gaps |

### External Integrations

```mermaid
flowchart TD
    subgraph Internal["Axiom"]
        Sched[Scheduling System]
    end
    
    subgraph External["External Systems"]
        GCal[Google Calendar]
        Outlook[Outlook/Exchange]
        ICS[ICS/CalDAV]
        SMS[SMS Gateway]
        Email[Email Server]
    end
    
    Sched <-->|Sync| GCal
    Sched <-->|Sync| Outlook
    Sched -->|Export| ICS
    Sched -->|Alerts| SMS
    Sched -->|Notifications| Email
    
    style Internal fill:#1565c0,color:#fff
    style External fill:#616161,color:#fff
    style Sched fill:#e3f2fd,color:#333
    style GCal fill:#f5f5f5,color:#333
    style Outlook fill:#f5f5f5,color:#333
    style ICS fill:#f5f5f5,color:#333
    style SMS fill:#f5f5f5,color:#333
    style Email fill:#f5f5f5,color:#333
    linkStyle default stroke:#777777,stroke-width:3px
```

---

## User Stories

### Schedule Requesters

1. **As a researcher**, I want to see all available system time slots for next week so I can plan my experiment.

2. **As a maintenance engineer**, I want to block out 4 hours for pump replacement with automatic notifications to affected users.

3. **As a training coordinator**, I want to ensure each operator gets their 4 hours/quarter requalification scheduled before deadlines.

4. **As a medical isotope customer**, I want to see available production slots that meet my delivery requirements.

### Schedule Managers

5. **As a system manager**, I want to review all pending schedule requests in one place with conflict indicators.

6. **As a system manager**, I want to set recurring maintenance windows (e.g., "Every Tuesday 6-8 AM") that automatically block scheduling.

7. **As a shift supervisor**, I want to see staffing levels for next week to identify coverage gaps.

8. **As a facility director**, I want monthly utilization reports showing scheduled vs. actual usage by category.

### Schedule Consumers

9. **As a system operator**, I want to see the next 4 hours of scheduled activities on my console display.

10. **As any staff member**, I want to see today's facility schedule on the entrance display when I arrive.

11. **As a researcher**, I want automatic email/SMS reminders 24 hours before my scheduled system time.

12. **As a compliance officer**, I want to verify that all required training was completed within regulatory timeframes.

---

## Display Requirements

### Facility Entrance Display

```mermaid
flowchart TB
    subgraph Display["Facility Entrance Display"]
        Header["PartnerLab Facility Schedule"]
        
        subgraph Current["CURRENT"]
            C1["System - 100 kW"]
            C2["Beam Port 4 - Researcher A NAA"]
            C3["Pneumatic Rabbit - Available"]
        end
        
        subgraph Next["NEXT 2 HOURS"]
            N1["11:00 AM - Power change"]
            N2["11:30 AM - Sample removal"]
            N3["12:00 PM - Lunch break"]
        end
        
        subgraph Today["TODAY"]
            T1["2:00 PM - Medical isotope"]
            T2["3:30 PM - Tour group"]
            T3["4:00 PM - Shift change"]
        end
    end
    
    style Display fill:#263238,color:#fff
    style Current fill:#d32f2f,color:#fff
    style Next fill:#1976d2,color:#fff
    style Today fill:#388e3c,color:#fff
    linkStyle default stroke:#777777,stroke-width:3px
```

### Console 4-Hour View

```mermaid
gantt
    title System Console - Next 4 Hours
    dateFormat HH:mm
    axisFormat %H:%M
    section System
    100 kW Operation    :active, r1, 10:30, 11:00
    Power Change        :crit, r2, 11:00, 11:15
    50 kW Operation     :r3, 11:15, 14:30
    section Beam Port 4
    Researcher A NAA    :active, bp1, 10:30, 11:30
    Available           :bp2, 11:30, 14:00
    Researcher B Setup  :bp3, 14:00, 14:30
    section Pneumatic
    Available           :p1, 10:30, 14:00
    Medical Isotope     :crit, p2, 14:00, 14:30
    section Staff
    SRO: T. Miller      :s1, 10:30, 14:30
    RO: J. Davis        :s2, 10:30, 14:30
```

---

## Success Metrics

| Metric | Target | Measurement Method |
|--------|--------|-------------------|
| **Schedule Accuracy** | 90% of scheduled activities start within 15 min | Compare scheduled vs. actual from ops log |
| **Conflict Rate** | <5% of requests have conflicts | Track rejection reasons |
| **Self-Service Adoption** | 80% of requests via portal (not email/phone) | Source tracking on requests |
| **Notification Delivery** | 100% of approved requests get confirmation within 5 min | Notification system logs |
| **Utilization Visibility** | Real-time dashboard always within 1 hour of actual | Compare schedule to ops log |
| **Compliance Coverage** | 100% of required training scheduled before expiry | Training deadline reports |

---

## Technical Requirements

### Performance

- Schedule queries return in <200ms for week view
- Support 1000+ schedule items per month
- Handle 50+ concurrent schedule viewers
- Real-time updates to all displays within 5 seconds

### Reliability

- 99.9% uptime for viewing schedules
- Graceful degradation if external calendars unavailable
- Offline-first design for read-only schedule access
- Audit trail of all schedule changes

**See also:** [Master Tech Spec § 9.4: Phased Deployment Topology](../tech-specs/spec-executive.md#94-phased-deployment-topology) and [§ 9.6: System Resilience & Offline-First Pattern](../tech-specs/spec-executive.md#96-system-resilience--offline-first-pattern)

The Scheduling System implements offline-first patterns to support facility operations during cloud outages:
- **Local cache**: SQLite replica of schedules synced daily at minimum
- **Outage behavior**: Serves read-only schedules from local cache; changes queued for sync when online
- **Facility displays**: Independent local data sources prevent single point of failure
- **Phased deployment**: Control room displays work even if facility server is offline; facility server works even if cloud is offline

### Security

- Role-based access to approval functions
- Immutable audit log of approvals
- Encryption of external calendar credentials
- Rate limiting on public schedule views

---

## Implementation Phases

### Phase 1: Core Scheduling (Months 1-2)
- Basic time slot management
- Conflict detection
- Manual approval workflow
- Console and facility displays

### Phase 2: Module Integration (Months 2-3)
- Experiment Manager integration
- System Ops Log integration
- Notification system
- Basic reporting

### Phase 3: Advanced Features (Months 3-4)
- External calendar sync
- Recurring schedules
- Priority/optimization engine
- Mobile access

### Phase 4: Intelligence (Months 4-6)
- Utilization analytics
- Predictive scheduling
- Resource optimization
- Compliance forecasting

---

*This PRD defines the Scheduling System as a cross-cutting concern that serves all Axiom modules while maintaining separation of concerns.*
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
