# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What a display *is*, separately from how anything draws it.

A chart catalog is not a folder of rendering code. It is the set of claims a
picture makes — what it plots, in what units, over what window, and on whose
authority — and those claims have to survive being rendered twice, in a
terminal and in a browser, without drifting apart.

So an entry here carries no drawing instructions at all. It names a ``kind``
from a closed vocabulary, and a renderer decides what a ``histogram`` looks
like on its own surface.

Three things make this worth a registry rather than a dict in a template.

**Provenance is part of the display, not a caption.** A chart drawn from a
model must never render as a measurement. That rule cannot live in the
renderer, because then every renderer has to remember it and the one that
forgets produces something indistinguishable from a reading. Here a spec
declares its provenance, and :meth:`ChartSpec.validate` refuses one that
claims to be measured while naming a modelled input.

**Inputs are named by role, where a role exists.** A chart whose inputs are
``fluid_temperature`` and ``wall_temperature`` can be drawn for any loop that
declares those roles; one whose inputs are ``STC1`` and ``PTC1`` can only be
drawn for VCU. Both are legitimate — a site's own instrument panel is properly
site-specific — but the distinction is what makes a peer comparison possible,
so :attr:`ChartSpec.portable` states it rather than leaving it to be inferred.

**A chart that cannot be drawn should say so.** Empty axes read as "your data
is broken"; that is exactly the confusion the serving catalog just had to be
fixed for. A spec carries the predicate for having enough to draw, so a
surface can say "no rod-position data in this window" instead of rendering
nothing and letting the viewer guess.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: The closed vocabulary of display kinds. A renderer implements these; a
#: catalog entry may not invent one, because a kind nothing can draw is a
#: promise the surface cannot keep.
#:
#: Kept deliberately small and shape-based rather than domain-based: there is
#: no `reactor_power_chart`, because that is a *chart*, not a kind of picture.
CHART_KINDS: tuple[str, ...] = (
    "timeseries",            # value(s) against time
    "dual_axis_timeseries",  # two series, two units, one time axis
    "measured_vs_predicted",  # the parity plot; carries the labelling rule
    "scatter",               # one channel against another, optional colour axis
    "xy_fit",                # scatter plus a fitted curve, and the fit's residual
    "bar",                   # one value per discrete bucket
    "histogram",             # binned distribution of one value
    "stacked_area",          # composition over time, parts summing to a whole
    "scalar_panel",          # current values, no axis
    "event_table",           # discrete occurrences with times
    "hex_map",               # per-position values on a hexagonal lattice
    "field_2d",              # a scalar field over a plane
    "coverage",              # what data exists, over what span
)

#: What the picture is made of. Distinct from a row's ``source_class``: a
#: single chart may combine rows of several classes, and the chart's own claim
#: is about the whole.
PROVENANCE = ("measured", "model_derived", "hybrid")

#: Row classes that make a chart model-derived. Mirrors MODELLED_CLASSES in the
#: conformance vocabulary, plus ``simulated``: synthetic data has never been a
#: measurement either, and a display that renders it as one is the failure this
#: whole module exists to prevent.
_MODELLED_INPUT_CLASSES = frozenset({"predicted", "estimated", "simulated"})

#: How a chart's x-extent is chosen.
WINDOWS = (
    "absolute",      # wall-clock range the caller picks
    "run_relative",  # time since the start of one run
    "instant",       # a single moment; no range
    "none",          # not time-based at all
)

#: How charts are grouped for a surface that has to lay them out.
#:
#: The grouping is not decoration. A surface with thirty charts and no grouping
#: shows them in whatever order the registry iterated, and the viewer's first
#: question — "which of these is about the thing I came here for" — goes
#: unanswered. Groups are by *question asked*, not by data source or by kind,
#: because two timeseries from different sources can answer the same question
#: and a viewer is looking for the question.
CHART_GROUPS: tuple[str, ...] = (
    "operations",    # what the plant did: power, rods, temperatures, status
    "thermal",       # where the heat went: fluid vs wall, balance, transport
    "runs",          # one run, or the distribution across many
    "model_parity",  # model against measurement, and the residual
    "spatial",       # position-resolved: core maps, fields, burnup
    "data_health",   # what data exists, how fresh, how complete
)

_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class ChartSpecError(ValueError):
    """A catalog entry that could not be admitted, and why."""


@dataclass(frozen=True)
class ChartSpec:
    """One display, described rather than drawn.

    ``chart_id`` is stable for the life of the chart: it is what a saved view,
    a bookmark and an audit record refer to. Renaming the human ``name`` is
    free; changing the id is not.
    """

    chart_id: str
    name: str
    kind: str
    provenance: str
    #: Roles this chart plots, e.g. ``("fluid_temperature", "wall_temperature")``.
    #: A chart defined on roles is drawable at any site that declares them.
    roles: tuple[str, ...] = ()
    #: Literal channel names, for a display that is genuinely site-specific.
    #: Using these is what makes a chart non-portable, which is a real choice
    #: and not a mistake — it is only a mistake when it is accidental.
    channels: tuple[str, ...] = ()
    #: The unit the y-axis is in, and who says so. ``unit_authority`` names
    #: where that came from — a site's channel map, a conversion, a standard —
    #: because an axis label nobody can trace is how a wrong unit survives.
    unit: str | None = None
    unit_authority: str | None = None
    window: str = "absolute"
    #: Minimum distinct series required before the chart means anything. A
    #: parity plot with one series is not a parity plot.
    min_series: int = 1
    #: Surfaces permitted to show it. Empty means unrestricted. Opt-in, so a
    #: chart is not exposed because it exists but because it said so.
    surfaces: tuple[str, ...] = ()
    #: Which group this belongs to, and where it sits within it.
    #:
    #: ``order`` is the default layout position, low first. A surface is free to
    #: override, but a catalog that expresses no preference forces every surface
    #: to invent one, and they will not agree.
    group: str = "operations"
    order: int = 100
    #: Shown by default on a surface that has not been told otherwise. Most
    #: charts are not: a default view crowded with everything is the same
    #: failure as no grouping at all.
    default: bool = False
    #: The gold verb that serves this chart's data, if one exists.
    #:
    #: ``None`` means no verb serves it yet — the catalog then reports the
    #: chart as a *data* gap rather than a rendering job, which is the
    #: distinction that decides who picks it up. A catalog that cannot tell
    #: those apart sends a front-end engineer to build a picture for data
    #: nobody can fetch.
    verb: str | None = None
    #: Free-form provenance note — which view or export this came from.
    origin: str | None = None
    notes: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def servable(self) -> bool:
        """True when a gold verb can actually fetch this chart's data."""
        return self.verb is not None

    @property
    def portable(self) -> bool:
        """True when this chart can be drawn for any site declaring its roles.

        A portable chart is what a peer comparison is built from. A chart that
        names literal channels answers a question about one installation.
        """
        return bool(self.roles) and not self.channels

    @property
    def inputs(self) -> tuple[str, ...]:
        return tuple(self.roles) + tuple(self.channels)

    def validate(self) -> list[str]:
        """Everything wrong with this entry, rather than the first thing."""
        errors: list[str] = []
        if not _ID.match(self.chart_id or ""):
            errors.append(
                f"chart_id {self.chart_id!r} is not a stable slug "
                "(lowercase, digits, . _ -)"
            )
        if not (self.name or "").strip():
            errors.append("name is empty — a chart_id is not a title")
        if self.kind not in CHART_KINDS:
            errors.append(
                f"kind {self.kind!r} is not one of {', '.join(CHART_KINDS)} — "
                "a kind nothing can draw is a promise the surface cannot keep"
            )
        if self.provenance not in PROVENANCE:
            errors.append(f"provenance {self.provenance!r} is not one of {', '.join(PROVENANCE)}")
        if self.group not in CHART_GROUPS:
            errors.append(f"group {self.group!r} is not one of {', '.join(CHART_GROUPS)}")
        if self.window not in WINDOWS:
            errors.append(f"window {self.window!r} is not one of {', '.join(WINDOWS)}")
        if not self.inputs:
            errors.append("no roles and no channels — nothing to plot")
        if self.unit and not self.unit_authority:
            errors.append(
                f"unit {self.unit!r} is declared with no unit_authority — an axis "
                "label nobody can trace is how a wrong unit survives"
            )
        if self.kind == "measured_vs_predicted":
            if self.provenance == "measured":
                errors.append(
                    "a measured_vs_predicted chart is 'hybrid' at least — it plots "
                    "a model against a measurement, and calling the pair a "
                    "measurement is the mislabelling this kind exists to prevent"
                )
            if self.min_series < 2:
                errors.append("measured_vs_predicted needs at least two series to compare")
        if self.kind == "dual_axis_timeseries" and self.min_series < 2:
            errors.append("dual_axis_timeseries plots two series; min_series must be at least 2")
        return errors

    def check_inputs(self, classes: dict[str, str]) -> list[str]:
        """Complain if the data behind this chart contradicts its label.

        ``classes`` maps an input name to the ``source_class`` of the rows
        actually behind it. A chart claiming to be measured while drawing a
        prediction is the error worth catching at the moment of drawing, since
        nothing downstream can tell afterwards.
        """
        modelled = sorted(
            name for name, cls in classes.items()
            if name in self.inputs and cls in _MODELLED_INPUT_CLASSES
        )
        if modelled and self.provenance == "measured":
            return [
                f"{self.chart_id}: declared 'measured' but "
                f"{', '.join(modelled)} carries modelled data "
                f"({', '.join(sorted({classes[m] for m in modelled}))}) — "
                "a model must never render as a measurement"
            ]
        if not modelled and self.provenance == "model_derived":
            return [
                f"{self.chart_id}: declared 'model_derived' but every input is "
                "measured — the label overstates and readers discount it"
            ]
        return []


class ChartRegistry:
    """``chart_id`` → spec. Mirrors the normalizer registry's contract."""

    def __init__(self) -> None:
        self._by_id: dict[str, ChartSpec] = {}

    def register(self, spec: ChartSpec) -> None:
        errors = spec.validate()
        if errors:
            raise ChartSpecError(f"{spec.chart_id or '<no id>'}: " + "; ".join(errors))
        if spec.chart_id in self._by_id:
            raise ChartSpecError(
                f"chart_id {spec.chart_id!r} is already registered — ids are stable "
                "references, so two charts may not share one"
            )
        self._by_id[spec.chart_id] = spec

    def get(self, chart_id: str) -> ChartSpec | None:
        return self._by_id.get(chart_id)

    def ids(self) -> list[str]:
        return sorted(self._by_id)

    def all(self) -> list[ChartSpec]:
        return [self._by_id[i] for i in self.ids()]

    def by_kind(self, kind: str) -> list[ChartSpec]:
        return [s for s in self.all() if s.kind == kind]

    def portable(self) -> list[ChartSpec]:
        """Charts drawable at any site declaring their roles."""
        return [s for s in self.all() if s.portable]

    def grouped(self) -> dict[str, list[ChartSpec]]:
        """``group -> specs``, each group in its own default order.

        This is what a surface lays out. Groups come back in the declared
        order of :data:`CHART_GROUPS` rather than alphabetically, because the
        sequence is itself a judgement: operations before parity before
        spatial is how someone reads a plant.
        """
        out: dict[str, list[ChartSpec]] = {}
        for group in CHART_GROUPS:
            members = [s for s in self.all() if s.group == group]
            if members:
                out[group] = sorted(members, key=lambda s: (s.order, s.chart_id))
        return out

    def defaults(self, group: str | None = None) -> list[ChartSpec]:
        """The charts a surface shows before anyone has chosen anything."""
        picked = [s for s in self.all() if s.default and (group is None or s.group == group)]
        return sorted(picked, key=lambda s: (CHART_GROUPS.index(s.group), s.order, s.chart_id))

    def comparable(self, chart_id: str, roles_by_site: dict[str, list[str]]) -> list[str]:
        """Which sites this chart can be drawn for, for a like-for-like compare.

        Only portable charts qualify: a chart naming literal channels asks a
        question about one installation, and answering it for another site
        would mean pretending two different instruments are the same one.
        """
        spec = self.get(chart_id)
        if spec is None or not spec.portable:
            return []
        wanted = set(spec.roles)
        return sorted(site for site, roles in roles_by_site.items() if wanted.issubset(roles))

    def verb_gaps(self) -> list[ChartSpec]:
        """Charts with no gold verb behind them.

        This is the catalog's most useful answer. A chart in here is a data
        job, not a rendering job, and confusing the two is how someone spends a
        week building a picture for numbers nobody can fetch.
        """
        return [s for s in self.all() if not s.servable]

    def verbs_required(self) -> dict[str, list[str]]:
        """``verb -> chart_ids`` it serves, for the verbs that exist.

        Read the other way it says which verb carries the most weight, and
        therefore which one being slow or wrong is felt everywhere.
        """
        out: dict[str, list[str]] = {}
        for spec in self.all():
            if spec.verb:
                out.setdefault(spec.verb, []).append(spec.chart_id)
        return out

    def drawable_with(self, roles: Sequence[str]) -> list[ChartSpec]:
        """Which portable charts a site with these roles can actually show.

        The point of asking is to avoid offering a chart that would come back
        empty — an empty chart reads as broken data, not as missing coverage.
        """
        have = set(roles)
        return [s for s in self.portable() if have.issuperset(s.roles)]


__all__ = [
    "CHART_GROUPS",
    "CHART_KINDS",
    "PROVENANCE",
    "WINDOWS",
    "ChartRegistry",
    "ChartSpec",
    "ChartSpecError",
]
