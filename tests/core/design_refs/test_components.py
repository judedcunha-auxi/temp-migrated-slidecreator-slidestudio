"""`design_refs.components`: every component lints clean, draws no AI look, and exports as native PowerPoint.

* every component, at its defaults and at the edges of its counts, passes the engine's authoring-contract
  linter with no error and no warning, on two synthetic manifests (the design-refs test master in
  `conftest.py` and the Office-default test master under `tests/engine/fixtures/masters`);
* a representative set exports through the engine as native shapes, tables and charts (no picture);
* a minimal real call draws none of the component's showcase (demo) content.

Ported from Slide Studio `server/tests/test_components.py`; the client master it also used is replaced by
the synthetic one, and the export runs through `app.engine.pipeline.export_deck` directly.
"""
from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from app.core.design_refs import components as C
from app.core.design_refs import notes as N
from tests.core.design_refs.conftest import manifest as synthetic_manifest

FIXTURES = Path(__file__).resolve().parents[2] / "engine" / "fixtures"
MANIFESTS: dict[str, Path | None] = {
    "synthetic": None,
    "office": FIXTURES / "masters" / "test-16x9.manifest.json",
}


def _manifest(kind: str) -> dict[str, Any]:
    path = MANIFESTS[kind]
    return synthetic_manifest() if path is None else json.loads(path.read_text(encoding="utf-8"))


def _lint(html: str, manifest: dict[str, Any]) -> list[str]:
    from app.engine import manifest as engine_manifest
    from app.engine.verify import lint

    report = lint.lint(html, engine_manifest.Manifest.from_json(manifest))
    return [f"{f.level}:{f.rule}: {f.message}" for f in report.findings if f.level in ("error", "warn")]


def test_the_token_grid_lands_on_whole_pixels():
    for kind in MANIFESTS:
        tokens = N.design_tokens(_manifest(kind))
        px = {k: float(v[:-2]) for k, v in tokens.items() if v.endswith("px") and not k.startswith("fs-")}
        assert all(v == int(v) for v in px.values()), kind
        assert px["e12"] == px["m-right"] and px["c1"] == px["m-left"]
        assert all(px[f"c{i + 1}"] - px[f"e{i}"] == px["gutter"] for i in range(1, 12))
        assert 16 <= px["gutter"] <= 24
    synthetic = N.design_tokens(_manifest("synthetic"))
    # the title placeholder is 22 pt; the primary is the first usable accent; the accent the next distinct hue
    assert synthetic["fs-title"] == "29.33px" and synthetic["primary"] == "#2F80ED"
    assert synthetic["accent"] == "#8E44AD"


EXPORTED = ["chevron_process", "value_chain", "harvey_table", "matrix_2x2", "comps_table", "sensitivity_table",
            "column_chart", "waterfall", "football_field", "share_bar", "line_chart", "gantt",
            "keyed_markers", "delta_badge", "bar_table", "bullet_rows"]


def _project(root: Path, manifest: dict[str, Any], slides: list[tuple[str, str]]) -> Path:
    """A project directory as `export_deck` reads it: project.json, manifest.json, the master, layouts, slides."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    shutil.copy2(FIXTURES / "masters" / "test-16x9.pptx", root / "master.pptx")
    shutil.copytree(FIXTURES / "masters" / "test-16x9" / "layouts", root / "layouts", dirs_exist_ok=True)
    (root / "assets").mkdir(exist_ok=True)
    records = []
    for index, (name, html) in enumerate(slides, start=1):
        sid = f"sld_{index:010d}"
        (root / "slides" / sid).mkdir(parents=True)
        (root / "slides" / sid / "v001.html").write_text(html, encoding="utf-8")
        records.append({"id": sid, "title": name, "layoutId": "layout-06", "current": 1})
    (root / "project.json").write_text(json.dumps({"id": "components", "name": "Components export",
                                                   "slides": records}), encoding="utf-8")
    return root


def test_a_representative_set_exports_as_native_powerpoint(tmp_path):
    """The engine end to end on the Office-default test master (free, no model): every exhibit is native
    shapes, tables and charts, no picture anywhere, and each chart is a real chart part."""
    pytest.importorskip("playwright")
    from app.engine.pipeline import export_deck
    from app.engine.reports import ExportOptions

    manifest = _manifest("office")
    project = _project(tmp_path / "project", manifest,
                       [(name, _component_slide(manifest, name)) for name in EXPORTED])
    try:
        result = export_deck(project, ExportOptions(out_dir=tmp_path / "out", workspace=tmp_path / "work"))
    except Exception as exc:  # noqa: BLE001
        if re.search(r"browser|playwright|edge|chromium", str(exc), re.I):
            pytest.skip(f"no measuring browser on this machine: {exc}")
        raise
    with zipfile.ZipFile(result.pptx) as archive:
        slides = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        assert len(slides) == len(EXPORTED)
        for name, part in zip(EXPORTED, slides, strict=True):
            xml = archive.read(part).decode("utf-8")
            assert "<p:pic>" not in xml and "<p:pic " not in xml, f"{name} was rasterised"
            charts = xml.count("<c:chart ")
            want = 1 if name in ("column_chart", "waterfall", "football_field", "share_bar", "line_chart") else 0
            assert charts == want, f"{name}: {charts} charts"
            if name in ("comps_table", "sensitivity_table"):
                assert "<a:tbl>" in xml, f"{name} is not a native table"
            if name in ("chevron_process", "value_chain"):
                assert xml.count("<a:custGeom>") >= 4, f"{name} lost its chevrons"
            if name in ("delta_badge", "bar_table"):     # the triangles are native polygons, not pictures
                assert xml.count("<a:custGeom>") >= (1 if name == "delta_badge" else 5), f"{name} lost its triangles"
            if name in ("bar_table", "bullet_rows", "keyed_markers"):
                texts = re.findall(r"<a:t>([^<]*)</a:t>", xml)
                least = {"bar_table": 30, "bullet_rows": 25, "keyed_markers": 8}[name]
                assert "<a:tbl>" not in xml and xml.count("<p:sp>") >= least, f"{name}: not native shapes"
                want_text = {"bar_table": ["14.2", "11.8", "+2.4", "-4.0 pts"], "bullet_rows": ["272", "vs 300"],
                             "keyed_markers": ["1", "2", "3", "Procurement"]}[name]
                for t_ in want_text:
                    assert any(t_ in x for x in texts), f"{name}: {t_!r} is not live text"


def _box(tokens: dict, name: str) -> dict:
    """The box a slide would give the component: the title zone for the title, the body area otherwise."""
    px = lambda k: int(float(tokens[k][:-2]))  # noqa: E731
    if name == "action_title":
        return {"x": px("m-left"), "y": px("title-top"), "w": px("content-w"), "h": px("title-bottom") - px("title-top")}
    if name in ("source_note", "tracker", "sticker", "cagr_arrow", "legend", "delta_badge"):
        return {}
    return {"x": px("m-left"), "y": px("content-top") + 24, "w": px("content-w"),
            "h": min(px("content-bottom") - px("content-top") - 24, 170 if name in ("kpi_tiles", "callout") else 10_000)}


def _slide(manifest: dict, fragments: list[str]) -> str:
    tokens = N.design_tokens(manifest)
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>\n{N.token_css(manifest)}\n'
            f'html,body{{margin:0;width:{tokens["canvas-w"]};height:{tokens["canvas-h"]};overflow:hidden;'
            f'background:transparent}}\nbody{{font-family:var(--font-body),sans-serif;color:var(--ink)}}\n'
            f'</style></head><body>\n' + "\n".join(fragments) + "\n</body></html>")


def _component_slide(manifest: dict, name: str, **params) -> str:
    tokens = N.design_tokens(manifest)
    frags = [C.get(name, tokens=tokens, **{**_box(tokens, name), **params})]
    if name != "action_title":
        frags.insert(0, C.get("action_title", tokens=tokens, **_box(tokens, "action_title")))
    if name != "source_note":
        frags.append(C.get("source_note", tokens=tokens, y=int(float(tokens["source-top"][:-2]))))
    return _slide(manifest, frags)


def test_the_library_has_the_promised_breadth():
    names = C.names()
    assert 25 <= len(names) <= 40
    for must in ("action_title", "source_note", "tracker", "kpi_tiles", "chevron_process", "gantt", "matrix_2x2",
                 "harvey_table", "check_matrix", "rag_table", "comparison_columns", "pros_cons", "pyramid", "funnel",
                 "hub_spoke", "value_chain", "org_tiers", "timeline", "callout", "cagr_arrow", "column_chart",
                 "waterfall", "football_field", "comps_table", "sensitivity_table", "sources_uses", "share_bar",
                 "mekko", "keyed_markers", "delta_badge", "bar_table", "bullet_rows"):
        assert must in names, must


def test_the_catalog_is_compact_and_lists_every_component():
    catalog = C.catalog()
    assert len(catalog) / 4 <= 1200, f"{len(catalog)} chars ≈ {len(catalog) // 4} tokens"
    for name in C.names():
        assert f"- {name}(" in catalog
    assert "{" not in catalog and "}" not in catalog, "the catalog is embedded in a str.format template"


@pytest.mark.parametrize("kind", sorted(MANIFESTS))
@pytest.mark.parametrize("name", C.names())
def test_every_component_lints_clean_at_its_defaults(kind, name):
    manifest = _manifest(kind)
    html = _component_slide(manifest, name)
    assert _lint(html, manifest) == []
    assert "<script" not in html and "http" not in html.replace("http-equiv", "")


VARIANTS = [
    ("chevron_process", {"steps": [{"title": f"Step {i}", "bullets": ["One", "Two"]} for i in range(7)]}),
    ("chevron_process", {"steps": [{"title": "A", "bullets": []}, {"title": "B"}, {"title": "C"}], "ramp": False}),
    ("kpi_tiles", {"tiles": [{"value": "USD 12.4bn", "label": "Revenue"}, {"value": "9%", "label": "Growth",
                                                                           "delta": "(2 pts)"}], "highlight": 0}),
    ("numbered_list", {"items": ["Plain string item", {"title": "Lead", "text": "With **bold** number 12%"}]}),
    ("three_column", {"columns": [{"title": "One", "bullets": ["a"]}, {"title": "Two", "bullets": ["b"]}],
                      "arrows": False, "highlight": None}),
    ("pyramid", {"levels": [{"title": "Top", "text": "t"}, {"title": "Base", "text": "b"}]}),
    ("funnel", {"stages": [{"label": f"S{i}", "value": str(100 - 15 * i)} for i in range(6)]}),
    ("hub_spoke", {"center": "Hub", "nodes": [{"title": f"N{i}", "text": "x"} for i in range(3)]}),
    ("hub_spoke", {"center": "Hub", "nodes": [{"title": f"N{i}", "text": "x"} for i in range(8)]}),
    ("issue_tree", {"root": "Question", "branches": [{"title": "One", "leaves": ["a"]}, {"title": "Two", "leaves": ["b", "c", "d", "e"]}]}),
    ("org_tiers", {"tiers": [{"label": "Board", "boxes": ["Board"]}, {"label": "Execs", "boxes": ["A", "B", "C"]},
                             {"label": "Teams", "boxes": ["1", "2", "3", "4", "5"]}], "highlight": None}),
    ("gantt", {"periods": [f"M{i}" for i in range(1, 13)], "rows": [{"label": "Run", "start": 0, "end": 12,
                                                                     "milestone": 12, "note": "Go-live"}], "today": 3.5}),
    ("timeline", {"milestones": [{"date": "2026", "title": "A"}, {"date": "2027", "title": "B"}], "highlight": None}),
    ("matrix_2x2", {"dots": [], "target": ""}),
    ("harvey_table", {"criteria": ["Only"], "rows": [{"label": "X", "scores": [0]}, {"label": "Y", "scores": [4]}],
                      "legend": False}),
    ("check_matrix", {"columns": ["A", "B"], "rows": [{"label": "r", "marks": ["yes", "partial"]}],
                      "highlight_column": None}),
    ("rag_table", {"columns": ["Item", "Status"], "status_column": 1, "rows": [{"cells": ["x", "a"]}]}),
    ("comparison_columns", {"options": ["A", "B"], "row_labels": ["Cost"], "cells": [["Low", "High"]],
                            "recommended": None, "verdict": ""}),
    ("comps_table", {"columns": ["Company", "EV/EBITDA"], "rows": [{"cells": ["A", -3.2]}, {"cells": ["B", None]}],
                     "stats": ["Median", "Mean", "High", "Low"], "unit": ""}),
    ("sensitivity_table", {"row_values": ["8%", "9%"], "col_values": ["2%"], "values": [[1.0], [2.0]], "base": [0, 0],
                           "caption": ""}),
    ("column_chart", {"categories": ["P", "Q", "R", "S"], "values": [9, 7, 4, 2], "orientation": "bar",
                      "highlight": [0], "header": "Share, %"}),
    ("column_chart", {"categories": ["A", "B", "C"], "values": [3, -2, 5], "highlight": [2],
                      "cagr": {"from": 0, "to": 2, "label": "+29%"}}),
    ("waterfall", {"categories": ["Start", "Down", "End"], "values": [100, -40, 60], "totals": [0, 2]}),
    ("football_field", {"rows": [{"label": "DCF", "low": 10, "high": 12}], "reference": None, "highlight": None}),
    ("share_bar", {"orientation": "bar", "categories": ["2024", "2025", "2026", "2027"], "series": [{"name": "Us", "values": [50, 50, 50, 50]},
                                                    {"name": "Them", "values": [50, 50, 50, 50]}]}),
    ("line_chart", {"categories": [str(y) for y in range(2019, 2026)],
                    "series": [{"name": "Only", "values": [1, 2, 3, 4, 5, 6, 7]}], "highlight": 0}),
    ("mekko", {"segments": [{"name": "One", "size": 1, "parts": [{"label": "Us", "share": 100}]}]}),
    ("callout", {"style": "fill", "body": "Filled takeaway with **one** number: 12%"}),
    ("callout", {"style": "bar", "body": "Takeaway beside a separate accent block"}),
    ("legend", {"items": [{"label": "Hex colour", "color": "#123456"}, {"label": "Token", "color": "rag-g"}]}),
    ("keyed_markers", {"markers": [{"n": 1, "x": 300, "y": 200}, {"n": 2, "x": 900, "y": 260}],
                       "notes": [{"n": 1, "lead": "Lead", "text": "One"}, {"n": 2, "text": "Two"}], "side": "below"}),
    ("keyed_markers", {"markers": [{"n": "A", "x": 640, "y": 300}], "color": "accent"}),
    ("delta_badge", {"value": "-12%", "good_when": "down"}),
    ("delta_badge", {"value": "0.0 pts"}),
    ("delta_badge", {"value": "(1.2)", "direction": "up", "good_when": "down"}),
    ("bar_table", {"rows": [{"label": f"Metric {i}", "value": 10 + i, "benchmark": 12, "format": "%",
                             "lower_is_better": i % 2 == 0} for i in range(8)],
                   "subject": "Us", "benchmark": "Peers", "scale": "column", "gap": "pct", "highlight": "better"}),
    ("bar_table", {"rows": [{"label": "Only", "value": 0, "benchmark": 5, "gap": "n.m."}], "gap": "none",
                   "highlight": "none"}),
    ("bullet_rows", {"rows": [{"label": "No bands", "actual": 3, "target": 4},
                              {"label": "Five bands, lower is better", "actual": 9, "target": 6,
                               "bands": [2, 4, 6, 8, 10], "lower_is_better": True}],
                     "scale": "column", "flag_misses": False}),
]


@pytest.mark.parametrize("name,params", VARIANTS, ids=[f"{n}-{i}" for i, (n, _) in enumerate(VARIANTS)])
def test_variants_lint_clean(name, params):
    manifest = _manifest("synthetic")
    assert _lint(_component_slide(manifest, name, **params), manifest) == []


_STRIPE = re.compile(r"border-(left|right|top|bottom)\s*:\s*(\d+(?:\.\d+)?)px\s+solid\s+(?!var\(--bg)")


@pytest.mark.parametrize("name", C.names())
def test_no_component_draws_the_ai_look(name):
    """No side stripes, no rounded card with a shadow, no emoji — at the defaults and in every callout style."""
    fragments = [C.get(name)] + ([C.get("callout", style=s) for s in ("panel", "bar", "fill")] if name == "callout" else [])
    for html in fragments:
        for side, width in _STRIPE.findall(html):
            assert float(width) <= 2, f"{name}: a {width}px {side} border stripe"
        assert "box-shadow" not in html, f"{name}: a drop shadow"
        assert not re.search("[\U0001F000-\U0001FAFF✅✨⚡⭐]", html), f"{name}: emoji"


def test_the_callout_bar_is_a_separate_block_not_a_border():
    bar = C.get("callout", style="bar", body="Takeaway", w=600, h=80)
    assert "border-left" not in bar
    # the accent block (8 px) and the panel (from x 16) do not touch: a gap, not a flush stripe
    assert "left:0px;top:0px;width:8px;height:80px" in bar and "left:16px;top:0px;width:584px;height:80px" in bar
    assert "var(--accent" in bar
    assert "var(--accent" not in C.get("callout", body="Takeaway"), "the default callout is a plain panel"


def test_unknown_component_is_a_key_error():
    with pytest.raises(KeyError):
        C.get("sankey_of_dreams")


@pytest.mark.parametrize("name,params", [
    ("chevron_process", {"colour": "red"}),                               # unknown parameter
    ("chevron_process", {"steps": "Diagnose, Design"}),                   # wrong type
    ("chevron_process", {"steps": [{"title": "Only one"}]}),              # too few steps
    ("chevron_process", {"steps": [{"title": str(i)} for i in range(8)]}),  # too many
    ("harvey_table", {"rows": [{"label": "X", "scores": [1, 2]}]}),       # scores do not match criteria
    ("sources_uses", {"uses": [{"label": "Equity", "value": 1}]}),        # does not tie
    ("kpi_tiles", {"w": 0}),                                              # empty box
    ("kpi_tiles", {"x": "64px"}),                                         # box values are numbers
    ("rag_table", {"rows": [{"cells": ["x", "y", "Purple", "z"]}]}),      # status not R/A/G
    ("column_chart", {"highlight": True}),                                # bool is not a list
])
def test_bad_params_are_value_errors(name, params):
    with pytest.raises(ValueError):
        C.get(name, **params)


def test_tokens_drive_the_fallbacks_and_the_chart_colours():
    tokens = {"primary": "#123456", "accent": "#ABCDEF", "grey-4": "#999999"}
    html = C.get("column_chart", tokens=tokens)
    spec = json.loads(re.search(r"data-chart='([^']+)'", html).group(1))
    assert spec["colors"] == ["#999999"] and spec["options"]["pointColors"] == {"0": {"5": "#ABCDEF"}}
    assert "var(--ink," in html
    tiles = C.get("kpi_tiles", tokens=tokens)
    assert "var(--primary,#123456)" in tiles and "var(--accent,#ABCDEF)" in tiles
    # Without tokens the fallbacks are the neutral palette, and the snippet still works on its own.
    assert "var(--primary,#1F4E79)" in C.get("kpi_tiles")


def test_a_fragment_is_one_positioned_block_with_a_header():
    html = C.get("chevron_process", x=64, y=150, w=1152, h=300)
    header, body = html.split("\n", 1)
    assert header.startswith("<!-- component chevron_process:") and "Params — steps: list of {title, bullets}" in header
    assert body.startswith('<div data-name="Process" data-pptx="group" style="position:absolute;left:64px;top:150px;'
                           'width:1152px;height:300px">')
    assert "calc(" not in body, "the export reads clip-path points as % or px only"


def test_numbers_follow_banking_conventions():
    assert C.fmt(-1.23, "0.0") == "(1.2)" and C.fmt(11.24, "x") == "11.2x" and C.fmt(None) == "n.a."
    assert C.fmt(1234.5, "0") == "1,234" and C.fmt(-4.56, "%") == "(4.6)%".replace(")%", "%)")
    table = C.get("comps_table")
    assert "(1.3%)" in table and "n.a." in table and "Median" in table and "Mean" in table


CUSTOM = {
    "action_title": {"text": "Zeta margin rises"},
    "source_note": {"source": "Source: Zeta filings"},
    "tracker": {"sections": ["Alpha", "Beta"]},
    "sticker": {"label": "Draft"},
    "callout": {"body": "Zeta takeaway"},
    "numbered_list": {"items": ["Zeta one", "Zeta two"]},
    "three_column": {"columns": [{"title": "Zeta A", "bullets": ["z1"]}, {"title": "Zeta B", "bullets": ["z2"]}]},
    "pros_cons": {"pros": ["Zeta upside"]},
    "kpi_tiles": {"tiles": [{"value": "7", "label": "Zeta sites"}, {"value": "9", "label": "Zeta hubs"}]},
    "chevron_process": {"steps": [{"title": "Z1"}, {"title": "Z2"}, {"title": "Z3"}]},
    "value_chain": {"stages": [{"title": "Z1"}, {"title": "Z2"}, {"title": "Z3"}]},
    "pyramid": {"levels": [{"title": "Z top"}, {"title": "Z base"}]},
    "funnel": {"stages": [{"label": "Z leads", "value": "10"}, {"label": "Z wins", "value": "2"}]},
    "hub_spoke": {"center": "Zeta", "nodes": [{"title": "Z1"}, {"title": "Z2"}, {"title": "Z3"}]},
    "issue_tree": {"root": "Zeta question", "branches": [{"title": "Z1", "leaves": ["z"]}, {"title": "Z2"}]},
    "org_tiers": {"tiers": [{"label": "Z board", "boxes": ["Zb"]}, {"label": "Z team", "boxes": ["Zt"]}]},
    "gantt": {"periods": ["P1", "P2"], "rows": [{"label": "Zeta work", "start": 0, "end": 2}]},
    "timeline": {"milestones": [{"date": "2031", "title": "Z1"}, {"date": "2032", "title": "Z2"}]},
    "matrix_2x2": {"dots": [{"label": "Zeta", "x": 0.4, "y": 0.6}]},
    "harvey_table": {"criteria": ["Zc"], "rows": [{"label": "Zr", "scores": [3]}]},
    "check_matrix": {"columns": ["Zc"], "rows": [{"label": "Zr", "marks": [1]}]},
    "rag_table": {"columns": ["Zeta item", "Status"], "rows": [{"cells": ["Zi", "G"]}]},
    "heatmap_table": {"columns": ["Zc"], "rows": [{"label": "Zr", "values": [1.0]}]},
    "comparison_columns": {"options": ["Zeta A", "Zeta B"], "cells": [["z1", "z2"]]},
    "comps_table": {"columns": ["Company", "EV / EBITDA"], "rows": [{"cells": ["Zeta Co", 7.1]}]},
    "sensitivity_table": {"row_values": ["7%"], "col_values": ["1%"], "values": [[5.0]]},
    "sources_uses": {"sources": [{"label": "Zeta debt", "value": 5}], "uses": [{"label": "Zeta buy", "value": 5}]},
    "column_chart": {"categories": ["A", "B", "C"], "values": [1, 2, 3], "highlight": [2]},
    "cagr_arrow": {"label": "+3% p.a."},
    "waterfall": {"categories": ["Zs", "Zd", "Ze"], "values": [10, -2, 8]},
    "football_field": {"rows": [{"label": "Zeta DCF", "low": 1, "high": 2}]},
    "share_bar": {"categories": ["Z1"], "series": [{"name": "Zeta", "values": [100]}]},
    "line_chart": {"categories": ["Z1", "Z2"], "series": [{"name": "Zeta", "values": [1, 2]}]},
    "mekko": {"segments": [{"name": "Zeta", "size": 1, "parts": [{"label": "Zp", "share": 100}]}]},
    "legend": {"items": [{"label": "Zeta", "color": "primary"}]},
    "keyed_markers": {"markers": [{"n": 1, "x": 300, "y": 300}]},
    "delta_badge": {"value": "-3%"},
    "bar_table": {"rows": [{"label": "Zeta cost", "value": 3, "benchmark": 2, "lower_is_better": True}]},
    "bullet_rows": {"rows": [{"label": "Zeta sales", "actual": 5, "target": 6}]},
}


def _demo_strings(name: str) -> set[str]:
    """Every text leaf (>= 4 chars) in the showcase values of the component's content params."""
    found: set[str] = set()

    def walk(v):
        if isinstance(v, str):
            if len(v.strip()) >= 4 and v != "text":   # "text" is a comps column format, a setting
                found.add(v)
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    spec = C.spec(name)
    for key in C.content_params(name):
        walk(spec.params[key])
    return found


def test_every_component_has_a_minimal_custom_call():
    assert set(CUSTOM) == set(C.names())


@pytest.mark.parametrize("name", C.names())
def test_a_custom_call_draws_no_demo_content(name):
    import html as html_lib

    body = html_lib.unescape(C.get(name, **CUSTOM[name]).split("\n", 1)[1])
    mine = json.dumps(CUSTOM[name])
    leaked = sorted(d for d in _demo_strings(name) if d in body and d not in mine)
    assert not leaked, f"{name} drew demo content: {leaked}"
    manifest = _manifest("synthetic")
    assert _lint(_component_slide(manifest, name, **CUSTOM[name]), manifest) == []
    assert C.get(name), "the showcase still works"


def test_the_reported_leaks_are_closed():
    chart = C.get("column_chart", categories=["A", "B", "C"], values=[1, 2, 3], highlight=[2])
    assert "p.a." not in chart and "Revenue" not in chart and "<polyline" not in chart
    six = C.get("column_chart", categories=list("ABCDEF"), values=[1, 2, 3, 4, 5, 6])
    assert "+11% p.a." not in six and "USD m" not in six
    assert "Offer" not in C.get("football_field", rows=[{"label": "DCF", "low": 1, "high": 2}])
    assert "Premium of 35%" not in C.get("pros_cons", pros=["Upside"]) and "Risks" not in C.get("pros_cons", pros=["Up"])
    bridge = C.get("waterfall", categories=["S", "D", "E"], values=[10, -2, 8])
    assert json.loads(re.search(r"data-chart='([^']+)'", bridge).group(1))["options"]["totals"] == [0, 2]
    assert "Growth, % p.a." not in C.get("heatmap_table", columns=["A"], rows=[{"label": "r", "values": [1]}])


@pytest.mark.parametrize("name,params", [
    ("column_chart", {"categories": ["A"]}),                               # values missing
    ("hub_spoke", {"nodes": [{"title": "a"}, {"title": "b"}, {"title": "c"}]}),  # center missing
    ("kpi_tiles", {"tiles": [{"value": "1"}], "highlight": 3}),            # index past the caller's tiles
    ("rag_table", {"columns": ["A", "B"], "rows": [{"cells": ["x", "G"]}], "status_column": 5}),
    ("comparison_columns", {"options": ["A", "B"], "cells": [["1", "2"]], "recommended": 2}),
    ("column_chart", {"categories": ["A"], "values": [1], "highlight": [4]}),
    ("pros_cons", {"pros_title": "Only a title"}),                         # nothing to list
])
def test_custom_calls_missing_data_or_with_bad_indices_are_value_errors(name, params):
    with pytest.raises(ValueError):
        C.get(name, **params)


def _boxes(html: str) -> list[dict]:
    """Every absolutely positioned div: its px geometry and the rest of its style."""
    out = []
    for m in re.finditer(r'<div[^>]*style="position:absolute;left:([\d.-]+)px;top:([\d.-]+)px;width:([\d.-]+)px'
                         r'(?:;height:([\d.-]+)px)?;?([^"]*)"', html):
        out.append({"x": float(m[1]), "y": float(m[2]), "w": float(m[3]), "h": float(m[4] or 0), "style": m[5]})
    return out


def _fills(html: str, x0: float) -> list[dict]:
    """The plain filled shapes (no text) starting at or right of x0: bars, bands, ticks."""
    return [b for b in _boxes(html.split("\n", 1)[1])
            if b["x"] >= x0 - 2 and b["style"].startswith("box-sizing:border-box;background:")
            and "font-size" not in b["style"]]


def test_delta_badge_colours_by_whether_the_move_is_good():
    fall_in_cost = C.get("delta_badge", value="-12%", good_when="down")
    assert "var(--rag-g" in fall_in_cost and "var(--rag-r" not in fall_in_cost
    assert "polygon(0 0,100% 0,50% 100%)" in fall_in_cost, "a fall points down"
    rise_in_cost = C.get("delta_badge", value="+5%", good_when="down")
    assert "var(--rag-r" in rise_in_cost and "polygon(50% 0,100% 100%,0 100%)" in rise_in_cost
    assert "var(--rag-g" in C.get("delta_badge", value="+5%"), "good_when defaults to up"
    assert "var(--rag-r" in C.get("delta_badge", value="(2.1)"), "parentheses read as a fall"
    flat = C.get("delta_badge", value="0.0%")
    assert "var(--ink-2" in flat and "clip-path" not in flat, "flat is a grey dash, no triangle"
    assert "polygon(0 0,100% 0,50% 100%)" in C.get("delta_badge", value="12%", direction="down")
    for kw in ({"value": "1%", "direction": "sideways"}, {"value": "1%", "good_when": "never"}, {"value": 12}):
        with pytest.raises(ValueError):
            C.get("delta_badge", **kw)


def test_bar_table_lower_is_better_flips_what_is_highlighted():
    """The subject's bar is flagged when it is WORSE, not when it is longer: a higher cost is worse, a higher
    delivery rate is better."""
    bx0 = min(280.0, 900 * 0.28) + 12

    def subject_fill(row, **kw):
        return _fills(C.get("bar_table", rows=[row], w=900, h=200, **kw), bx0)[0]["style"]  # subject bar first

    cost = {"label": "Cost", "value": 14.0, "benchmark": 12.0, "lower_is_better": True}
    assert "var(--rag-r" in subject_fill(cost), "higher cost is worse"
    assert "var(--primary" in subject_fill({**cost, "lower_is_better": False}), "higher is better without the flag"
    otd = {"label": "OTD", "value": 91.0, "benchmark": 95.0, "format": "%"}
    assert "var(--rag-r" in subject_fill(otd), "a shorter bar is worse when higher is better"
    assert "var(--primary" in subject_fill({**otd, "lower_is_better": True})
    assert "var(--rag-g" in subject_fill({**cost, "lower_is_better": False}, highlight="better")
    assert "var(--primary" in subject_fill(cost, highlight="none")
    # the gap badge reads the same way: a cost above the benchmark is a red up-triangle, no green anywhere
    html = C.get("bar_table", rows=[cost])
    assert "+2.0" in html and "polygon(50% 0,100% 100%,0 100%)" in html
    assert html.count("var(--rag-r") >= 2 and "var(--rag-g" not in html
    assert "var(--accent" not in C.get("bar_table"), "the accent stays free for the slide's takeaway"


def test_bar_table_labels_scales_and_gaps():
    rows = [{"label": "Margin", "value": 20.0, "benchmark": 10.0, "format": "%"},
            {"label": "Days", "value": 5, "benchmark": 4, "format": "0", "lower_is_better": True}]
    html = C.get("bar_table", rows=rows, w=1000, h=300)
    for label in ("20.0%", "10.0%", "+10.0 pts", ">5<", ">4<", "+1<"):
        assert label in html, label
    bx0 = min(280.0, 1000 * 0.28) + 12
    bar_w = 1000 - bx0 - 124 - 72

    def lengths(scale):
        return [b["w"] for b in _fills(C.get("bar_table", rows=rows, w=1000, h=300, scale=scale), bx0)
                if b["x"] == bx0 and b["y"] >= 30]      # below the legend row

    assert lengths("row") == pytest.approx([bar_w, bar_w / 2, bar_w, bar_w * 0.8], abs=0.02)
    assert lengths("column") == pytest.approx([bar_w, bar_w / 2, bar_w / 4, bar_w / 5], abs=0.02)
    assert "+100.0%" in C.get("bar_table", rows=rows[:1], gap="pct")
    assert "n.m." in C.get("bar_table", rows=[{**rows[0], "gap": "n.m."}])
    assert "polygon" not in C.get("bar_table", rows=rows, gap="none")


def test_bar_table_rows_are_compact_in_dense_type():
    rows = [{"label": f"M{i}", "value": i + 1, "benchmark": 3} for i in range(3)]
    body = C.get("bar_table", rows=rows, x=64, y=120, w=1152, h=480).split("\n", 1)[1]
    rules = sorted(b["y"] for b in _boxes(body) if b["h"] == 1 and b["w"] == 1152)
    pitch = [b - a for a, b in zip(rules, rules[1:], strict=False)]
    assert pitch and max(pitch) <= 40, f"rows stretched to {pitch}: they must stay compact, not fill the box"
    assert "var(--fs-dense" in body and "var(--fs-source" in body and "var(--fs-body" not in body
    for kw in ({"rows": [{"label": "x", "value": -1, "benchmark": 2}]},               # bars start at zero
               {"rows": [{"label": "x", "value": 1}]},                                 # no benchmark
               {"rows": [{"label": "x", "value": 1, "benchmark": 2}] * 14, "h": 200},  # cannot fit legibly
               {"rows": rows, "scale": "log"}, {"rows": rows, "highlight": "longer"}):
        with pytest.raises(ValueError):
            C.get("bar_table", **kw)


def test_bullet_rows_draw_bands_bar_and_target_tick():
    row = {"label": "Sales", "actual": 50, "target": 80, "bands": [40, 70, 100], "format": "0"}
    html = C.get("bullet_rows", rows=[row], w=1000, h=200)
    px0 = min(260.0, 1000 * 0.26) + 12
    pw = 1000 - px0 - 150 - 12
    shapes = [b for b in _fills(html, px0) if b["y"] >= 30]      # below the legend row
    bands = [b for b in shapes if "grey" in b["style"]]
    assert [b["w"] for b in bands] == pytest.approx([pw * .4, pw * .3, pw * .3], abs=0.02)
    assert "grey-3" in bands[0]["style"] and "grey-1" in bands[-1]["style"], "dark = poor, light = good"
    bar = next(b for b in shapes if "rag-r" in b["style"] or "primary" in b["style"])
    assert bar["w"] == pytest.approx(pw * .5, abs=0.02) and "var(--rag-r" in bar["style"], "a miss is red"
    tick = next(b for b in shapes if b["w"] == 3)
    assert tick["x"] == pytest.approx(px0 + pw * .8 - 1.5, abs=0.02) and tick["h"] > bar["h"]
    assert "var(--ink" in tick["style"] and "<b>50</b>" in html and "vs 80" in html
    # lower is better: the same numbers now beat the target, and the bands run light (good) to dark (poor)
    low = C.get("bullet_rows", rows=[{**row, "lower_is_better": True}], w=1000, h=200)
    assert "var(--rag-r" not in low
    lb = [b for b in _fills(low, px0) if b["y"] >= 30 and "grey" in b["style"]]
    assert "grey-1" in lb[0]["style"] and "grey-3" in lb[-1]["style"]
    for kw in ({"rows": [{**row, "bands": [70, 40]}]}, {"rows": [{"label": "x", "actual": 1}]},
               {"rows": [{**row, "actual": -2}]}, {"rows": [row], "scale": "log"}):
        with pytest.raises(ValueError):
            C.get("bullet_rows", **kw)


def test_keyed_markers_sit_on_their_anchors_and_match_the_notes():
    html = C.get("keyed_markers", x=64, y=120, w=1152, h=400,
                 markers=[{"n": 1, "x": 300, "y": 260}, {"n": 2, "x": 500, "y": 300}],
                 notes=[{"n": 2, "lead": "Second", "text": "b"}, {"n": 1, "lead": "First", "text": "a"}])
    body = html.split("\n", 1)[1]
    chips = [b for b in _boxes(body) if "border-radius:50%" in b["style"]]
    assert len(chips) == 4, "two markers on the exhibit, the same two numbers in the panel"
    assert (chips[0]["x"] + 12, chips[0]["y"] + 12) == (300 - 64, 260 - 120), "a marker is centred on its anchor"
    numbers = re.findall(r'border-radius:50%[^"]*">([^<]+)</div>', body)
    assert sorted(numbers[:2]) == sorted(numbers[2:]) == ["1", "2"]
    assert body.index("Second") < body.index("First"), "notes keep the caller's order"
    for kw in ({"markers": [{"n": 3, "x": 300, "y": 260}], "notes": [{"n": 1, "text": "a"}]},   # no note 3
               {"markers": [{"n": 1, "x": 1100, "y": 260}], "notes": [{"n": 1, "text": "a"}]},  # under the panel
               {"markers": [{"n": 1, "x": 10, "y": 260}]},                                    # off the box
               {"markers": [{"n": 1}]}, {"markers": [{"n": 1, "x": 300, "y": 260}], "side": "left"}):
        with pytest.raises(ValueError):
            C.get("keyed_markers", x=64, y=120, w=1152, h=400, **kw)


def test_the_exhibit_devices_follow_the_master_tokens():
    tokens = {"primary": "#123456", "rag-r": "#AA0000", "rag-g": "#00AA00", "ink": "#101010"}
    for name in ("keyed_markers", "delta_badge", "bar_table", "bullet_rows"):
        html = C.get(name, tokens=tokens)
        bare = re.sub(r"var\(--[\w-]+,#[0-9A-Fa-f]{6}\)", "", html)
        assert not re.search(r"#[0-9A-Fa-f]{6}", bare), f"{name} hard-codes a colour outside a token"
    assert "var(--rag-r,#AA0000)" in C.get("bar_table", tokens=tokens)
