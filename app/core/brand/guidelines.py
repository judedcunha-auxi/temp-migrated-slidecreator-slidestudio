"""Brand fields from a brand-guidelines PDF: one model call (Darwin `_shared/brandExtract.ts`).

`/api/brand-extract` queues a `darwin.brand_guidelines` job (app/core/darwin/brand_jobs.py); its handler
calls `extract_guidelines` with the PDF. The model reads the whole PDF as a document block and answers
with the six fields below; each is kept only if it is valid (`sanitize_extract`, per-field salvage:
one bad value never fails the extraction).

What changed from Darwin: the forced `emit_brand_fields` tool call is a structured-output JSON schema
on the provider port (the storyline's `StorylineModel`), so no SDK is imported here and the tests use a
scripted fake. The prompt, the fields and their limits are Darwin's; the model is `STORYLINE_MODEL`,
as Darwin used.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any

from app.core.storyline.ports import ModelUsage, StorylineModel, StructuredRequest

#: Darwin's error when the model gave no fields (its "no tool call" case).
NO_FIELDS = "Claude did not return an extraction tool call"
MAX_TOKENS = 2048

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
#: field -> (min length, max length) for the strings; colours are #RRGGBB (`FIELD_SCHEMAS`).
_LIMITS: dict[str, tuple[int, int]] = {"company": (1, 120), "headingFont": (1, 80), "bodyFont": (1, 80),
                                       "styleTemplate": (1, 4000)}
FIELDS: tuple[str, ...] = ("company", "primaryColor", "accentColor", "headingFont", "bodyFont", "styleTemplate")

SYSTEM = """You extract brand identity facts from brand-guidelines documents for a slide-generation product.
Read the document and emit ONLY what it actually specifies, via the emit_brand_fields tool:
- company: the brand/company name as the guidelines present it.
- primaryColor / accentColor: the two most load-bearing brand colors as #RRGGBB hex. Prefer explicitly designated primary/secondary colors; convert from RGB/CMYK/Pantone when no hex is given.
- headingFont / bodyFont: the specified typefaces for headings vs body. If only one family is specified, use it for both.
- styleTemplate: 4-8 lines of slide art direction distilled from the guidelines (tone of voice, imagery rules, layout dos and don'ts), written as imperative instructions for a slide designer. Refer to colors and fonts ONLY via the literal placeholders {primaryColor}, {accentColor}, {headingFont}, {bodyFont}, and to the company via {company}, so the template stays valid if those values change.
Omit any field the document does not support — never guess."""  # noqa: E501 - Darwin's prompt, verbatim

PROMPT = "Extract the brand identity fields from these brand guidelines."

#: `emit_brand_fields`' input schema as a structured-output schema (every field optional).
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "company": {"type": "string"},
        "primaryColor": {"type": "string", "description": "#RRGGBB"},
        "accentColor": {"type": "string", "description": "#RRGGBB"},
        "headingFont": {"type": "string"},
        "bodyFont": {"type": "string"},
        "styleTemplate": {"type": "string", "description": "Slide art direction using {token} placeholders"},
    },
    "required": [],
}


class GuidelinesError(Exception):
    """The model answered with no fields. The message is Darwin's text, safe to show."""


@dataclass(frozen=True)
class GuidelinesResult:
    fields: dict[str, str]
    usage: ModelUsage
    model: str


def sanitize_extract(raw: Any) -> dict[str, str]:
    """`sanitizeExtract`: keep every valid field, drop the rest."""
    r = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    for key in FIELDS:
        value = r.get(key)
        if not isinstance(value, str):
            continue
        if key in ("primaryColor", "accentColor"):
            if _HEX.match(value):
                out[key] = value
            continue
        low, high = _LIMITS[key]
        if low <= len(value.encode("utf-16-le", "surrogatepass")) // 2 <= high:
            out[key] = value
    return out


async def extract_guidelines(model: StorylineModel, pdf: bytes, *, model_name: str) -> GuidelinesResult:
    """One model call over the PDF. Raises the port's ModelUnavailable / ModelRejected, and
    GuidelinesError when the reply carries no fields object."""
    request = StructuredRequest(
        purpose="brand_guidelines", model=model_name, system=SYSTEM, schema_name="emit_brand_fields",
        schema=SCHEMA, max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.b64encode(pdf).decode("ascii")}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    reply = await model.structured(request)
    if reply.data is None:
        raise GuidelinesError(NO_FIELDS)
    return GuidelinesResult(fields=sanitize_extract(reply.data), usage=reply.usage, model=reply.model or model_name)
