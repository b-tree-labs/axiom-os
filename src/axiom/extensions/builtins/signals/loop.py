# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Loop tracking models for the Sense → Synthesize → Create → Publish → Sense cycle.

Tracks loop iterations, subscriptions, and health metrics to measure and
improve feedback loop velocity over time.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from axiom.infra.state import atomic_write


class LoopStage(Enum):
    """Stages in the product development loop."""
    SENSE = "sense"
    SYNTHESIZE = "synthesize"
    CREATE = "create"
    PUBLISH = "publish"
    FEEDBACK = "feedback"  # User feedback sensing


class FeedbackType(Enum):
    """Types of user feedback to sense."""
    USEFULNESS = "usefulness"  # Does it solve the problem?
    EASE = "ease"  # Is it intuitive?
    JOY = "joy"  # Is it delightful?
    PERFORMANCE = "performance"  # Is it fast/reliable?
    COMPLETENESS = "completeness"  # What's missing?


class SubscriberRole(Enum):
    """Roles that subscribe to loop artifacts."""
    PRODUCT = "product"
    DESIGN = "design"
    BUILD = "build"
    OPERATIONS = "operations"


class ArtifactType(Enum):
    """Types of artifacts produced in the loop."""
    PRD_UPDATE = "prd_update"
    DESIGN_BRIEF = "design_brief"
    DESIGN_SPEC = "design_spec"
    ISSUE = "issue"
    FEATURE = "feature"
    FEEDBACK_REPORT = "feedback_report"


class DeliveryMethod(Enum):
    """How subscribers receive artifacts."""
    EMAIL = "email"
    SLACK = "slack"
    FEED = "feed"
    WEBHOOK = "webhook"


@dataclass
class Subscription:
    """Who subscribes to what artifacts."""
    subscription_id: str
    subscriber_id: str  # Person or agent identifier
    subscriber_role: SubscriberRole
    artifact_type: ArtifactType
    initiative_filter: list[str] | None = None  # Specific PRDs/initiatives
    signal_type_filter: list[str] | None = None  # Specific signal types
    delivery_method: DeliveryMethod = DeliveryMethod.FEED
    active: bool = True
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def matches(self, artifact_type: ArtifactType, initiative: str | None) -> bool:
        """Check if this subscription matches an artifact."""
        if not self.active:
            return False
        if self.artifact_type != artifact_type:
            return False
        if self.initiative_filter and initiative not in self.initiative_filter:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "subscription_id": self.subscription_id,
            "subscriber_id": self.subscriber_id,
            "subscriber_role": self.subscriber_role.value,
            "artifact_type": self.artifact_type.value,
            "initiative_filter": self.initiative_filter,
            "signal_type_filter": self.signal_type_filter,
            "delivery_method": self.delivery_method.value,
            "active": self.active,
            "created_at": self.created_at,
        }


@dataclass
class LoopIteration:
    """Tracks one complete loop cycle for a feature/initiative.

    A loop iteration starts when initial signals are sensed and ends
    when user feedback on the shipped feature is captured.
    """
    iteration_id: str
    initiative: str  # PRD or feature name

    # Stage timestamps (None = not reached yet)
    initial_sense_at: str | None = None
    synthesis_at: str | None = None
    design_complete_at: str | None = None
    published_at: str | None = None
    feedback_sensed_at: str | None = None

    # Current stage
    current_stage: LoopStage = LoopStage.SENSE

    # Linked artifacts
    signal_ids: list[str] = field(default_factory=list)
    prd_draft_ids: list[str] = field(default_factory=list)
    design_brief_ids: list[str] = field(default_factory=list)
    design_spec_ids: list[str] = field(default_factory=list)
    issue_ids: list[str] = field(default_factory=list)
    feature_ids: list[str] = field(default_factory=list)
    feedback_signal_ids: list[str] = field(default_factory=list)

    # Quality tracking
    rework_count: int = 0
    quality_score: float | None = None

    # Status
    closed: bool = False
    closed_at: str | None = None

    @property
    def cycle_time_days(self) -> float | None:
        """Calculate cycle time from initial sense to feedback."""
        if not self.initial_sense_at or not self.feedback_sensed_at:
            return None
        start = datetime.fromisoformat(self.initial_sense_at)
        end = datetime.fromisoformat(self.feedback_sensed_at)
        return (end - start).total_seconds() / 86400

    @property
    def stage_durations(self) -> dict[str, float | None]:
        """Calculate duration of each stage in days."""
        durations = {}

        timestamps = [
            ("sense_to_synthesize", self.initial_sense_at, self.synthesis_at),
            ("synthesize_to_design", self.synthesis_at, self.design_complete_at),
            ("design_to_publish", self.design_complete_at, self.published_at),
            ("publish_to_feedback", self.published_at, self.feedback_sensed_at),
        ]

        for name, start, end in timestamps:
            if start and end:
                s = datetime.fromisoformat(start)
                e = datetime.fromisoformat(end)
                durations[name] = (e - s).total_seconds() / 86400
            else:
                durations[name] = None

        return durations

    def advance_to(self, stage: LoopStage) -> None:
        """Advance iteration to a new stage."""
        now = datetime.now().isoformat()
        self.current_stage = stage

        if stage == LoopStage.SENSE and not self.initial_sense_at:
            self.initial_sense_at = now
        elif stage == LoopStage.SYNTHESIZE:
            self.synthesis_at = now
        elif stage == LoopStage.CREATE:
            pass  # design_complete_at set when design is done
        elif stage == LoopStage.PUBLISH:
            self.published_at = now
        elif stage == LoopStage.FEEDBACK:
            self.feedback_sensed_at = now

    def mark_design_complete(self) -> None:
        """Mark design stage as complete."""
        self.design_complete_at = datetime.now().isoformat()

    def close(self, quality_score: float | None = None) -> None:
        """Close the loop iteration."""
        self.closed = True
        self.closed_at = datetime.now().isoformat()
        if quality_score:
            self.quality_score = quality_score

    def to_dict(self) -> dict:
        return {
            "iteration_id": self.iteration_id,
            "initiative": self.initiative,
            "current_stage": self.current_stage.value,
            "initial_sense_at": self.initial_sense_at,
            "synthesis_at": self.synthesis_at,
            "design_complete_at": self.design_complete_at,
            "published_at": self.published_at,
            "feedback_sensed_at": self.feedback_sensed_at,
            "cycle_time_days": self.cycle_time_days,
            "stage_durations": self.stage_durations,
            "signal_ids": self.signal_ids,
            "prd_draft_ids": self.prd_draft_ids,
            "design_brief_ids": self.design_brief_ids,
            "design_spec_ids": self.design_spec_ids,
            "issue_ids": self.issue_ids,
            "feature_ids": self.feature_ids,
            "feedback_signal_ids": self.feedback_signal_ids,
            "rework_count": self.rework_count,
            "quality_score": self.quality_score,
            "closed": self.closed,
            "closed_at": self.closed_at,
        }


@dataclass
class LoopHealthMetrics:
    """Aggregated health metrics for the design loop."""
    computed_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # Velocity metrics (in days)
    avg_cycle_time: float | None = None
    avg_sense_latency: float | None = None
    avg_synthesis_latency: float | None = None
    avg_create_latency: float | None = None
    avg_publish_latency: float | None = None
    avg_feedback_latency: float | None = None

    # Quality metrics
    avg_signal_quality: float | None = None
    synthesis_approval_rate: float | None = None
    design_hit_rate: float | None = None
    rework_rate: float | None = None

    # Throughput metrics
    signals_per_day: float | None = None
    synthesis_per_day: float | None = None
    designs_per_week: float | None = None
    features_per_sprint: float | None = None

    # Loop health
    open_iterations: int = 0
    closed_iterations: int = 0
    loop_closure_rate: float | None = None  # % of iterations that close within target

    @property
    def health_score(self) -> float | None:
        """Compute overall health score (0-100)."""
        scores = []

        # Velocity component (lower is better, target < 30 days)
        if self.avg_cycle_time:
            velocity_score = max(0, 100 - (self.avg_cycle_time - 14) * 3)
            scores.append(velocity_score)

        # Quality component
        if self.synthesis_approval_rate:
            scores.append(self.synthesis_approval_rate * 100)
        if self.design_hit_rate:
            scores.append(self.design_hit_rate * 100)

        # Closure rate
        if self.loop_closure_rate:
            scores.append(self.loop_closure_rate * 100)

        return sum(scores) / len(scores) if scores else None

    def to_dict(self) -> dict:
        return {
            "computed_at": self.computed_at,
            "velocity": {
                "avg_cycle_time_days": self.avg_cycle_time,
                "avg_sense_latency_days": self.avg_sense_latency,
                "avg_synthesis_latency_days": self.avg_synthesis_latency,
                "avg_create_latency_days": self.avg_create_latency,
                "avg_publish_latency_days": self.avg_publish_latency,
                "avg_feedback_latency_days": self.avg_feedback_latency,
            },
            "quality": {
                "avg_signal_quality": self.avg_signal_quality,
                "synthesis_approval_rate": self.synthesis_approval_rate,
                "design_hit_rate": self.design_hit_rate,
                "rework_rate": self.rework_rate,
            },
            "throughput": {
                "signals_per_day": self.signals_per_day,
                "synthesis_per_day": self.synthesis_per_day,
                "designs_per_week": self.designs_per_week,
                "features_per_sprint": self.features_per_sprint,
            },
            "loop_health": {
                "open_iterations": self.open_iterations,
                "closed_iterations": self.closed_iterations,
                "loop_closure_rate": self.loop_closure_rate,
                "health_score": self.health_score,
            },
        }


class LoopTracker:
    """Tracks loop iterations and computes health metrics.

    Usage:
        tracker = LoopTracker()

        # Start a new iteration
        iteration = tracker.start_iteration("experiment_manager", signal_ids=["sig-001"])

        # Advance through stages
        tracker.advance(iteration.iteration_id, LoopStage.SYNTHESIZE)
        tracker.add_artifact(iteration.iteration_id, "prd_draft", "draft-001")

        # Close when feedback received
        tracker.close_iteration(iteration.iteration_id, quality_score=0.85)

        # Get health metrics
        metrics = tracker.compute_metrics()
    """

    def __init__(self, store_path: Path | None = None):
        self.store_path = store_path or (
            Path(__file__).parent.parent / "inbox" / "processed" / "loop_tracking.json"
        )
        self.iterations: dict[str, LoopIteration] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self._load()

    def _load(self) -> None:
        """Load tracking data from disk."""
        if self.store_path.exists():
            data = json.loads(self.store_path.read_text())
            # Hydrate iterations
            for it_data in data.get("iterations", []):
                it = LoopIteration(
                    iteration_id=it_data["iteration_id"],
                    initiative=it_data["initiative"],
                )
                it.current_stage = LoopStage(it_data.get("current_stage", "sense"))
                it.initial_sense_at = it_data.get("initial_sense_at")
                it.synthesis_at = it_data.get("synthesis_at")
                it.design_complete_at = it_data.get("design_complete_at")
                it.published_at = it_data.get("published_at")
                it.feedback_sensed_at = it_data.get("feedback_sensed_at")
                it.signal_ids = it_data.get("signal_ids", [])
                it.prd_draft_ids = it_data.get("prd_draft_ids", [])
                it.design_brief_ids = it_data.get("design_brief_ids", [])
                it.design_spec_ids = it_data.get("design_spec_ids", [])
                it.issue_ids = it_data.get("issue_ids", [])
                it.feature_ids = it_data.get("feature_ids", [])
                it.feedback_signal_ids = it_data.get("feedback_signal_ids", [])
                it.rework_count = it_data.get("rework_count", 0)
                it.quality_score = it_data.get("quality_score")
                it.closed = it_data.get("closed", False)
                it.closed_at = it_data.get("closed_at")
                self.iterations[it.iteration_id] = it

    def _save(self) -> None:
        """Persist tracking data to disk."""
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "iterations": [it.to_dict() for it in self.iterations.values()],
            "subscriptions": [sub.to_dict() for sub in self.subscriptions.values()],
            "saved_at": datetime.now().isoformat(),
        }
        atomic_write(self.store_path, data)

    def start_iteration(
        self,
        initiative: str,
        signal_ids: list[str] | None = None,
    ) -> LoopIteration:
        """Start a new loop iteration."""
        iteration_id = f"loop_{initiative}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        iteration = LoopIteration(
            iteration_id=iteration_id,
            initiative=initiative,
            initial_sense_at=datetime.now().isoformat(),
            signal_ids=signal_ids or [],
        )
        self.iterations[iteration_id] = iteration
        self._save()
        return iteration

    def advance(self, iteration_id: str, stage: LoopStage) -> None:
        """Advance an iteration to a new stage."""
        if iteration_id in self.iterations:
            self.iterations[iteration_id].advance_to(stage)
            self._save()

    def add_artifact(
        self,
        iteration_id: str,
        artifact_type: str,
        artifact_id: str,
    ) -> None:
        """Link an artifact to an iteration."""
        if iteration_id not in self.iterations:
            return

        it = self.iterations[iteration_id]

        if artifact_type == "signal":
            it.signal_ids.append(artifact_id)
        elif artifact_type == "prd_draft":
            it.prd_draft_ids.append(artifact_id)
        elif artifact_type == "design_brief":
            it.design_brief_ids.append(artifact_id)
        elif artifact_type == "design_spec":
            it.design_spec_ids.append(artifact_id)
        elif artifact_type == "issue":
            it.issue_ids.append(artifact_id)
        elif artifact_type == "feature":
            it.feature_ids.append(artifact_id)
        elif artifact_type == "feedback":
            it.feedback_signal_ids.append(artifact_id)

        self._save()

    def close_iteration(
        self,
        iteration_id: str,
        quality_score: float | None = None,
    ) -> None:
        """Close an iteration."""
        if iteration_id in self.iterations:
            self.iterations[iteration_id].close(quality_score)
            self._save()

    def get_open_iterations(self) -> list[LoopIteration]:
        """Get all non-closed iterations."""
        return [it for it in self.iterations.values() if not it.closed]

    def get_iterations_by_initiative(self, initiative: str) -> list[LoopIteration]:
        """Get all iterations for an initiative."""
        return [it for it in self.iterations.values() if it.initiative == initiative]

    def compute_metrics(self, _since_days: int = 30) -> LoopHealthMetrics:
        """Compute health metrics from recent iterations."""
        _today = datetime.now().isoformat()[:10]  # Simplified

        # Filter to recent closed iterations
        closed = [
            it for it in self.iterations.values()
            if it.closed and it.cycle_time_days is not None
        ]

        metrics = LoopHealthMetrics()
        metrics.open_iterations = len(self.get_open_iterations())
        metrics.closed_iterations = len(closed)

        if closed:
            # Velocity metrics
            cycle_times = [it.cycle_time_days for it in closed if it.cycle_time_days]
            if cycle_times:
                metrics.avg_cycle_time = sum(cycle_times) / len(cycle_times)

            # Stage latencies
            sense_to_synth: list[float] = [
                d for it in closed
                if (d := it.stage_durations.get("sense_to_synthesize")) is not None
            ]
            if sense_to_synth:
                metrics.avg_synthesis_latency = sum(sense_to_synth) / len(sense_to_synth)

            # Quality
            quality_scores: list[float] = [
                q for it in closed if (q := it.quality_score) is not None
            ]
            if quality_scores:
                metrics.design_hit_rate = sum(quality_scores) / len(quality_scores)

            # Rework
            rework_counts = [it.rework_count for it in closed]
            if rework_counts:
                metrics.rework_rate = sum(1 for r in rework_counts if r > 0) / len(rework_counts)

            # Closure rate (closed within 30 days)
            fast_closures = sum(1 for it in closed if it.cycle_time_days and it.cycle_time_days <= 30)
            metrics.loop_closure_rate = fast_closures / len(closed)

        return metrics

    def add_subscription(self, subscription: Subscription) -> None:
        """Add a subscription."""
        self.subscriptions[subscription.subscription_id] = subscription
        self._save()

    def get_subscribers(
        self,
        artifact_type: ArtifactType,
        initiative: str | None = None,
    ) -> list[Subscription]:
        """Get subscriptions matching an artifact."""
        return [
            sub for sub in self.subscriptions.values()
            if sub.matches(artifact_type, initiative)
        ]
