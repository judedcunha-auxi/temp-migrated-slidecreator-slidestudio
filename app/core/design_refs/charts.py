"""The chart types the export can emit natively (authoring contract §Charts).

One constant, shared by the design prompt (`app/core/design/prompts.py`) and the brief's feasibility
check (`slide_brief.chart_types`), so the list the model is told and the list a brief is checked
against cannot drift. `bubble`, `radar`, stock and surface are deliberately absent: the renderer draws
nothing for them, so they are a lint error.
"""

from __future__ import annotations

CHART_TYPES = ("column, column_stacked, column_stacked_100, bar, bar_stacked, bar_stacked_100, line, "
               "line_markers, area, area_stacked, pie, pie_exploded, doughnut, doughnut_exploded, "
               "scatter, scatter_lines, waterfall")
