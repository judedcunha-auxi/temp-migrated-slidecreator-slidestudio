"""Darwin's one-shot quick generation: `/api/quick-generate` + the job that was
`quick-generate-background.ts`, and what `/api/quick-status` and `/api/quick-image` read.

The job (`darwin.quick_generate`) drafts a storyline from the wizard fields (the storyline core, dividers
inserted, Anthropic through the LLM layer), assembles each slide's image prompt with the caller's brand kit
(`prompt.assemble_slide_prompt`, no layout hint, never a tile), and renders every slide (10 in flight),
conditioned on the slide type's brand master. An inline layout PNG posted with the request beats the brand's
Layout master for that one job. The result is `{slides: [{slideNumber, imageRef}], warnings}`; any failure
fails the whole job with a message the status route shows, as Darwin did.

Differences from Darwin (docs/darwin-api.md §6):

* **the global image cap applies** (plan §6.5, C12): Darwin's background skipped `reserveImageSlot`, so quick
  spend was unbounded. No slot left fails the job with "Free capacity reached, try again tomorrow.";
* the first failure cancels the images still being drawn (Darwin let them run on and pay);
* the storyline call and every image are recorded in the cost ledger (Darwin recorded none);
* the job runs as its owner (D31, C12): the background function took the user id from its body;
* a model or provider failure shows its safe message (the storyline's, or the image provider's); any other
  failure reads "Generation failed" (Darwin showed any exception's text).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.config.storyline import StorylineSettings
from app.core.darwin.caps import CAP_REACHED, reserve_image_slot
from app.core.darwin.generate import GenerationDeps, read_brand_asset, resolve_master
from app.core.darwin.image_gen import ImageGenError
from app.core.darwin.prompt import assemble_slide_prompt, prompt_kit, resolve_brand_source
from app.core.darwin.prompt_text import MASTER_INSTRUCTION, MASTER_INSTRUCTION_RTL
from app.core.darwin.usage import EST_COST_MASTER, EST_COST_PER_IMAGE, KIND_IMAGE, KIND_STORYLINE, record_usage_quietly
from app.core.jobs.queue import JobType
from app.core.jobs.worker import Handler, JobContext, JobOutcome, PermanentJobError
from app.core.storage.models import JobRecord
from app.core.storage.ports import NotFound
from app.core.storyline.models import StorylineInputs
from app.core.storyline.ports import ModelError, StorylineModel
from app.core.storyline.service import StorylineRefused, draft_storyline
from app.core.storyline.validation import StorylineValidationError

_log = logging.getLogger(__name__)

QUICK_JOB = "darwin.quick_generate"
JOB_TYPE = JobType(QUICK_JOB, expensive=False, max_attempts=1, timeout_s=900.0)  # TODO-P5 in-flight

GENERATION_FAILED = "Generation failed"
LAYOUT_TOO_BIG = "Layout image must be a PNG up to 4MB"
LAYOUT_NOT_PNG = "Layout image must be a PNG"
LAYOUT_NOT_STRING = "layoutImageB64 must be a base64 string"
MAX_LAYOUT_BYTES = 4 * 1024 * 1024
PNG_MAGIC = b"\x89PNG"

_MASTER_ASSETS = (("layout", "master", "master_key"), ("title", "titleMaster", "title_master_key"),
                  ("divider", "dividerMaster", "divider_master_key"))


@dataclass(frozen=True)
class QuickDeps:
    generation: GenerationDeps
    model: StorylineModel
    storyline: StorylineSettings


class _Failed(Exception):
    """A failure whose message the status route may show."""


def quick_slides(record: JobRecord) -> list[dict[str, Any]]:
    """The done record's slides, `[{slideNumber, imageRef}]`, sorted (Darwin sorted them before writing)."""
    slides = (record.result or {}).get("slides")
    return [s for s in slides if isinstance(s, dict)] if isinstance(slides, list) else []


def quick_handler(deps: QuickDeps) -> Handler:
    async def handle(job: JobContext) -> JobOutcome:
        storage = deps.generation.storage
        layout_ref = job.record.inputs.get("layoutRef")
        try:
            return await _run(deps, job)
        except _Failed as exc:
            raise PermanentJobError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - "Generation failed", without internals
            _log.exception("quick: the job failed")
            raise PermanentJobError(GENERATION_FAILED) from exc
        finally:
            if isinstance(layout_ref, str):  # a one-shot blob: quick jobs have no retry path
                try:
                    await storage.blobs.delete(job.owner, layout_ref)
                except Exception:  # noqa: BLE001
                    _log.info("quick: could not delete the inline layout")

    return handle


async def _run(deps: QuickDeps, job: JobContext) -> JobOutcome:
    gen, owner, storage = deps.generation, job.owner, deps.generation.storage
    raw = job.record.inputs.get("inputs")
    inputs: dict[str, Any] = raw if isinstance(raw, dict) else {}
    try:
        draft = await draft_storyline(deps.model, StorylineInputs.from_wizard(inputs,
                                                                             max_slides=deps.storyline.max_slides),
                                      settings=deps.storyline)
    except (StorylineValidationError, StorylineRefused) as exc:
        raise _Failed(str(exc)) from exc
    except ModelError as exc:
        _log.error("quick: the storyline call failed: %s", exc)
        raise _Failed(GENERATION_FAILED) from exc
    await record_usage_quietly(storage.usage, owner, model=draft.model or deps.storyline.model,
                               cost_usd=draft.usage.cost_usd, kind=KIND_STORYLINE,
                               idempotency_key=f"job:{job.record.id}:storyline")

    source = await resolve_brand_source(storage, owner, inputs.get("brandId"))
    kit = prompt_kit(source.kit, inputs)
    style = inputs.get("styleInstructions") if isinstance(inputs.get("styleInstructions"), str) else None
    language = inputs.get("language") if isinstance(inputs.get("language"), str) else None
    slides = [(s.number, s.type, assemble_slide_prompt(s.wire(), kit, style, None, language))
              for s in draft.storyline.slides]

    masters: dict[str, bytes] = {}
    try:
        for kind, asset, key in _MASTER_ASSETS:
            if getattr(kit.base, key):
                data = await read_brand_asset(storage, owner, source.brand_id, asset)
                if data:
                    masters[kind] = data
    except Exception:  # noqa: BLE001 - "brand master unavailable, generating without it"
        _log.exception("quick: the brand master is unavailable; generating without it")
    layout_ref = job.record.inputs.get("layoutRef")
    if isinstance(layout_ref, str):
        try:
            masters["layout"] = (await storage.blobs.get(owner, layout_ref)).data
        except NotFound:
            _log.error("quick: the inline layout image is missing; generating without it")
    instruction = MASTER_INSTRUCTION_RTL if language == "ar" else MASTER_INSTRUCTION

    semaphore = asyncio.Semaphore(max(1, gen.concurrency))
    generated: list[dict[str, Any]] = []

    async def run_one(number: int, slide_type: str, prompt: str) -> None:
        async with semaphore:
            if not await reserve_image_slot(gen.redis, gen.darwin):
                raise _Failed(CAP_REACHED)
            master, _ = resolve_master(masters, slide_type)
            try:
                image = await (gen.images.edit(instruction + prompt, [master]) if master is not None
                               else gen.images.generate(prompt))
            except ImageGenError as exc:
                raise _Failed(exc.message) from exc
            stored = await storage.blobs.put(owner, image.png, "image/png")
            cost = EST_COST_MASTER if master is not None else EST_COST_PER_IMAGE
            await record_usage_quietly(storage.usage, owner, model=image.model, cost_usd=cost, kind=KIND_IMAGE,
                                       slide_number=number if number >= 1 else None,
                                       idempotency_key=f"job:{job.record.id}:slide:{number}")
            generated.append({"slideNumber": number, "imageRef": stored.ref, "cost": cost})

    try:
        async with asyncio.TaskGroup() as group:
            for number, slide_type, prompt in slides:
                group.create_task(run_one(number, slide_type, prompt))
    except BaseExceptionGroup as group_error:
        failed = next((e for e in group_error.exceptions if isinstance(e, _Failed)), None)
        if failed is not None:
            raise failed from group_error
        raise
    generated.sort(key=lambda s: s["slideNumber"])
    spent = draft.usage.cost_usd + sum(s.pop("cost") for s in generated)
    return JobOutcome(result={"slides": generated, "warnings": list(draft.warnings)}, cost_usd=spent)
