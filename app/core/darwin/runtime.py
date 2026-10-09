"""The runtime behind Darwin's routes: the job registry, the queue, the models, and the workers.

`DarwinRuntime` is what the routes reach through `app.state.darwin` (`app/api/deps.get_runtime`).
`build_runtime(...)` wires it from settings; tests pass their fakes (a scripted storyline model, the
fake image generator, a stub design path) and run jobs with `run_pending()`.

Job types registered here:

| Type | Enqueued by | Handler |
|---|---|---|
| `storyline` | `/api/storyline` | `app/core/storyline/job.py` |
| `slides.design_and_export` | `/api/pptx-submit`, `/api/image-to-slide` | `app/core/slides/jobs.py` |
| `exports.stitch_deck`, `brand.extract`, `masters.render_layouts` | (not yet by a Darwin route) | `app/core/slides/jobs.py` |
| `darwin.pptx_deck` | `/api/pptx-deck-submit` | `app/core/darwin/exports.py` |
| `darwin.generate` | `/api/generate`, `/api/retry` (was `generate-background`) | `app/core/darwin/generate.py` |
| `darwin.refine` | `/api/refine` (was `refine-background`) | `app/core/darwin/refine.py` |
| `darwin.quick_generate` | `/api/quick-generate` (was `quick-generate-background`) | `app/core/darwin/quick.py` |

The storyline job's `prompter` defaults to `DarwinSlidePrompter` (`app/core/darwin/prompt.py`): each slide of
a storyline result carries Darwin's image prompt, as `storyline-background.ts` built it.

Every type is registered NOT expensive: Darwin had no per-user in-flight limit, and the queue's limit
of 3 would add a 429 to its routes. TODO-P5 in-flight: Phase 5 turns it on, with the rate limits.

Workers: `WORKER_CONCURRENCY` loops run in the API process when it is above 0 (`start_workers`, from
the app's lifespan). With 0, jobs wait in Redis for a worker process running the same image.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.config.ai import AISettings, ai_settings
from app.config.darwin import DarwinSettings, darwin_settings
from app.config.engine import EngineSettings, engine_settings
from app.config.settings import Settings
from app.config.storyline import StorylineSettings, storyline_settings
from app.core.darwin import exports as darwin_exports
from app.core.darwin import generate as darwin_generate
from app.core.darwin import quick as darwin_quick
from app.core.darwin import refine as darwin_refine
from app.core.darwin.image_gen import ImageGenerator, OpenAIImageGenerator
from app.core.darwin.prompt import DarwinSlidePrompter
from app.core.darwin.usage import KIND_DESIGN, KIND_STORYLINE, record_usage_quietly
from app.core.design.context import DesignServices
from app.core.jobs.queue import JobQueue
from app.core.jobs.registry import JobRegistry
from app.core.jobs.worker import Handler, JobContext, JobOutcome, Worker
from app.core.llm.ports import ProviderResolver
from app.core.redis_client import RedisStore
from app.core.slides.jobs import PipelineDeps
from app.core.storage.factory import scratch_root
from app.core.storage.ports import Storage
from app.core.storyline import job as storyline_job
from app.core.storyline.llm_adapter import LlmStorylineModel
from app.core.storyline.ports import SlidePrompter, StorylineModel
from app.engine.renderer import PptxRenderClient, Renderer

_log = logging.getLogger(__name__)


@dataclass
class DarwinRuntime:
    queue: JobQueue
    registry: JobRegistry
    handlers: dict[str, Handler]
    storyline_model: StorylineModel
    images: ImageGenerator
    darwin: DarwinSettings
    storyline: StorylineSettings
    _stop: asyncio.Event | None = None
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def worker(self, worker_id: str | None = None) -> Worker:
        return Worker(self.queue, self.handlers, worker_id=worker_id)

    async def run_pending(self, limit: int = 50) -> int:
        """Run queued jobs in this task until none is left (tests, scripts). Returns how many ran."""
        worker = self.worker("inline")
        ran = 0
        while ran < limit and await worker.run_once():
            ran += 1
        return ran

    def start_workers(self, count: int) -> None:
        if count <= 0 or self._tasks:
            return
        self._stop = asyncio.Event()
        for n in range(count):
            worker = self.worker(f"api-{n}")
            self._tasks.append(asyncio.create_task(worker.run(self._stop), name=f"worker-{n}"))
        _log.info("jobs: started %d worker loop(s) in the API process", count)

    async def stop_workers(self) -> None:
        if self._stop is not None:
            self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()


def ledgered(handler: Handler, storage: Storage, *, kind: str, model: str) -> Handler:
    """`handler`, plus one usage-ledger row for what a successful attempt cost (C10: the cost in
    est_cost_usd, the model in model). A failed attempt's spend is on the job record only: the
    providers do not report a failed call's usage."""

    async def handle(job: JobContext) -> JobOutcome:
        outcome = await handler(job)
        if outcome.cost_usd > 0:
            deck_id = job.record.inputs.get("deckId")
            slide = job.record.inputs.get("slide")
            await record_usage_quietly(storage.usage, job.owner, model=model, cost_usd=outcome.cost_usd, kind=kind,
                                       deck_id=deck_id if isinstance(deck_id, str) else None,
                                       slide_number=slide if isinstance(slide, int) and slide >= 1 else None,
                                       idempotency_key=f"job:{job.record.id}")
        return outcome

    return handle


def _renderer_factory(engine: EngineSettings) -> Callable[[], Renderer | None]:
    if not engine.renderer_url:
        return lambda: None
    return lambda: PptxRenderClient(engine.renderer_url, timeout_s=engine.renderer_timeout_s,
                                    endpoints=engine.endpoints)


def build_runtime(
    settings: Settings,
    redis: RedisStore,
    storage: Storage,
    *,
    storyline_model: StorylineModel | None = None,
    images: ImageGenerator | None = None,
    resolve: ProviderResolver | None = None,
    services: DesignServices | None = None,
    prompter: SlidePrompter | None = None,
    ai: AISettings | None = None,
    darwin: DarwinSettings | None = None,
    storyline: StorylineSettings | None = None,
    engine: EngineSettings | None = None,
    scratch: Path | None = None,
    renderer_factory: Callable[[], Renderer | None] | None = None,
) -> DarwinRuntime:
    """Wire the runtime. Nothing here calls a model, the network or a worker: the providers and the
    OpenAI client are built lazily on first use."""
    ai = ai if ai is not None else ai_settings
    darwin = darwin if darwin is not None else darwin_settings
    storyline = storyline if storyline is not None else storyline_settings
    engine = engine if engine is not None else engine_settings
    if resolve is None:
        from app.core.llm.factory import resolver

        resolve = resolver(ai)
    model = storyline_model if storyline_model is not None else LlmStorylineModel(resolve, ai)
    prompter = prompter if prompter is not None else DarwinSlidePrompter(storage)
    image_gen = images if images is not None else OpenAIImageGenerator(darwin)

    registry = JobRegistry()
    # TODO-P5 in-flight: the storyline type is "expensive" in its own module; Darwin had no limit.
    registry.add(replace(storyline_job.job_type(storyline), expensive=False),
                 ledgered(storyline_job.make_handler(model, settings=storyline, prompter=prompter), storage,
                          kind=KIND_STORYLINE, model=storyline.model))
    deps = PipelineDeps(blobs=storage.blobs, scratch_root=scratch or scratch_root(settings), settings=ai,
                        resolve=resolve, services=services,
                        renderer_factory=renderer_factory or _renderer_factory(engine))
    darwin_exports.register(registry, deps, storage)
    design = darwin_exports.SLIDE_JOB
    registry.handlers[design] = ledgered(registry.handlers[design], storage, kind=KIND_DESIGN,
                                         model=ai.llm_design_model)

    # --- the generation batch (Phase 7a): generate/retry, refine, quick-generate ----------------------
    # The `-background` functions as internal jobs (D31), run as the job's owner. TODO-P5 in-flight: each is
    # registered NOT expensive (Darwin had no in-flight limit) and runs once (a re-run would pay again).
    generation = darwin_generate.GenerationDeps(storage=storage, redis=redis, images=image_gen, darwin=darwin)
    registry.add(darwin_generate.JOB_TYPE, darwin_generate.generation_handler(generation))
    registry.add(darwin_refine.JOB_TYPE, darwin_refine.refine_handler(generation))
    registry.add(darwin_quick.JOB_TYPE, darwin_quick.quick_handler(
        darwin_quick.QuickDeps(generation=generation, model=model, storyline=storyline)))
    # --- end of the generation batch -----------------------------------------------------------------

    queue = JobQueue(redis, storage)
    handlers = registry.install(queue)
    return DarwinRuntime(queue=queue, registry=registry, handlers=handlers, storyline_model=model,
                         images=image_gen,
                         darwin=darwin, storyline=storyline)
