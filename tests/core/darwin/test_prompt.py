"""Darwin's image prompt, ported: byte-for-byte against Darwin's own output (data/darwin_prompt_golden.json).

The fixture was dumped by bundling Slide-Creator's TypeScript (`prompt.ts`, `refine.ts`, `layoutMatcher.ts`,
`workzone.ts`) and running it on synthetic slides and kits: every assembled prompt is pinned by its SHA-256
(a handful in full, for a readable diff), plus the layout matcher, the zone hints, the tile sizes and the
refinement prompt.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app.core.brand.workzone import Workzone, should_tile
from app.core.darwin import prompt_text
from app.core.darwin.js import is_js_integer, js_length, js_number, js_round, js_str, js_trim
from app.core.darwin.prompt import (
    DarwinSlidePrompter,
    assemble_slide_prompt,
    framework_hint,
    layout_hint_text,
    match_layout,
    prompt_kit,
    steered_prompt,
    type_scaffold,
    workzone_px_to_image_size,
)
from app.core.storage.memory import InMemoryStorage
from app.core.storage.models import BrandCreate, CallerContext, IdentityClaims

GOLDEN = json.loads((Path(__file__).parent / "data" / "darwin_prompt_golden.json").read_text(encoding="utf-8"))
PROMPTS = GOLDEN["prompts"]


def _assemble(case: dict[str, Any]) -> str:
    kit = prompt_kit(GOLDEN["rawKits"][case["kit"]], case.get("legacy"))
    return assemble_slide_prompt(GOLDEN["slides"][case["slide"]], kit, case.get("style"), case.get("hint"),
                                 case.get("language"), bool(case.get("contentOnly")))


@pytest.mark.parametrize("case", PROMPTS, ids=[c["name"] for c in PROMPTS])
def test_prompt_matches_darwin(case: dict[str, Any]) -> None:
    got = _assemble(case)
    if "prompt" in case and got != case["prompt"]:
        diff = "\n".join(difflib.unified_diff(case["prompt"].splitlines(), got.splitlines(), "darwin", "port",
                                              lineterm=""))
        pytest.fail(f"prompt differs from Darwin's:\n{diff}")
    assert hashlib.sha256(got.encode("utf-8")).hexdigest() == case["sha256"]


def test_the_hint_cases_use_the_matched_layout() -> None:
    """The fixture's `hint` is the matched layout's text (generate / storyline-background); recompute it."""
    for case in (c for c in PROMPTS if c["name"].endswith("/hint")):
        kit = prompt_kit(GOLDEN["rawKits"][case["kit"]])
        slide = GOLDEN["slides"][case["slide"]]
        matched = match_layout(kit.layouts, slide.get("type"), slide.get("framework"))
        assert matched is not None
        assert layout_hint_text(matched, kit.workzone) == case["hint"]


@pytest.mark.parametrize("row", GOLDEN["steered"])
def test_steered_prompt(row: dict[str, str]) -> None:
    assert steered_prompt(row["base"], row["instr"]) == row["prompt"]


def test_match_layout() -> None:
    layouts = GOLDEN["layouts"]
    for row in GOLDEN["matches"]:
        pool = [layouts[i] for i in row["subset"]] if "subset" in row else layouts
        found = match_layout(pool, row["type"], row["framework"])
        assert (found["index"] if found else None) == row["index"], row
    assert match_layout(None, "data", "x") is None and match_layout([], "data", "x") is None


def test_layout_hints() -> None:
    by_index = {layout["index"]: layout for layout in GOLDEN["layouts"]}
    for row in GOLDEN["hints"]:
        wz = Workzone(**row["workzone"]) if row["workzone"] else None
        assert layout_hint_text(by_index[row["index"]], wz) == row["text"]


def test_tile_sizes_and_gate() -> None:
    for row in GOLDEN["sizes"]:
        wz = row["workzone"]
        w, h, padded = workzone_px_to_image_size(wz["width"] * 2560, wz["height"] * 1440)
        assert (w, h, padded) == (row["size"]["width"], row["size"]["height"], row["size"]["padded"]), wz
    for row in GOLDEN["tiles"]:
        assert should_tile("content", Workzone(**row["workzone"]), ["content"]) is row["content"]


def test_vocabulary_lookups() -> None:
    assert framework_hint("Value-Chain") == prompt_text.FRAMEWORK_HINTS["value chain"]
    assert framework_hint("nothing like it") is None
    assert type_scaffold("nope") == type_scaffold(None) == prompt_text.SCAFFOLDS["framework"]
    assert len(prompt_text.FRAMEWORK_HINTS) == 65 and len(prompt_text.SCAFFOLDS) == 22


def test_js_semantics() -> None:
    assert [js_str(x) for x in (12.0, 2.5, 1e21, 0.00001, 1e-7, -3, 123456789012, 0.1, 10**22, True, None)] == [
        "12", "2.5", "1e+21", "0.00001", "1e-7", "-3", "123456789012", "0.1", "1e+22", "true", "null"]
    assert js_round(2.5) == 3 and js_round(-2.5) == -2 and js_round(0.5) == 1
    assert js_trim("\ufeff x \u3000") == "x" and js_length("a\U0001F600") == 3
    assert [js_number(x) for x in (None, "", " 2 ", "1.0", "1e0", "0x10", "+3", ".5")] == [0, 0, 2, 1, 1, 16, 3, 0.5]
    for bad in ("abc", "1_000", "inf", "-0x10", "1e", "0b12"):
        assert js_number(bad) != js_number(bad)  # NaN
    assert is_js_integer(2.0) and is_js_integer(3) and not is_js_integer(True) and not is_js_integer(2.5)


@pytest.mark.asyncio
async def test_the_storyline_prompter_reads_the_callers_brand() -> None:
    storage = InMemoryStorage()
    ctx = CallerContext(subject="alice", request_id="r")
    await storage.users.get_or_create(ctx, IdentityClaims(email="a@example.com"))
    brand = await storage.brands.create(ctx, BrandCreate(name="B", kit=GOLDEN["rawKits"]["extracted"]))
    slide = GOLDEN["slides"]["content"]
    prompter = DarwinSlidePrompter(storage)

    texts = await prompter("alice", {"brandId": brand.id, "language": "ar", "styleInstructions": "Sober."}, [slide])
    kit = prompt_kit(GOLDEN["rawKits"]["extracted"], {"brandId": brand.id})
    matched = match_layout(kit.layouts, slide["type"], slide["framework"])
    assert matched is not None
    assert texts == [assemble_slide_prompt(slide, kit, "Sober.", layout_hint_text(matched, kit.workzone), "ar")]

    # A brand the caller cannot read falls back to the (empty) profile kit, as Darwin's resolveBrandSource.
    fallback = await prompter("alice", {"brandId": "3fa85f64-5717-4562-b3fc-2c963f66afa6"}, [slide])
    assert fallback == [assemble_slide_prompt(slide, prompt_kit(None), None, None, None)]
