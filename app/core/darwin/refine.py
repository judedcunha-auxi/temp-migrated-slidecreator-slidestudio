"""Darwin's slide refinement and revert: `/api/refine` + the job that was `refine-background.ts` +
`_shared/refine.ts: runRefine`, and `/api/revert`.

**Refine** (`darwin.refine`): the slide's stored prompt plus the user's instruction (`prompt.steered_prompt`)
is rendered again, conditioned on the SAME brand master the slide was generated with (the deck's master
decision and the slide's master kind), and on the user's first image attachment when there is one. The new
image is appended as the slide's next version with the instruction, and made current. As in Darwin:

* the route marks the slide `generating` before it answers, so the first poll is not terminal;
* the image takes a global image slot (none left: the slide's error is Darwin's capacity text);
* an image failure marks the slide `error` with the provider's message, anything else "Refinement failed";
* the deck becomes `done` when every slide is terminal, else stays `generating` (one slide's failure never
  relabels the deck `failed`).

The job runs as its owner (D31, C12): Darwin's background function took the user id from its body.
Differences: a slide number the deck does not have is not added as a phantom slide (the port cannot create
one); the refine job fails quietly instead. The attachment is stored as a blob for the job (not carried in
the job record) and deleted after use. A refined slide is no longer a workzone tile (`is_tile` false, as
Darwin's rewritten state); its master kind is kept, where Darwin dropped it (its second refine of a title or
divider slide fell back to the Layout master).

**Revert** (`revert_slide`): the slide's current version moves to an earlier one. Its status is not changed
(Darwin changed only the database copy, never the job state `/api/status` reads).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.darwin import prompt_text as text
from app.core.darwin.caps import CAP_REACHED, reserve_image_slot
from app.core.darwin.generate import GenerationDeps, read_brand_asset
from app.core.darwin.image_gen import GeneratedImage, ImageGenError
from app.core.darwin.js import node_base64
from app.core.darwin.prompt import resolve_brand_source, steered_prompt
from app.core.darwin.usage import EST_COST_MASTER, EST_COST_PER_IMAGE, KIND_IMAGE, record_usage_quietly
from app.core.errors import ApiError
from app.core.jobs.queue import JobType
from app.core.jobs.worker import Handler, JobContext, JobOutcome, PermanentJobError
from app.core.storage.models import CallerContext, DeckPatch, SlideStatusPatch, VersionCreate
from app.core.storage.ports import NotFound, Storage

_log = logging.getLogger(__name__)

REFINE_JOB = "darwin.refine"
JOB_TYPE = JobType(REFINE_JOB, expensive=False, max_attempts=1, timeout_s=600.0)  # TODO-P5 in-flight

REFINE_FAILED = "Refinement failed"
IMAGE_FAILED = "Image generation failed"
NO_SUCH_SLIDE = "No such slide"
NO_SUCH_VERSION = "No such version"

ATTACHMENT_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
USER_IMAGE_NOTE = ("Image {n} is a reference provided by the user — incorporate its relevant visual elements or "
                   "style into the slide where they align with the refinement request.")
_ASSET_FOR_KIND = {"title": "titleMaster", "divider": "dividerMaster", "layout": "master"}


# ------------------------------------------------------------------------------------ the route
def valid_attachments(attachments: Any) -> list[dict[str, Any]]:
    """`(attachments ?? []).filter(...)`: image attachments with string data and a known media type.
    Anything else that is not an array throws in Darwin (`.filter` of a non-array): a TypeError here too."""
    if attachments is None:
        return []
    if not isinstance(attachments, list):
        raise TypeError("attachments is not an array")
    return [a for a in attachments
            if isinstance(a, dict) and a.get("type") == "image" and isinstance(a.get("data"), str)
            and (a.get("media_type") if a.get("media_type") is not None else "") in ATTACHMENT_TYPES]


def decode_attachment(data: str) -> bytes:
    """`Buffer.from(data, 'base64')`: lenient, never raises (`js.node_base64`)."""
    return node_base64(data)


async def mark_generating(storage: Storage, ctx: CallerContext, deck_id: str, number: int) -> None:
    """Refine's synchronous pre-mark: best effort, never fails the route (a missing slide is skipped)."""
    try:
        await storage.slides.update_status(ctx, deck_id, number, SlideStatusPatch(status="generating"))
    except NotFound:
        _log.info("refine: slide %d is not in the deck", number)
    except Exception:  # noqa: BLE001 - Darwin logged and went on
        _log.exception("refine: failed to pre-mark slide %d generating", number)


async def revert_slide(storage: Storage, ctx: CallerContext, deck_id: str, number: int, version: int) -> None:
    """`/api/revert` after its checks: 404 "No such slide" / "No such version", else the version is current."""
    try:
        slide = await storage.slides.get(ctx, deck_id, number)
    except NotFound as exc:
        raise ApiError(404, NO_SUCH_SLIDE) from exc
    known = {v.v for v in slide.versions} or ({1} if slide.status == "done" else set())
    if version not in known:
        raise ApiError(404, NO_SUCH_VERSION)
    try:
        await storage.slides.set_current(ctx, deck_id, number, version)
    except NotFound as exc:  # a synthesised v1 with nothing stored behind it
        raise ApiError(404, NO_SUCH_VERSION) from exc


# -------------------------------------------------------------------------------------- the job
async def _master(deps: GenerationDeps, owner: CallerContext, deck_brand: Any, kind: str | None) -> bytes | None:
    try:
        source = await resolve_brand_source(deps.storage, owner, deck_brand)
        return await read_brand_asset(deps.storage, owner, source.brand_id, _ASSET_FOR_KIND.get(kind or "layout",
                                                                                                 "master"))
    except Exception:  # noqa: BLE001 - "master unavailable, refining without it"
        _log.exception("refine: the master is unavailable; refining without it")
        return None


def refine_handler(deps: GenerationDeps) -> Handler:
    """The `darwin.refine` handler. See the module docstring."""

    async def handle(job: JobContext) -> JobOutcome:
        owner, inputs, storage = job.owner, job.record.inputs, deps.storage
        deck_id, number = str(inputs.get("deckId") or ""), inputs.get("number")
        instruction = str(inputs.get("instruction") or "")
        attachment_ref = inputs.get("attachmentRef") if isinstance(inputs.get("attachmentRef"), str) else None
        if not isinstance(number, int):
            raise PermanentJobError(NO_SUCH_SLIDE)
        try:
            try:
                deck = await storage.decks.get(owner, deck_id)
                slide = await storage.slides.get(owner, deck_id, number)
            except (NotFound, ValueError) as exc:
                raise PermanentJobError(NO_SUCH_SLIDE) from exc
            try:
                outcome = await _refine(deps, job, deck.id, deck.master_used, deck.inputs.get("brandId"),
                                        number, slide.spec.prompt, slide.master_kind, instruction, attachment_ref)
            except Exception:  # noqa: BLE001 - record a terminal error on THIS slide, never leave it generating
                _log.exception("refine: the job failed")
                try:
                    await storage.slides.update_status(owner, deck.id, number,
                                                       SlideStatusPatch(status="error", error=REFINE_FAILED))
                except Exception:  # noqa: BLE001
                    _log.exception("refine: failed to record the error on the slide")
                outcome = JobOutcome(result={"deckId": deck.id, "number": number, "error": REFINE_FAILED})
            rows = await storage.slides.list_for_deck(owner, deck.id)
            terminal = all(s.status in ("done", "error") for s in rows)
            await storage.decks.update(owner, deck.id, DeckPatch(status="done" if terminal else "generating"))
            return outcome
        finally:
            if attachment_ref:
                try:
                    await storage.blobs.delete(owner, attachment_ref)
                except Exception:  # noqa: BLE001 - best effort, as Darwin's one-shot blobs
                    _log.info("refine: could not delete the attachment blob")

    return handle


async def _refine(deps: GenerationDeps, job: JobContext, deck_id: str, master_used: bool | None, deck_brand: Any,
                  number: int, base_prompt: str, master_kind: str | None, instruction: str,
                  attachment_ref: str | None) -> JobOutcome:
    owner, storage = job.owner, deps.storage

    async def fail(error: str) -> JobOutcome:
        await storage.slides.update_status(owner, deck_id, number, SlideStatusPatch(status="error", error=error))
        return JobOutcome(result={"deckId": deck_id, "number": number, "error": error})

    await storage.slides.update_status(owner, deck_id, number, SlideStatusPatch(status="generating"))
    master = await _master(deps, owner, deck_brand, master_kind) if master_used else None
    user_image: bytes | None = None
    if attachment_ref:
        try:
            user_image = (await storage.blobs.get(owner, attachment_ref)).data or None
        except NotFound:
            user_image = None

    if not await reserve_image_slot(deps.redis, deps.darwin):
        return await fail(CAP_REACHED)

    steered = steered_prompt(base_prompt, instruction)
    images: list[bytes] = []
    edit_prompt = steered
    if master is not None:
        images.append(master)
        edit_prompt = text.MASTER_INSTRUCTION + steered
    if user_image is not None:
        images.append(user_image)
        edit_prompt = f"{edit_prompt}\n\n{USER_IMAGE_NOTE.format(n=2 if master is not None else 1)}"
    try:
        image: GeneratedImage = await (deps.images.edit(edit_prompt, images) if images
                                       else deps.images.generate(steered))
    except ImageGenError as exc:
        return await fail(exc.message)
    except Exception:  # noqa: BLE001 - shown as the slide's error, without internals
        _log.exception("refine: the image call failed")
        return await fail(IMAGE_FAILED)
    cost = EST_COST_MASTER if images else EST_COST_PER_IMAGE
    try:
        stored = await storage.blobs.put(owner, image.png, "image/png")
        version = await storage.slides.append_version(owner, deck_id, number, VersionCreate(
            mode="image", image_ref=stored.ref, instruction=instruction[:2000], cost_usd=cost))
    except Exception:  # noqa: BLE001 - writeImage failing is a render failure: no version committed
        _log.exception("refine: storing the image failed")
        return await fail(IMAGE_FAILED)
    try:
        await storage.slides.update_status(owner, deck_id, number, SlideStatusPatch(is_tile=False))
        await storage.slides.record_refinement(owner, deck_id, number)
    except Exception:  # noqa: BLE001 - the image is committed; counters are best effort
        _log.exception("refine: the post-commit updates failed")
    await record_usage_quietly(storage.usage, owner, model=image.model, cost_usd=cost, kind=KIND_IMAGE,
                               deck_id=deck_id, slide_number=number, idempotency_key=f"job:{job.record.id}")
    return JobOutcome(result={"deckId": deck_id, "number": number, "version": version.v}, cost_usd=cost)

