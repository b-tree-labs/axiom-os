# Active Initiatives — Example
# Copy to runtime/config/initiatives.md and fill in your projects.
#
# This file is used by the correlator to map discussion topics to known
# projects. The ID is used internally for signal correlation.
#
# Aliases column: comma-separated alternative names, abbreviations,
# and sub-features that should resolve to this initiative.
# This prevents initiative fragmentation in reports (e.g., "DT Project"
# and "Digital Twin Project" appearing as separate sections).
#
# Weight column: strategic importance (1=low, 5=critical). Controls
# ordering in weekly summaries — higher weight initiatives listed first.
# Default is 3 if omitted.
#
# Pause Reason column: if set, the initiative is treated as paused in
# summaries with the reason displayed. Leave empty for active initiatives.

| ID | Name | Aliases | Status | Owners | Repos | Weight | Pause Reason |
|----|------|---------|--------|--------|-------|--------|--------------|
| 1 | Main Digital Twin | DT, digital twin | Active | Smith, Doe | facility_digital_twin | 4 | |
| 2 | Benchmark Suite | benchmarks | Planning | Wong | benchmarks | 3 | |
| 3 | Data Platform | data infra, analytics | Active | Doe | data_platform | 3 | |
