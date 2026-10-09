"""The prompts and the output schemas (Darwin `anthropic.test.ts` prompt cases, Slide Studio
`test_the_prompt_is_dense_and_names_the_whole_library`), and the structured-output subset the schemas keep to."""

from __future__ import annotations

from typing import Any

from app.core.storyline import archetypes, prompts
from app.core.storyline.models import StorylineInputs
from app.core.storyline.vocabulary import SLIDE_TYPES, framework_names

UNSUPPORTED = {"minItems", "maxItems", "minimum", "maximum", "minLength", "maxLength", "multipleOf", "pattern"}


def _walk_objects(schema: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            found.append(schema)
        for value in schema.values():
            found += _walk_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            found += _walk_objects(value)
    return found


def _keys(schema: Any) -> set[str]:
    if isinstance(schema, dict):
        out = set(schema)
        for key, value in schema.items():
            if key != "properties":
                out |= _keys(value)
            else:
                for prop in value.values():
                    out |= _keys(prop)
        return out
    if isinstance(schema, list):
        return set().union(*(_keys(v) for v in schema)) if schema else set()
    return set()


def test_the_standard_prompt_is_darwins_rules_and_library():
    system = prompts.storyline_system("standard", "en")
    assert "MUST be a name from this library" in system and "Never invent a bespoke framework" in system
    assert "spider/radar chart becomes a weighted scoring matrix" in system
    assert "; ".join(framework_names()) in system  # Darwin's order, Darwin's separator
    assert '"case-study"' in system and '"navigator"' in system
    assert "Provide 3-4 dense, specific bullets" in system and "≤110 words" in system
    assert "emit_storyline" not in system and "response format" in system
    assert archetypes.catalog_text() in system
    assert "ARABIC" not in system


def test_the_dense_prompt_is_slide_studios():
    system = prompts.storyline_system("dense", "en")
    assert all(name in system for name in framework_names())
    assert "110 words" not in system and "[verify]" in system and "executiveSummary" in system
    assert "3-6 dense, specific bullets" in system


def test_arabic_appends_the_directive_and_keeps_the_english_prompt_as_its_prefix():
    en = prompts.storyline_system("standard", "en")
    ar = prompts.storyline_system("standard", "ar")
    assert "LANGUAGE — ARABIC OUTPUT" in ar and "Modern Standard Arabic" in ar and "Western/Latin numerals" in ar
    assert "keep bullets short" in ar and "executiveSummary" not in ar.split("ARABIC OUTPUT")[1]
    # Only the catalog's direction words differ before the directive.
    head = ar.split("\n\nLANGUAGE")[0]
    assert head.replace(archetypes.catalog_text("ar"), "") == en.replace(archetypes.catalog_text(), "")
    dense_ar = prompts.storyline_system("dense", "ar")
    assert "the executiveSummary, every slide title" in dense_ar and "keep bullets short" not in dense_ar


def test_the_user_message_is_darwins_and_carries_the_intake_extras():
    inputs = StorylineInputs.from_wizard({"topic": "x", "company": "Acme", "numSlides": 1, "audience": "exec",
                                          "style": "s", "context": "Board wants a decision on the $28M ask.",
                                          "keyMessages": ["ROI is 4.8x", "Act this quarter"]})
    msg = prompts.storyline_user(inputs)
    assert msg.splitlines()[:5] == ["Create a 1-slide presentation storyline.", "Topic: x",
                                    "Company / brand reference: Acme", "Audience: exec", "Style preset: s"]
    assert "Alignment notes from intake: Board wants a decision on the $28M ask." in msg
    assert "Key messages that must land: ROI is 4.8x; Act this quarter" in msg
    assert msg.endswith("Number the slides 1..1.")
    bare = prompts.storyline_user(StorylineInputs.from_wizard({"topic": "x", "numSlides": 3}))
    assert "Alignment notes" not in bare and "Key messages" not in bare
    assert "Company / brand reference: generic professional" in bare


def test_modes_add_slide_studios_structure_rule():
    assert prompts.structure_rule("auto", 12) == ""
    assert "No title, agenda" in prompts.structure_rule("collection", 5)
    assert 'type "title"' in prompts.structure_rule("deck", 12)
    assert prompts.structure_rule("single", 1).startswith("Exactly 1 slide")


def test_the_schemas_keep_to_the_structured_output_subset():
    for schema in (prompts.storyline_schema("standard"), prompts.storyline_schema("dense"),
                   prompts.intake_schema(), prompts.intake_schema(ask_format=True)):
        for obj in _walk_objects(schema):
            assert obj.get("additionalProperties") is False, obj
            assert set(obj.get("required", [])) <= set(obj.get("properties", {}))
        assert not UNSUPPORTED & _keys(schema)


def test_the_storyline_schema_lists_the_slide_types_and_dense_adds_the_summary():
    std = prompts.storyline_schema("standard")
    assert std["properties"]["slides"]["items"]["properties"]["type"]["enum"] == list(SLIDE_TYPES)
    assert "executiveSummary" not in std["properties"]
    assert "executiveSummary" in prompts.storyline_schema("dense")["required"]


def test_the_intake_prompt_carries_the_brief_state_and_format_only_on_request():
    system = prompts.intake_system({"topic": "GTM"})
    assert system.endswith('Current brief state (already captured — do not re-ask):\n{"topic":"GTM"}')
    assert "update_brief" not in system and "format and slide count" not in system
    assert "format and slide count" in prompts.intake_system({}, ask_format=True)
    assert "format" not in prompts.intake_schema()["properties"]["brief"]["properties"]
    assert "format" in prompts.intake_schema(True)["properties"]["brief"]["properties"]
