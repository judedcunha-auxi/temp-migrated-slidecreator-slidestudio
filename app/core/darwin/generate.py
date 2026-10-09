"""Darwin's image-mode deck generation: `/api/generate`, `/api/retry` and the job that was
`generate-background.ts` + `_shared/generateCore.ts: runGeneration`.

**The route side** (`prepare_deck`, `retry_inputs`): validate and repair the posted storyline, assemble each
slide's image prompt server-side (the client's `prompt` is ignored, so the locked guardrails can never be
stripped), persist the deck with its slides (all `idle`: Darwin's initial job state) and queue the job.

**The job** (`darwin.generate`, `generation_handler`): Darwin's background function, now an internal Redis
job (D31; it is not an HTTP route, so the unauthenticated trigger of C12 is gone). The job runs as its owner:
the deck, the brand and its masters are read with the owner's identity, never with an id from a body.

* The brand's masters (Layout, Title, Divider PNGs) condition the renders when the kit says they exist; a
  slide uses the master of its type (title -> Title, divider -> Divider, else Layout, falling back to
  Layout). Without a Layout master but with extracted layouts, a synthesised wireframe of the matched layout
  conditions it instead (`compose.layout_wireframe`). Which master was used, and why none was
  (`no-brand`, `no-master-uploaded`, `blob-unavailable`, `load-error`), is recorded on the deck.
* A content slide of a brand with a usable workzone and captured furniture renders as a content-only tile at
  the workzone's size (`prompt.workzone_image_size`), with no master.
* Every image takes a global image slot first (`caps.reserve_image_slot`); none left: the slide's error is
  "Free capacity reached, try again tomorrow." At most 10 images are in flight per job.
* A finished image is a new slide version (append-only; a retried slide gains a version instead of
  overwriting v1 as Darwin did) and one ledger row with its cost (C10: the cost in `est_cost_usd`).
* An image failure marks that slide `error` with the provider's message (a content-policy refusal reads as
  the slide's error, as in Darwin); any other failure reads "Image generation failed" (no internals).
* At the end the deck is `done` when every slide is done or error, else `failed`.

A retry (`retryOnly`) regenerates only the listed slides, keeps the deck's original master decision (a
retried slide matches its siblings), and leaves the other slides untouched.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config.darwin import DarwinSettings
from app.core.brand.kit import archetype_for_type
from app.core.brand.workzone import is_degenerate, should_tile
from app.core.darwin import compose
from app.core.darwin import prompt_text as text
from app.core.darwin.caps import CAP_REACHED, reserve_image_slot
from app.core.darwin.image_gen import GeneratedImage, ImageGenerator, ImageGenError
from app.core.darwin.prompt import (
    PromptKit,
    assemble_slide_prompt,
    layout_hint_text,
    match_layout,
    prompt_kit,
    resolve_brand_source,
    workzone_image_size,
)
from app.core.darwin.usage import EST_COST_MASTER, EST_COST_PER_IMAGE, KIND_IMAGE, record_usage_quietly
from app.core.jobs.queue import JobType
from app.core.jobs.worker import Handler, JobContext, JobOutcome, PermanentJobError
from app.core.redis_client import RedisStore
from app.core.storage.models import (
    CallerContext,
    CounterBump,
    Deck,
    DeckCreate,
    DeckPatch,
    MasterKind,
    MasterSkipReason,
    SlideSpec,
    SlideStatusPatch,
    VersionCreate,
    is_valid_id,
)
from app.core.storage.ports import NotFound, Storage
from app.core.storyline.service import revalidate

_log = logging.getLogger(__name__)

GENERATE_JOB = "darwin.generate"
#: Darwin's generateCore pool size: gpt-image's images-per-minute limits make a whole deck at once a 429 storm.
CONCURRENCY = 10
IMAGE_FAILED = "Image generation failed"

# Darwin's background functions ran once (a failure was recorded per slide, never retried): an attempt that
# re-ran would pay for every image again. TODO-P5 in-flight: registered NOT expensive (Darwin had no limit).
JOB_TYPE = JobType(GENERATE_JOB, expensive=False, max_attempts=1, timeout_s=900.0)

_MASTER_ASSETS: tuple[tuple[MasterKind, str], ...] = (("layout", "master"), ("title", "titleMaster"),
                                                      ("divider", "dividerMaster"))


class InvalidStoryline(ValueError):
    """The posted storyline fails validation: `/api/generate` answers 500 "Internal error" (quirk 3)."""


@dataclass(frozen=True)
class GenerationDeps:
    storage: Storage
    redis: RedisStore
    images: ImageGenerator
    darwin: DarwinSettings
    concurrency: int = CONCURRENCY


# ------------------------------------------------------------------------------------- the route
@dataclass(frozen=True)
class PreparedDeck:
    deck: Deck
    warnings: list[str]
    job_inputs: dict[str, Any]


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


async def prepare_deck(storage: Storage, ctx: CallerContext, payload: Any,
                       *, clock: Any = lambda: datetime.now(UTC)) -> PreparedDeck:
    """`generate.ts` up to the trigger: validate + repair, assemble the prompts, persist the deck and its
    slides, the counters and the intake link. Raises `InvalidStoryline` (Darwin's ZodError) and lets a
    storage failure propagate (500 either way)."""
    if not isinstance(payload, dict):
        raise InvalidStoryline("the body is not an object")  # payload.presentationTitle on null/array/string
    try:
        storyline, warnings = revalidate({"presentationTitle": payload.get("presentationTitle"),
                                          "slides": payload.get("slides")})
    except ValueError as exc:
        raise InvalidStoryline(str(exc)) from exc
    inputs = payload.get("inputs")
    fields: dict[str, Any] = inputs if isinstance(inputs, dict) else {}
    brand_id = _str(fields.get("brandId"))
    source = await resolve_brand_source(storage, ctx, brand_id)
    kit = prompt_kit(source.kit, fields)
    style = _str(fields.get("styleInstructions"))
    language = "ar" if fields.get("language") == "ar" else None
    creation = _str(fields.get("creationMethod"))
    session_key = _str(payload.get("intakeSessionKey"))

    job_slides: list[dict[str, Any]] = []
    specs: list[SlideSpec] = []
    for slide in storyline.slides:
        wire = slide.wire()
        content_only = should_tile(archetype_for_type(slide.type), kit.workzone,
                                   list(kit.base.furniture_archetypes or []))
        matched = match_layout(kit.layouts, slide.type, slide.framework) if kit.layouts else None
        hint = layout_hint_text(matched, kit.workzone) if matched is not None and not content_only else None
        prompt = assemble_slide_prompt(wire, kit, style, hint, language, content_only)
        entry: dict[str, Any] = {"number": slide.number, "type": slide.type, "framework": slide.framework,
                                 "prompt": prompt}
        if slide.archetype_id:
            entry["archetypeId"] = slide.archetype_id
        job_slides.append(entry)
        specs.append(SlideSpec(number=slide.number, title=slide.title, type=slide.type, section=slide.section,
                               framework=slide.framework, description=slide.description, bullets=list(slide.bullets),
                               prompt=prompt, archetype_id=slide.archetype_id,
                               chart_data=slide.chart_data.wire() if slide.chart_data else None))

    title = str(payload.get("presentationTitle"))[:500]
    deck = await storage.decks.create(ctx, DeckCreate(
        title=title, inputs=fields, storyline=storyline.wire(), status="generating",
        brand_id=brand_id if brand_id and is_valid_id(brand_id) else None, language=language,
        creation_method=creation if creation and len(creation) <= 32 else None), specs)

    # Darwin's fire-and-forget enrichment: counters, the start time, the intake transcript link.
    for what, call in (("deck counters", lambda: storage.users.bump_counters(ctx, CounterBump(decks=1))),
                       ("generation start", lambda: storage.decks.update(ctx, deck.id, DeckPatch(
                           generation_started_at=clock()))),
                       ("intake link", (lambda: storage.transcripts.link_intake_to_deck(ctx, session_key, deck.id))
                        if session_key else None)):
        if call is None:
            continue
        try:
            await call()
        except Exception:  # noqa: BLE001 - void Promise.all([...]) in generate.ts: never fails the route
            _log.info("generate: the %s update failed", what, exc_info=True)

    job_inputs: dict[str, Any] = {"deckId": deck.id, "slides": job_slides}
    if brand_id:
        job_inputs["brandId"] = brand_id
    if language:
        job_inputs["language"] = language
    return PreparedDeck(deck=deck, warnings=warnings, job_inputs=job_inputs)


def storyline_types(deck: Deck) -> dict[int, str]:
    """`slideTypesFromStoryline`: slide number -> type, from the persisted storyline."""
    out: dict[int, str] = {}
    storyline = deck.storyline if isinstance(deck.storyline, dict) else {}
    for s in storyline.get("slides") or []:
        if isinstance(s, dict) and isinstance(s.get("number"), int) and not isinstance(s.get("number"), bool) \
                and isinstance(s.get("type"), str):
            out[s["number"]] = s["type"]
    return out


async def retry_inputs(storage: Storage, ctx: CallerContext, deck: Deck, retry_only: list[Any]) -> dict[str, Any]:
    """`retry.ts`: the deck's stored prompts with each slide's type re-attached from the storyline (so a
    retried title/divider keeps its own master), its brand and language from `decks.inputs`; the deck goes
    back to `generating`. `retryOnly` is passed on verbatim, as Darwin did."""
    slides = await storage.slides.list_for_deck(ctx, deck.id)
    types = storyline_types(deck)
    job_slides: list[dict[str, Any]] = []
    for slide in slides:
        entry: dict[str, Any] = {"number": slide.number, "prompt": slide.spec.prompt}
        if slide.number in types:
            entry["type"] = types[slide.number]
        job_slides.append(entry)
    await storage.decks.update(ctx, deck.id, DeckPatch(status="generating"))
    inputs: dict[str, Any] = {"deckId": deck.id, "slides": job_slides, "retryOnly": retry_only}
    brand_id = deck.inputs.get("brandId")
    if isinstance(brand_id, str) and brand_id:
        inputs["brandId"] = brand_id
    if deck.inputs.get("language") == "ar":
        inputs["language"] = "ar"
    return inputs


# ---------------------------------------------------------------------------------------- the job
def master_kind_for_type(slide_type: Any) -> MasterKind:
    if slide_type == "title":
        return "title"
    if slide_type == "divider":
        return "divider"
    return "layout"


def resolve_master(masters: Mapping[str, bytes], slide_type: Any) -> tuple[bytes | None, MasterKind | None]:
    """`resolveMaster`: the slide type's own master, falling back to the Layout master."""
    preferred = master_kind_for_type(slide_type)
    if preferred in ("title", "divider") and preferred in masters:
        return masters[preferred], preferred
    if "layout" in masters:
        return masters["layout"], "layout"
    return None, None


async def read_brand_asset(storage: Storage, ctx: CallerContext, brand_id: str | None, name: str) -> bytes | None:
    """A brand asset by its fixed name (the kit's *Key values only say THAT it exists), None when missing."""
    if not brand_id:
        return None
    try:
        return (await storage.brands.get_asset(ctx, brand_id, name)).data
    except NotFound:
        return None


@dataclass
class _Plan:
    kit: PromptKit | None = None
    masters: dict[str, bytes] = field(default_factory=dict)
    skip_reason: MasterSkipReason | None = None
    wireframes: dict[int, bytes] | None = None
    tile_size: str | None = None


async def _plan(deps: GenerationDeps, owner: CallerContext, brand_id: Any, slides: list[dict[str, Any]]) -> _Plan:
    """The brand side of `generate-background.ts`: the masters, the skip reason, the wireframes, the tile size."""
    plan = _Plan()
    try:
        source = await resolve_brand_source(deps.storage, owner, brand_id)
        kit = plan.kit = prompt_kit(source.kit)
        base = kit.base
        if not (base.master_key or base.title_master_key or base.divider_master_key):
            plan.skip_reason = "no-master-uploaded" if brand_id else "no-brand"
        else:
            keys = {"layout": base.master_key, "title": base.title_master_key, "divider": base.divider_master_key}
            for kind, asset in _MASTER_ASSETS:
                if keys[kind]:
                    data = await read_brand_asset(deps.storage, owner, source.brand_id, asset)
                    if data:
                        plan.masters[kind] = data
            if not plan.masters:
                plan.skip_reason = "blob-unavailable"
                _log.error("generate: the kit names master(s) but none could be read")
        if "layout" not in plan.masters and kit.layouts:
            try:
                cache: dict[int, bytes] = {}
                wireframes: dict[int, bytes] = {}
                for s in slides:
                    matched = match_layout(kit.layouts, s.get("type"), s.get("framework"))
                    if matched is None:
                        continue
                    key = id(matched)
                    if key not in cache:
                        cache[key] = await asyncio.to_thread(compose.layout_wireframe, matched,
                                                             kit.master_decorations, kit.logo_shapes)
                    wireframes[int(s["number"])] = cache[key]
                plan.wireframes = wireframes or None
            except Exception:  # noqa: BLE001 - degrade to text-only generation, as Darwin
                _log.exception("generate: wireframe synthesis failed, continuing without")
    except Exception:  # noqa: BLE001 - "Brand master unavailable, generating without it"
        plan.skip_reason = "load-error"
        _log.exception("generate: the brand master is unavailable; generating without it")
    planned = plan.kit
    if planned is not None and planned.workzone is not None and not is_degenerate(planned.workzone):
        plan.tile_size = workzone_image_size(planned.workzone)
    return plan


def _wanted(retry_only: list[Any] | None, number: int) -> bool:
    """`retryOnly.includes(number)`: SameValueZero on numbers (2 matches 2.0); "2" and true do not."""
    if retry_only is None:
        return True
    return any(isinstance(n, (int, float)) and not isinstance(n, bool) and n == number for n in retry_only)


async def _render(deps: GenerationDeps, prompt: str, *, tile_size: str | None, master: bytes | None,
                  guide: bytes | None, rtl: bool) -> GeneratedImage:
    if tile_size:
        return await deps.images.generate(prompt, size=tile_size)
    if master is not None:
        instruction = text.MASTER_INSTRUCTION_RTL if rtl else text.MASTER_INSTRUCTION
        return await deps.images.edit(instruction + prompt, [master])
    if guide is not None:
        instruction = text.LAYOUT_WIREFRAME_INSTRUCTION_RTL if rtl else text.LAYOUT_WIREFRAME_INSTRUCTION
        return await deps.images.edit(instruction + prompt, [guide])
    return await deps.images.generate(prompt)


def generation_handler(deps: GenerationDeps) -> Handler:
    """The `darwin.generate` handler (generate and retry). See the module docstring."""

    async def handle(job: JobContext) -> JobOutcome:
        owner, inputs, storage = job.owner, job.record.inputs, deps.storage
        deck_id = str(inputs.get("deckId") or "")
        try:
            deck = await storage.decks.get(owner, deck_id)
        except (NotFound, ValueError) as exc:
            raise PermanentJobError("Deck not found") from exc
        slides = [s for s in inputs.get("slides") or []
                  if isinstance(s, dict) and isinstance(s.get("number"), int) and isinstance(s.get("prompt"), str)]
        raw_retry = inputs.get("retryOnly")
        retry_only = raw_retry if isinstance(raw_retry, list) else None
        brand_id = inputs.get("brandId") if isinstance(inputs.get("brandId"), str) else None
        rtl = inputs.get("language") == "ar"

        plan = await _plan(deps, owner, brand_id, slides)
        masters = plan.masters
        if retry_only is not None:
            if not deck.master_used:  # a retried slide matches its siblings: the deck's original decision
                masters = {}
        else:
            used = bool(masters)
            await storage.decks.update(owner, deck.id, DeckPatch(
                master_used=used, master_skip_reason=None if used else plan.skip_reason))

        semaphore = asyncio.Semaphore(max(1, deps.concurrency))
        spent: list[float] = []

        async def run_one(slide: dict[str, Any]) -> None:
            async with semaphore:
                number = int(slide["number"])
                if not await reserve_image_slot(deps.redis, deps.darwin):
                    await storage.slides.update_status(owner, deck.id, number,
                                                       SlideStatusPatch(status="error", error=CAP_REACHED))
                    return
                await storage.slides.update_status(owner, deck.id, number,
                                                   SlideStatusPatch(status="generating", error=None))
                kit = plan.kit
                tile = bool(plan.tile_size and kit is not None and should_tile(
                    archetype_for_type(slide.get("type")), kit.workzone, list(kit.base.furniture_archetypes or [])))
                master, kind = (None, None) if tile else resolve_master(masters, slide.get("type"))
                guide = None if tile or master is not None else (plan.wireframes or {}).get(number)
                try:
                    image = await _render(deps, slide["prompt"], tile_size=plan.tile_size if tile else None,
                                          master=master, guide=guide, rtl=rtl)
                    cost = EST_COST_MASTER if master is not None else EST_COST_PER_IMAGE
                    stored = await storage.blobs.put(owner, image.png, "image/png")
                    await storage.slides.append_version(owner, deck.id, number,
                                                        VersionCreate(mode="image", image_ref=stored.ref,
                                                                      cost_usd=cost))
                    await storage.slides.update_status(owner, deck.id, number,
                                                       SlideStatusPatch(master_kind=kind, is_tile=tile))
                except ImageGenError as exc:
                    await storage.slides.update_status(owner, deck.id, number,
                                                       SlideStatusPatch(status="error", error=exc.message))
                    return
                except Exception:  # noqa: BLE001 - the slide fails, the deck goes on (no internals shown)
                    _log.exception("generate: slide %d failed", number)
                    await storage.slides.update_status(owner, deck.id, number,
                                                       SlideStatusPatch(status="error", error=IMAGE_FAILED))
                    return
                spent.append(cost)
                await record_usage_quietly(storage.usage, owner, model=image.model, cost_usd=cost, kind=KIND_IMAGE,
                                           deck_id=deck.id, slide_number=number,
                                           idempotency_key=f"job:{job.record.id}:slide:{number}")

        await asyncio.gather(*(run_one(s) for s in slides if _wanted(retry_only, int(s["number"]))))
        rows = await storage.slides.list_for_deck(owner, deck.id)
        terminal = all(s.status in ("done", "error") for s in rows)
        await storage.decks.update(owner, deck.id, DeckPatch(status="done" if terminal else "failed"))
        return JobOutcome(result={"deckId": deck.id, "images": len(spent)}, cost_usd=sum(spent))

    return handle
