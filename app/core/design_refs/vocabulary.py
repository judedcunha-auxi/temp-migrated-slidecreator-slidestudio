"""The visual vocabulary the design chat draws on: slide types and framework recipes.

Provenance. Ported on 2026-09-28 from the sister repo Slide-Creator (read-only there):
- `netlify/functions/_shared/slideTypes.ts: SCAFFOLDS` -> `SLIDE_TYPES` (22 layout archetypes);
- `netlify/functions/_shared/frameworks.ts: FRAMEWORKS` -> `FRAMEWORKS`, and `ALIASES` -> `ALIASES`.
Both registries were calibrated there on 932 reference consulting slides.

Why they are rewritten, not copied: Slide-Creator feeds them to an IMAGE model that paints a whole 16:9
picture. Slide Studio's model writes HTML under the authoring contract (`docs/engine/03-AUTHORING-CONTRACT.md`)
that the deterministic engine rebuilds as native PowerPoint. So every hint here is translated into that
vocabulary: charts are `data-chart` (bubble and radar are not native, so they become SVG circles and text),
Harvey balls, checks and diamonds are small SVG or CSS shapes (a Unicode glyph the theme font lacks is a
linted defect), logos, flags and photos appear only if the project has them as assets, and the "do not render
page numbers / render as an image" rules are gone because the master draws the furniture. The hints are also
cut to one terse line each: the whole library rides in the cached system prompt on every turn, so each word
costs on every call.

`VOCABULARY` is the pre-rendered Markdown block `prompts.DESIGN_SYSTEM` embeds. It is plain text with single
braces; `prompts` escapes it for `str.format`. `ALIASES` is not rendered (it exists to map free-text framework
names onto the canonical ones) - see `canonical_framework`.
"""
from __future__ import annotations

import re

#: Slide type -> layout directive. Order is the order the prompt lists them: furniture first, then the body
#: workhorses, then the evidence/synthesis/program types, then the sparing accents.
SLIDE_TYPES: dict[str, str] = {
    "title": "the master's title layout, its placeholders only (client, deliverable, date); nothing competing.",
    "agenda": "numbered section list, oversized numerals in primary; reused as a tracker with the active "
              "section in accent and the rest grey.",
    "divider": "the master's section layout: section number + short heading; whitespace is the design.",
    "navigator": "the deck's framework map redrawn, every tile grey except the active section (accent outline "
                 "+ 'Focus of this section' tag); longer decks only.",
    "closing": "the recommendation as a 'therefore': summary columns joined by plus badges into one accent "
               "verdict band; next steps as 3-5 numbered actions with owner and date.",
    "framework": "DEFAULT body slide. One framework from the library is the main exhibit, composed with a side "
                 "panel and one takeaway; 1-3-word labels, bold lead phrases.",
    "data": "one hero `data-chart`, labels on every mark, unit in the chart header, the so-what annotated on "
            "the chart; context and implications in a side rail <= 1/3 of the width.",
    "table": "one dense matrix: a row spine with group bands, cells reduced to SVG checks / Harvey balls / heat "
             "shading decoded by one legend, a verdict column closing every row.",
    "comparison": "2-4 options in strictly parallel columns (same row labels, mirrored bullets), +/- rows; the "
                  "winner flagged by an accent border and a 'Recommended' tag or verdict band.",
    "process": "left-to-right numbered steps (chevrons or columns), verb-led headers, a dark-to-light ramp of "
               "primary, parallel 2-3-bullet stacks, deliverables aligned on one baseline beneath.",
    "timeline": "gantt (left activity rail, shaded month columns, bars ending in labelled milestone diamonds) or "
                "dated milestones alternating above/below one axis line.",
    "benchmark": "one row per peer: name (logo only if in assets) + one hard fact line, theme keywords "
                 "highlighted in legend colours; right-rail takeaway turning evidence into rules.",
    "case-study": "one named subject per slide: identity panel left or top, fixed panels (overview, exhibit, "
                  "headline stats), a dark so-what sidebar; sibling slides reuse the template exactly.",
    "map": "a map only if one is in the assets (never invent one): numbered pins + peripheral callout cards; "
           "without one, a regional grid or table. So-what in a side rail.",
    "scorecard": "one option against a fixed criteria spine scored with Harvey balls or checks + a one-line "
                 "rationale each, one verdict badge; siblings keep the scaffold so readers diff glyphs.",
    "synthesis": "findings panel -> one chevron -> numbered recommendations panel; every item opens with a bold "
                 "lead-in phrase.",
    "charter": "one initiative: metadata strip (owner, budget, start, duration) over a 3-column grid of field "
               "panels (description, stakeholders, risks, outputs, dependencies), 2-3 bullets each.",
    "deep-dive": "the parent framework in miniature at the top, non-focal elements grey and the focal one in "
                 "accent; the body expands that element into fixed detail panels.",
    "org": "hierarchy boxes coloured by function, a left rail naming the tiers, arrows labelled with what "
           "flows; compact legend. RACI grids: one owner per row.",
    "kpi": "3-5 equal tiles, each one oversized number (the headline one in accent) over a one-line qualifier.",
    "market": "TAM/SAM/SOM as concentric circles or a narrowing funnel, values inside, filter criteria "
              "labelled at each stage.",
    "quote": "one large attributed statement (or 2-3), bold only the operative phrase, name and role beneath.",
}

#: Framework name -> how to build it in this app's HTML. Grouped only for readability in the prompt; the
#: names are the Slide-Creator canonical keys so `ALIASES` resolves onto them.
FRAMEWORKS: dict[str, dict[str, str]] = {
    "Matrices and positioning": {
        "2x2 matrix": "two named axes with low->high arrows, quadrant verdict labels, items as numbered circles "
                      "keyed to a side list, the verdict quadrant tinted.",
        "SWOT analysis": "four quadrant panels, same internal structure, <= 4 bold-lead bullets each; accent "
                         "only the quadrant that drives the recommendation.",
        "BCG growth-share matrix": "2x2 of market growth (y) vs relative share (x), archetype labels in the "
                                   "corners, offerings as SVG circles sized by revenue.",
        "bubble chart": "NOT native: SVG circles (area ~ value) on two labelled axes, direct labels, target zone "
                        "tinted; takeaway in one accent box.",
        "Venn diagram": "2-3 outlined circles, one-word labels, only the overlap filled in accent with the "
                        "takeaway; bullets outside on straight leader lines.",
        "archetype spectrum (continuum)": "one labelled axis arrow ordering 3-5 identical archetype cards; the "
                                          "verdict is the highlighted card, not prose.",
        "heatmap": "single-hue intensity cells over grouped rows, no text in cells, a 3-step legend; takeaway in "
                   "one banner or verdict column.",
        "value chain heatmap": "chevron value-chain header as the column axis over grouped rows, two fills "
                               "only (primary/secondary), minimal legend.",
        "RAG status matrix": "reuse an established framework and overlay only red/amber/green status dots with "
                             "a footer legend.",
    },
    "Flows and sequences": {
        "value chain": "left-to-right chevrons with objective/description rows column-aligned beneath each "
                       "stage; example cards hang under their stage.",
        "chevron process flow": "3-8 equal chevrons, each over an aligned descriptor card with one bold phrase; "
                                "accent only the focal step.",
        "numbered step process": "equal step columns, oversized numerals on a dark-to-light ramp, verb-led "
                                 "headers, parallel bullet stacks; snake only at 6+ steps.",
        "phased roadmap": "lettered phase columns in a deepening ramp under one arrow band, duration ribbons "
                          "under spans; or one straight rising arrow with 2-3 callouts.",
        "gantt chart": "bars (divs) on shaded month columns, activities in a left rail grouped by theme, bars "
                       "ending in labelled milestone diamonds; dashed outline = tentative.",
        "milestone timeline": "dated nodes on one horizontal axis line, identically structured cards "
                              "alternating above/below; the so-what in a separate sidebar.",
        "swimlane diagram": "horizontal role lanes labelled in a left gutter, one card per step, straight "
                            "colour-coded arrows by flow type, compact legend.",
        "customer journey map": "stage columns with the same vertical rhythm (stage chip, icon, action, "
                                "two-line description) and a pain-point or gap row beneath.",
        "funnel": "narrowing trapezoids (clip-path polygon) with counts inside, filter criteria labelled per "
                  "stage; one colour family.",
        "flywheel": "4-5 nodes in a closed loop around a named hub, straight arrow segments between nodes, "
                    "short bullet clusters outside.",
        "convergence flow": "several input chevrons converging into one central statement, then 2-3 impact "
                            "tiles; reuse the inputs' established colours.",
        "decision flowchart": "yes/no diamonds leading to outcome boxes with straight elbow connectors; only "
                              "the chosen path in solid fill.",
        "many-to-one grouping map": "numbered source rows spanning into labelled group boxes, a rationale "
                                    "column on the right; one chevron, no arrows.",
        "value chain coverage map": "chevron stage axis with actor lanes whose bars span exactly the stages "
                                    "each actor covers; two tones + legend; one dashed accent box marks the gap.",
    },
    "Structures and hierarchies": {
        "pyramid": "stacked trapezoid tiers (base = primary conditions), a numbered test question per tier, a "
                   "side rail of examples.",
        "layered architecture": "three horizontal bands, each with 4-5 icon+label tiles (1-3 words); the "
                                "bands do the grouping.",
        "hub-and-spoke": "central hub circle with symmetric spokes (straight lines) to 4-8 nodes, each spoke "
                         "labelled with what flows.",
        "Porter's five forces": "central rivalry box, four force boxes at the compass points with inward "
                                "arrows, 2-3 bullets each; accent the dominant force.",
        "TAM/SAM/SOM concentric circles": "three nested circles with the value inside each ring and one-line "
                                          "definitions to the right; accent only SOM.",
        "org chart": "hierarchy boxes coloured by function, straight elbow connectors, a left level rail, "
                     "numbered callouts pinned to the zones they affect.",
        "three-tier governance stack": "three tier bands keyed by a numbered left rail, entity boxes, every "
                                       "arrow labelled with the verb of what moves (funds, reports).",
        "section navigator": "the deck's framework map with every tile grey except the current section (accent "
                             "dashed outline + 'Focus of this section' tag).",
        "stage deep-dive breadcrumb": "the parent process strip on top with non-focal stages grey, then a "
                                      "fixed detail layout for the lit stage.",
        "taxonomy grid": "category columns each owning one tint, header bands over uniform chips; density is "
                         "tamed by structure, never by connectors.",
    },
    "Tables and scorecards": {
        "options evaluation matrix": "parallel option columns on one row spine (description, + / - rows, "
                                     "Low/Med/High band); the winner gets a 'Recommended' tag, accent border "
                                     "and a verdict strip.",
        "weighted scoring matrix": "criteria rows with weight chips vs option columns scored in Harvey balls, "
                                   "a total row and a rank badge; the verdict rests on visible arithmetic.",
        "single-option criteria scorecard": "fixed per-option page: mandate band, criteria boxes with Harvey "
                                            "balls + evidence, a ranking badge; repeated identically per option.",
        "gated evaluation scorecard": "three numbered gate columns (question vs assessment), a right "
                                      "recommendation rail ending in an SVG check or cross.",
        "checkmark presence matrix": "SVG check cells under entity columns, rows in labelled bands, the "
                                     "subject's column outlined in accent.",
        "benchmark comparison table": "one row per peer on a fixed attribute spine, the subject's row outlined "
                                      "in accent, an average row, a dark lessons band at the bottom.",
        "attribute comparison table": "dimension rows in a left rail vs 2-3 entity columns, bold keywords do "
                                      "the emphasis; a synthesis banner closes it.",
        "KPI inventory table": "grouped category rows with indicator / measure / unit / target columns; "
                               "targets as coloured chips.",
        "OKR cascade table": "nested ID chips (1.1, 1.1.1), goal bands, KR / target / baseline columns, an "
                             "accent commitment banner.",
        "mapping table with tag chips": "domain spine, coloured tag chips, goal text, a Lead/Support verdict "
                                        "column; a footer legend decodes chips.",
        "responsibility assignment table": "one decision per row with numbered ID chips and a narrow Owner "
                                           "column (or R/A/C/I cells); nothing else competing.",
        "numbered initiative list table": "two columns (numbered ID chip + name, two-line description) under "
                                          "one dark header band.",
        "audience interaction-intensity matrix": "audience rows vs value-proposition columns resolved by one "
                                                 "shaded intensity column with a 3-step legend.",
        "master-detail catalog with category nav": "left category nav (active item highlighted, rest grey) "
                                                   "beside a uniform tile grid; a (1/3) series counter.",
    },
    "Cards, panels and lists": {
        "KPI tiles": "uniform tiles each led by one oversized number (61.5%, USD 73 bn) with a one-line caption; "
                     "the stat before any prose.",
        "icon card grid": "one equal card per item (2-6): dark header bar, an SVG icon only if it carries "
                          "meaning, a short paragraph with one bold phrase; one full-width takeaway banner.",
        "mirrored two-panel comparison": "two panels in contrasting theme tones with strictly parallel "
                                         "structure; the synthesis in one bottom banner.",
        "icon-labeled definition rows": "4-6 rows: a solid label cell with icon or letter badge on the left, "
                                        "bold-lead description on the right.",
        "numbered lever stack": "full-width numbered bands (01-07), each one bold lever name + one sentence; "
                                "targets panel on the right edge.",
        "additive criteria equation": "3-4 lettered criteria cards joined by plus badges into one accent "
                                      "outcome band - the argument reads as arithmetic.",
        "initiative charter card": "metadata strip (owner, budget, start, duration) over a 3-column grid of "
                                   "labelled field tiles, 2-4 dash bullets each.",
        "role charter panel": "entity band, purpose/responsibility bullets left, a two-column grid of member "
                              "tiles right; fixed geometry across the series.",
        "best-practices-to-recommendations panels": "best-practices panel -> chevron -> numbered "
                                                    "recommendations panel, each verb-first and bold-led.",
        "component-to-lessons synthesis grid": "three columns: numbered components -> lettered "
                                               "sub-components (1a-3b) -> lessons with bold accent lead-ins.",
        "segment value-proposition profile": "value-proposition rows left, example stakeholders middle, a "
                                             "vertical low/medium/high gauge right.",
        "left identity panel + numbered list profile": "dark identity panel in the left third, numbered chip "
                                                       "lists in two labelled tiers on the right.",
        "stakeholder aspiration mapping": "who was heard | what they said (aspiration cards) | where it lands "
                                          "(option columns with SVG checks).",
        "logo rows with color-coded keyword highlighting": "one entity per row with its statement, theme "
                                                           "keywords highlighted in legend colours.",
    },
    "Data exhibits (native data-chart unless noted)": {
        "waterfall chart": "`waterfall` with totals marked; labels on every bar, callouts only on the bars "
                           "that matter, one-line takeaway beneath.",
        "100% stacked bar chart": "`column_stacked_100`/`bar_stacked_100`, value labels on segments, one "
                                  "legend; the subject bar tinted or outlined.",
        "source-document exhibit with extraction grid": "the real artifact (asset image only) as an exhibit "
                                                        "panel left, decomposed into numbered component cards "
                                                        "right.",
        "annotated geographic map with callouts": "map image from the assets only, numbered pins + "
                                                  "identically structured callout cards around it.",
    },
}

#: The extra exhibits consultants and bankers expect that Slide-Creator's image model never needed: each
#: says which native chart or table it is, so the model does not hand-draw them.
FINANCE_EXHIBITS: dict[str, str] = {
    "football field": "`bar_stacked` horizontal: series 1 = low end in the background colour (invisible), "
                      "series 2 = high - low in primary; low/high values as text at the bar ends; the offer or "
                      "current price as a `referenceLines` entry; one bar per methodology, sorted.",
    "trading / transaction comps": "`<table>`: company rows, metric columns (EV/Revenue, EV/EBITDA, P/E, "
                                   "growth, margin) with the unit in the header; numbers right-aligned, same "
                                   "decimals per column, multiples as 12.5x; bold header over a 1px rule, thin "
                                   "row rules, no vertical lines; Median and Mean rows in bold under a rule; the "
                                   "target row tinted.",
    "bridge (EBITDA, revenue, valuation)": "`waterfall` with the start and end bars as totals, increases in "
                                           "primary/positive tone, decreases in grey or red, labels in "
                                           "parentheses for negatives.",
    "sources & uses / cap table": "two side-by-side tables with bold total rows and a % column; totals must "
                                  "tie.",
    "sensitivity table": "grid of the output for two input ranges, the base case cell in accent, row and "
                         "column input values in bold headers.",
    "trend with CAGR": "`column` or `line` in grey with the latest or forecast bars in primary, a straight "
                       "SVG arrow between the endpoints labelled 'CAGR +x%'.",
    "market share / mix": "`bar` sorted descending (prefer to pie beyond 4 slices), the client bar in accent "
                          "via `pointColors`, others grey.",
}


def _lines(entries: dict[str, str]) -> str:
    return "\n".join(f"- {name}: {hint}" for name, hint in entries.items())


def render() -> str:
    """The Markdown block the system prompt embeds (single braces; the caller escapes for `str.format`).

    Slide types in full; frameworks and finance exhibits by NAME only. Their recipes are delivered on demand
    by `plan_exhibit` (`design_refs.exhibit_plan`), which moved about 2.5k tokens out of every turn's prompt
    (`docs/DESIGN-DEVICES-RESEARCH.md` §3.2)."""
    groups = "\n".join(f"- {group}: " + ", ".join(entries) for group, entries in FRAMEWORKS.items())
    return (
        "## Slide types\n"
        "Decide the type first; its framework is the main exhibit the composition rules build around.\n"
        f"{_lines(SLIDE_TYPES)}\n\n"
        "## Framework library (recipes via `plan_exhibit`)\n"
        "Use one of these by preference; map an unusual ask to its nearest entry (a radar becomes a scoring "
        "matrix, a mind map a hub-and-spoke, anything 3D its flat equivalent; bubble charts are SVG circles).\n"
        f"{groups}\n"
        "- Finance and banking: " + ", ".join(FINANCE_EXHIBITS)
    )


#: Pre-rendered once at import: the prompt is a cached constant, so it must be byte-identical per process.
VOCABULARY = render()

#: Observed variant spelling -> canonical `FRAMEWORKS` name (matched after `normalize_framework_name`).
#: Ported verbatim from Slide-Creator `frameworks.ts: ALIASES`.
ALIASES: dict[str, str] = {
    # Human shorthands (spec-enumerated) — the calibration aliases below are observed long-forms.
    '2x2': '2x2 matrix',
    'two-by-two': '2x2 matrix',
    'two-by-two matrix': '2x2 matrix',
    'tam sam som': 'TAM/SAM/SOM concentric circles',
    'tam sam som concentric circles': 'TAM/SAM/SOM concentric circles',
    'swot': 'SWOT analysis',
    'gantt': 'gantt chart',
    '2x2 matrix (prioritization scatter with numbered dots)': '2x2 matrix',
    'two-axis positioning map': '2x2 matrix',
    'option typology grid (focus x breadth archetypes)': '2x2 matrix',
    'typology grid (knowledge/economic driver x specialized/generalist)': '2x2 matrix',
    'strengths-weaknesses-opportunities-threats grid': 'SWOT analysis',
    'five forces': "Porter's five forces",
    'growth-share matrix': 'BCG growth-share matrix',
    'star/cash-cow matrix': 'BCG growth-share matrix',
    'horizontal funnel': 'funnel',
    'filter funnel (long list to shortlisted gate)': 'funnel',
    'circular cycle diagram': 'flywheel',
    'circular cycle diagram (plan-monitor-review-act)': 'flywheel',
    'segmented cycle (wheel) diagram': 'flywheel',
    'circular three-element swirl': 'flywheel',
    'tiered criteria pyramid (primary vs additional conditions)': 'pyramid',
    'value chain (chevron)': 'value chain',
    'value chain (five lifecycle streams as pillar cards)': 'value chain',
    'three-stage value chain with example cards': 'value chain',
    'value chain with outputs swimlane': 'value chain',
    'value chain with mapped output artifacts': 'value chain',
    'value chain with item-mapping matrix': 'value chain',
    'five-stage value chain with mapped example chips': 'value chain',
    'swimlane diagram (role lanes: approver/overseer/participants)': 'swimlane diagram',
    'phased swimlane': 'swimlane diagram',
    'phase-mapped swimlane matrix': 'swimlane diagram',
    'phased roadmap (ascending arrow on impact-vs-time axes)': 'phased roadmap',
    'phased roadmap (horizontal step flow with duration ribbons)': 'phased roadmap',
    'impact-vs-time growth curve with phase callouts': 'phased roadmap',
    'bridge chart': 'waterfall chart',
    'revenue/OPEX decomposition bar': 'waterfall chart',
    'three-layer band taxonomy': 'layered architecture',
    'three-band strategy framework grid': 'layered architecture',
    'layered architecture (3-tier stack as level/components/description rows)': 'layered architecture',
    'overlapping circles': 'Venn diagram',
    'customer journey map (talent lifecycle with gap dots)': 'customer journey map',
    'five-stage talent lifecycle pipeline': 'customer journey map',
    'lifecycle stage model with metrics strip': 'customer journey map',
    'heatmap with verdict column': 'heatmap',
    'criteria heatmap (importance intensity squares)': 'heatmap',
    'tiered taxonomy heatmap': 'heatmap',
    'service-by-partnership-type matrix with involvement heatmap': 'heatmap',
    'graded dot importance matrix': 'heatmap',
    'criteria rating matrix (dot-intensity)': 'heatmap',
    'KPI-to-goal impact mapping matrix (dot grid)': 'heatmap',
    'skills-importance matrix with graded dot legend': 'heatmap',
    'scatter with sized bubbles': 'bubble chart',
    'initiative timeline bars': 'gantt chart',
    'activity schedule': 'gantt chart',
    'market sizing circles': 'TAM/SAM/SOM concentric circles',
    'nested market circles': 'TAM/SAM/SOM concentric circles',
    'hub-and-spoke (center with labeled bidirectional flows)': 'hub-and-spoke',
    'hub-and-spoke objectives wheel + program tile grid': 'hub-and-spoke',
    'radial ecosystem wheel': 'hub-and-spoke',
    'quadrant ring around central hub': 'hub-and-spoke',
    'stat callout tiles': 'KPI tiles',
    'parallel stat-callout evidence panels': 'KPI tiles',
    'headline stat counters': 'KPI tiles',
    'twin evidence panels with impact stat blocks': 'KPI tiles',
    'org chart with level axis': 'org chart',
    'org chart with numbered decision callouts': 'org chart',
    'side-by-side org charts': 'org chart',
    'paired org charts isolating one structural difference': 'org chart',
    'org chart (department hierarchy in color-coded column groups)': 'org chart',
    'org chart (board hub + department tile grid)': 'org chart',
    'strategy module navigator grid': 'section navigator',
    'strategy blueprint grid': 'section navigator',
    'strategy house map with section highlight': 'section navigator',
    'layered module map (strategy-house navigator)': 'section navigator',
    'assessment framework map (active cell highlighted)': 'section navigator',
    'framework navigator with active-cell highlight': 'section navigator',
    'phase-by-component framework grid with active-cell highlight': 'section navigator',
    'three-tier business model map with focus-of-this-section tag': 'section navigator',
    'three-panel document tracker': 'section navigator',
    'module map with dashed focus highlight': 'section navigator',
    'section navigator (framework map with focus highlight)': 'section navigator',
    'initiative charter card grid': 'initiative charter card',
    'labeled field grid': 'initiative charter card',
    'icon-tagged charter grid': 'initiative charter card',
    'initiative overview card': 'initiative charter card',
    'option evaluation matrix with pros/cons rows and verdict strip': 'options evaluation matrix',
    'option comparison columns with recommendation flag': 'options evaluation matrix',
    'pros/cons matrix with recommendation highlight': 'options evaluation matrix',
    'three-option archetype comparison columns': 'options evaluation matrix',
    'option-scoring matrix against named design principles': 'options evaluation matrix',
    'multi-option comparison matrix with example-logo rows': 'options evaluation matrix',
    'lettered option column array with example logos': 'options evaluation matrix',
    'three-option model columns with mini org diagrams': 'options evaluation matrix',
    'value chain coverage map (intensity arrows over chevron phases)': 'value chain coverage map',
    'value chain coverage bars (gantt-style service spans)': 'value chain coverage map',
    'value chain coverage curves (overlapping area bands)': 'value chain coverage map',
    'value chain with focus-intensity gradient': 'value chain coverage map',
    'value chain with maturity-tier focus spans': 'value chain coverage map',
    'value-chain coverage arrows (gap analysis)': 'value chain coverage map',
    'swimlane coverage map over chevron stage axis': 'value chain coverage map',
    'ecosystem actor map plotted on value chain': 'value chain coverage map',
    'value chain coverage with valley-of-death dot flow': 'value chain coverage map',
    'lifecycle coverage gantt': 'value chain coverage map',
    'weighted scoring matrix with harvey balls': 'weighted scoring matrix',
    'options-by-criteria scoring matrix with harvey balls': 'weighted scoring matrix',
    'weighted scoring table': 'weighted scoring matrix',
    'single-criterion harvey-ball scoring table': 'weighted scoring matrix',
    'weighted criteria matrix (criteria, weight chips, key questions)': 'weighted scoring matrix',
    'scoring rubric matrix (criteria x anchored scale)': 'weighted scoring matrix',
    'prioritization scorecard table': 'weighted scoring matrix',
    'criteria scoring matrix': 'weighted scoring matrix',
    'benchmark checkmark matrix': 'checkmark presence matrix',
    'checkmark matrix': 'checkmark presence matrix',
    'checkmark feature matrix': 'checkmark presence matrix',
    'function-presence matrix': 'checkmark presence matrix',
    'role-by-country checkmark matrix': 'checkmark presence matrix',
    'benchmark coverage matrix (checkmark heatmap)': 'checkmark presence matrix',
    'facility-to-service coverage matrix': 'checkmark presence matrix',
    'capability checklist matrix': 'checkmark presence matrix',
    'OKR cascade table (goals/objectives/key results)': 'OKR cascade table',
    'OKR key-results definition matrix (KR | contribution | target rationale | baseline)': 'OKR cascade table',
    'goals-objectives-key results cascade table': 'OKR cascade table',
    'domain-to-goals mapping table (icon chips)': 'mapping table with tag chips',
    'mapping matrix with color-coded tag chips': 'mapping table with tag chips',
    'domain-to-goal mapping table': 'mapping table with tag chips',
    'five-pillar icon cards': 'icon card grid',
    'four-pillar columns with icon headers': 'icon card grid',
    'icon tile grid': 'icon card grid',
    'design-principles panel grid': 'icon card grid',
    'guiding-principles grid (2x3)': 'icon card grid',
    'five-pillar value cards with photo backgrounds': 'icon card grid',
    'three image-capped panel cards': 'icon card grid',
    'banded card grid': 'icon card grid',
    'versus split panel': 'mirrored two-panel comparison',
    'two-panel color split with icon bubbles': 'mirrored two-panel comparison',
    'side-by-side mandate panels': 'mirrored two-panel comparison',
    'two-entity role split diagram': 'mirrored two-panel comparison',
    'entity split branch diagram': 'mirrored two-panel comparison',
    'two-panel option clusters': 'mirrored two-panel comparison',
    'interim vs target-state comparison': 'mirrored two-panel comparison',
    'side-by-side case panels with KPI footer strips': 'mirrored two-panel comparison',
    'two-column case comparison': 'mirrored two-panel comparison',
    'narrow-to-wide continuum with archetype cards': 'archetype spectrum (continuum)',
    'binding-spectrum ordered columns': 'archetype spectrum (continuum)',
    'centralization spectrum with mini org charts': 'archetype spectrum (continuum)',
    'continuum scale with categorical bins': 'archetype spectrum (continuum)',
    'option cards ordered along effort-spectrum arrow': 'archetype spectrum (continuum)',
    'option spectrum with mini org-chart schematics': 'archetype spectrum (continuum)',
    'three-tier scope spectrum with pictograms': 'archetype spectrum (continuum)',
    'framework-locator rail + best practices to lessons/recommendations two-column flow': 'best-practices-to-recommendations panels',
    'lessons-and-recommendations mapping panels': 'best-practices-to-recommendations panels',
    'categorized taxonomy grid': 'taxonomy grid',
    'four-pillar taxonomy grid': 'taxonomy grid',
    'tiered taxonomy grid': 'taxonomy grid',
    'grouped dimension taxonomy': 'taxonomy grid',
    'six-dimension taxonomy columns': 'taxonomy grid',
    'two-level component taxonomy grid': 'taxonomy grid',
    'three-pillar service taxonomy with sub-service chips': 'taxonomy grid',
    'typology matrix (archetypes x attribute rows)': 'taxonomy grid',
    'country benchmark matrix': 'benchmark comparison table',
    'benchmark composition table': 'benchmark comparison table',
    'entity profile comparison matrix': 'benchmark comparison table',
    'peer benchmarking table with thematic icon tagging': 'benchmark comparison table',
    'benchmark data table with design-guideline takeaway panel': 'benchmark comparison table',
    'benchmark comparison grid': 'benchmark comparison table',
    'two-column benchmark profile grid': 'benchmark comparison table',
    'attribute comparison matrix': 'attribute comparison table',
    'attribute-by-attribute comparison table': 'attribute comparison table',
    'two-entity attribute comparison table': 'attribute comparison table',
    'two-column attribute comparison matrix with heat-shaded intensity bars': 'attribute comparison table',
    'summary attribute matrix': 'attribute comparison table',
    'two-option attribute matrix': 'attribute comparison table',
    'three-panel segment profile (value-prop list, stakeholder logo wall, interaction gauge)': 'segment value-proposition profile',
    'segment value-proposition profile with interaction gauge': 'segment value-proposition profile',
    'icon-labeled dimension definition rows': 'icon-labeled definition rows',
    'icon-led stacked bands': 'icon-labeled definition rows',
    'icon-keyed stakeholder definition table': 'icon-labeled definition rows',
    'categorized definition table (type-description-example)': 'icon-labeled definition rows',
    'stakeholder taxonomy list (lettered chips)': 'icon-labeled definition rows',
    'KPI inventory table (category / KPI / measure / unit)': 'KPI inventory table',
    'KPI definition table': 'KPI inventory table',
    'KPI inventory matrix (streams x indicators)': 'KPI inventory table',
    'metric definition table with formula column and target pills': 'KPI inventory table',
    'benchmark KPI comparison table': 'KPI inventory table',
    'value-chain focus map + criteria scorecard strip': 'single-option criteria scorecard',
    'value chain + Harvey-ball criteria scorecard': 'single-option criteria scorecard',
    'criteria scorecard': 'single-option criteria scorecard',
    '4-step numbered process columns': 'numbered step process',
    '3-step numbered columns': 'numbered step process',
    'numbered step sequence with chevron connectors': 'numbered step process',
    'stepped process boxes': 'numbered step process',
    'serpentine process flow': 'numbered step process',
    'vertical numbered step arrow': 'numbered step process',
    'three-step numbered process with slide-thumbnail previews': 'numbered step process',
    'annotated geographic map with hub callouts': 'annotated geographic map with callouts',
    'geographic map with radiating callout cards': 'annotated geographic map with callouts',
    'map with hub-to-spoke expansion arrows': 'annotated geographic map with callouts',
    'dual annotated maps with numbered cluster callouts': 'annotated geographic map with callouts',
    'world map with regional ranking tables': 'annotated geographic map with callouts',
    'annotated site/aerial map with checklist legend': 'annotated geographic map with callouts',
    '100% stacked share-shift chart': '100% stacked bar chart',
    'paired 100% stacked bar charts': '100% stacked bar chart',
    'small-multiple 100% stacked bars with annotation callouts': '100% stacked bar chart',
    'parallel stacked bar charts (multi-entity benchmark)': '100% stacked bar chart',
    '100% stacked split bar with annotation panels': '100% stacked bar chart',
    'logo-quote benchmark rows with color-coded themes': 'logo rows with color-coded keyword highlighting',
    'tagged statement list (color-coded keyword highlighting)': 'logo rows with color-coded keyword highlighting',
    'color-coded keyword taxonomy over logo rows': 'logo rows with color-coded keyword highlighting',
    'color-coded naming decomposition with legend sidebar': 'logo rows with color-coded keyword highlighting',
    'branding comparison table with keyword tagging': 'logo rows with color-coded keyword highlighting',
    'tiered governance flow diagram': 'three-tier governance stack',
    'layered governance chart with priority/funding/execution bands': 'three-tier governance stack',
    'three-tier governance stack with escalation arrows': 'three-tier governance stack',
    'governance hierarchy (authority cascade)': 'three-tier governance stack',
    'three-layer governance stack with benchmark org charts': 'three-tier governance stack',
    'component-lessons matrix (component / sub-component / lessons learned)': 'component-to-lessons synthesis grid',
    'three-column mapping grid (component to sub-component to lessons)': 'component-to-lessons synthesis grid',
    'dimension-lessons synthesis table': 'component-to-lessons synthesis grid',
    'stage spotlight breadcrumb with example-card trio': 'stage deep-dive breadcrumb',
    'value-chain breadcrumb + two-panel detail (overview vs example)': 'stage deep-dive breadcrumb',
    'drill-down chevron flow with steps/description swimlane': 'stage deep-dive breadcrumb',
    'chevron process with stage deep-dive grid': 'stage deep-dive breadcrumb',
    'numbered chevron process': 'chevron process flow',
    'five-step chevron process': 'chevron process flow',
    'chevron process flow (8 steps)': 'chevron process flow',
    '3-step chevron process with deliverables under each step': 'chevron process flow',
    'six-step numbered chevron process grouped into phases': 'chevron process flow',
    'source artifact + numbered extraction list': 'source-document exhibit with extraction grid',
    'numbered mandate grid with source-document exhibit': 'source-document exhibit with extraction grid',
    'annotated form anatomy with checklist': 'source-document exhibit with extraction grid',
    'numbered component anatomy with expanded checklist': 'source-document exhibit with extraction grid',
    'labeled anatomy breakdown of screenshot exhibit': 'source-document exhibit with extraction grid',
    'gap-analysis matrix (lessons vs opportunities) with RAG dot coding': 'RAG status matrix',
    'layered framework with traffic-light status overlay': 'RAG status matrix',
    '4-pillar framework with traffic-light maturity dots': 'RAG status matrix',
    'role charter panels': 'role charter panel',
    'committee description matrix': 'role charter panel',
    'governance roles-and-responsibilities grid': 'role charter panel',
    'stakeholder interaction matrix with intensity heat column': 'audience interaction-intensity matrix',
    'audience-value matrix with intensity dots': 'audience interaction-intensity matrix',
    'value-proposition matrix with interaction-intensity bars': 'audience interaction-intensity matrix',
    'responsibility mapping table': 'responsibility assignment table',
    'many-to-one owner mapping': 'responsibility assignment table',
    'gated evaluation scorecard (criteria-assessment-recommendation)': 'gated evaluation scorecard',
    'master-detail catalog grid with left category nav': 'master-detail catalog with category nav',
    'tabbed category nav with priced tile grid': 'master-detail catalog with category nav',
    'tabbed matrix table': 'master-detail catalog with category nav',
    'tiered portfolio coverage matrix': 'master-detail catalog with category nav',
    'two-column initiative-description table': 'numbered initiative list table',
    'two-panel color-coded numbered initiative master list': 'numbered initiative list table',
    'dimmed-context dual panel': 'numbered initiative list table',
    'initiative-description table': 'numbered initiative list table',
    'chevron-header coverage heatmap': 'value chain heatmap',
    'left identity panel + numbered opportunity list': 'left identity panel + numbered list profile',
    'icon-labeled attribute row stack with mission panel': 'left identity panel + numbered list profile',
    'headshot roster plus aspiration-to-mandate mapping table': 'stakeholder aspiration mapping',
    'photo roster + aspiration cards + option-mapping columns': 'stakeholder aspiration mapping',
    'stakeholder aspiration-to-option mapping table': 'stakeholder aspiration mapping',
    'workshop-artifact collage + aspiration cards': 'stakeholder aspiration mapping',
    'numbered lever stack with benchmark logo column': 'numbered lever stack',
    'numbered criteria stack': 'numbered lever stack',
    'grouped numbered lever list': 'numbered lever stack',
    'numbered guiding-principles list with targets panel': 'numbered lever stack',
    'numbered icon list (gear chain)': 'numbered lever stack',
    'additive criteria equation (A+B+C+D to outcome band)': 'additive criteria equation',
    'additive criteria formula (lettered cards joined by plus badges)': 'additive criteria equation',
    'A-plus-B-arrow-C summary equation columns': 'additive criteria equation',
    'milestone timeline with document cards': 'milestone timeline',
    'milestone timeline with alternating callouts': 'milestone timeline',
    'three-milestone chevron timeline': 'milestone timeline',
    'phased evolution timeline with dated milestones': 'milestone timeline',
    'many-to-one mapping diagram (functions to departments)': 'many-to-one grouping map',
    'grouping cascade (functions to departments to rationale)': 'many-to-one grouping map',
    'many-to-one mapping table': 'many-to-one grouping map',
    'mapping grid (existing units to recommended departments)': 'many-to-one grouping map',
    'convergence flow (segment chevrons to central hub to impact tiles)': 'convergence flow',
    'converging arrow streams with implication callout': 'convergence flow',
    'input convergence flow to target outcome': 'convergence flow',
    'two-path convergent flow': 'convergence flow',
    'decision flowchart with method definition panel': 'decision flowchart',
    'two-branch decision tree with VS pairs': 'decision flowchart',
    'decision tree on insource-outsource spectrum': 'decision flowchart',
    'criteria-to-decision flow': 'decision flowchart',
}


def normalize_framework_name(name: str) -> str:
    """Lowercase, drop apostrophes, fold x-signs, collapse punctuation: the Slide-Creator normaliser."""
    name = name.lower().replace("'", "").replace("’", "").replace("×", "x")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/+]+", " ", name)).strip()


_CANONICAL = {name for entries in FRAMEWORKS.values() for name in entries}
_LOOKUP = {normalize_framework_name(n): n for n in _CANONICAL}
_LOOKUP.update({normalize_framework_name(a): c for a, c in ALIASES.items()})


def canonical_framework(name: str) -> str | None:
    """The canonical framework for a name, alias or variant spelling, or None when it is unknown."""
    return _LOOKUP.get(normalize_framework_name(name))


def framework_hint(name: str) -> str | None:
    """The build hint for a framework name or alias (for tools that want one entry, not the whole library)."""
    canonical = canonical_framework(name)
    if canonical is None:
        return None
    return next(entries[canonical] for entries in FRAMEWORKS.values() if canonical in entries)
