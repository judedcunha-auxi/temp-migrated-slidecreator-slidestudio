"""`DarwinEnv`: the whole service on fakes, for Darwin route tests and the contract harness.

    with darwin_env(tmp_path) as env:
        alice = env.user("alice")                       # provisions the profile, returns its subject
        r = env.client.post("/api/storyline", json={...}, headers=env.auth("alice"))
        env.run_jobs()                                  # the worker runs what the route queued
        deck = env.seed_deck("alice", slides=2)         # a deck with one image version per slide

What it wires (no network, no paid call, no Redis server):

* the app from `create_app()`, on fakeredis and the General service fake (`env.store`);
* a real token check against a local issuer (`env.issuer`, tests/fakes/identity.py);
* the Darwin runtime with a scripted storyline model (`env.model`), the fake image generator
  (`env.images`) and, by default, a FAST fake for the slide export job (`fake_export=True`): it stores
  a tiny .pptx and the result shape `design_and_export` returns, without Chromium or a design turn.
  `fake_export=False` runs the real pipeline on the scripted design provider (slow: a browser).

Async calls into the store or the queue go through `env.call(...)`, on the app's own event loop.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import fakeredis
from fastapi.testclient import TestClient
from pptx import Presentation

from app.config.storyline import StorylineSettings
from app.core.darwin import exports as darwin_exports
from app.core.darwin.exports import deck_handler
from app.core.darwin.jobs import PPTX_CONTENT_TYPE
from app.core.darwin.runtime import DarwinRuntime, build_runtime
from app.core.jobs.worker import JobContext, JobOutcome, PermanentJobError
from app.core.redis_client import RedisClient
from app.core.slides.jobs import PipelineDeps
from app.core.storage.models import BrandCreate, CallerContext, DeckCreate, SlideSpec, VersionCreate
from app.core.storage.ports import NotFound
from app.main import create_app
from tests.conftest import build_ai_settings, build_darwin_settings, build_settings
from tests.fakes.general_service import FakeGeneralService
from tests.fakes.identity import FakeIssuer
from tests.fakes.image_gen import FakeImageGenerator, tiny_png
from tests.fakes.storyline_model import ScriptedStorylineModel

T = TypeVar("T")


def tiny_pptx(slides: int = 1) -> bytes:
    prs = Presentation()
    for _ in range(slides):
        prs.slides.add_slide(prs.slide_layouts[6])
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


@dataclass
class DarwinEnv:
    client: TestClient
    store: FakeGeneralService
    issuer: FakeIssuer
    runtime: DarwinRuntime
    model: ScriptedStorylineModel
    images: FakeImageGenerator

    # ------------------------------------------------------------------ plumbing
    def call(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        async def run() -> T:
            return await fn(*args, **kwargs)

        assert self.client.portal is not None
        return self.client.portal.call(run)

    def run_jobs(self) -> int:
        return self.call(self.runtime.run_pending)

    # --------------------------------------------------------------------- users
    def ctx(self, subject: str) -> CallerContext:
        return CallerContext(subject=subject, request_id="req-test")

    def user(self, subject: str, *, email: str | None = None, admin: bool = False) -> str:
        """Provision a user (as the first authenticated request would); returns the profile id."""
        _, profile = self.call(self.store.make_user, subject, email=email, admin=admin)
        return profile.id

    def auth(self, subject: str, **token: Any) -> dict[str, str]:
        return self.issuer.headers(subject, **token)

    # --------------------------------------------------------------------- decks
    def seed_deck(self, subject: str, *, slides: int = 1, types: list[str] | None = None,
                  inputs: dict[str, Any] | None = None, refined: bool = False) -> str:
        """A deck owned by `subject` with `slides` slides, each with an image version v1 (and v2 when
        `refined`). Returns the deck id."""
        self.user(subject)
        ctx = self.ctx(subject)
        kinds = types or ["content"] * slides
        story = {"presentationTitle": "Seeded", "slides": [
            {"number": n + 1, "title": f"Slide {n + 1}", "type": kinds[n], "framework": "", "description": "",
             "bullets": []} for n in range(slides)]}
        specs = [SlideSpec(number=n + 1, title=f"Slide {n + 1}", type=kinds[n]) for n in range(slides)]

        async def seed() -> str:
            deck = await self.store.decks.create(ctx, DeckCreate(title="Seeded", inputs=inputs or {"topic": "t"},
                                                                 storyline=story), specs)
            for n in range(1, slides + 1):
                for _ in range(2 if refined else 1):
                    ref = (await self.store.blobs.put(ctx, tiny_png(), "image/png")).ref
                    await self.store.slides.append_version(ctx, deck.id, n, VersionCreate(mode="image", image_ref=ref))
            return deck.id

        return self.call(seed)

    def job(self, subject: str, job_id: str) -> Any:
        return self.call(self.store.jobs.get, self.ctx(subject), job_id)

    # ------------------------------------------------------------- brands (feature/darwin-brands)
    def seed_brand(self, owner: str, *, name: str = "Acme", kit: dict[str, Any] | None = None, org: bool = False,
                   domain: str | None = None, assets: dict[str, bytes] | None = None) -> str:
        """A brand and its assets (asset name -> bytes). `org=True`: an org brand created by `owner`, who
        is made an admin; `domain` maps an email domain to it. Returns the brand id."""
        self.user(owner, admin=org)
        ctx = self.ctx(owner)

        async def seed() -> str:
            data = BrandCreate(name=name, kit=kit or {})
            brand = await (self.store.brands.create_org(ctx, data) if org else self.store.brands.create(ctx, data))
            if domain:
                await self.store.orgs.map_domain(ctx, domain, brand.id)
            for asset, content in (assets or {}).items():
                await self.store.brands.put_asset(ctx, brand.id, asset, content, _asset_type(asset, content))
            return brand.id

        return self.call(seed)

    def member(self, subject: str, email: str) -> dict[str, str]:
        """Auth headers for a user whose VERIFIED email puts them in an org brand's domain."""
        self.user(subject, email=email)
        return self.auth(subject, email=email)

    def brand(self, subject: str, brand_id: str) -> Any:
        return self.call(self.store.brands.get, self.ctx(subject), brand_id).brand

    def asset(self, subject: str, brand_id: str, name: str) -> bytes | None:
        async def read() -> bytes | None:
            try:
                return (await self.store.brands.get_asset(self.ctx(subject), brand_id, name)).data
            except NotFound:
                return None

        return self.call(read)


PNG_MAGIC = bytes([0x89]) + b"PNG"


def _asset_type(name: str, content: bytes) -> str:
    if content[:4] == PNG_MAGIC:
        return "image/png"
    if content[:4] == b"%PDF":
        return "application/pdf"
    return "application/json" if name.startswith("furniture") else "application/octet-stream"


def fake_export_handler(store: FakeGeneralService) -> Callable[[JobContext], Awaitable[JobOutcome]]:
    """Stands in for `slides.design_and_export` (and so for the stitch's parts): stores a 1-slide .pptx
    and a design-deck stand-in, and answers the real handler's result shape. Fails like the real job
    when the input names no image or spec."""

    async def handle(job: JobContext) -> JobOutcome:
        inputs = job.record.inputs
        if not inputs.get("imageRef") and not inputs.get("spec"):
            raise PermanentJobError("Give exactly one of a spec or an image.")
        if inputs.get("imageRef"):
            await store.blobs.get(job.owner, str(inputs["imageRef"]))  # the image is readable by the owner
        pptx = await store.blobs.put(job.owner, tiny_pptx(), PPTX_CONTENT_TYPE)
        deck = await store.blobs.put(job.owner, b"PK\x05\x06" + b"\x00" * 18, "application/zip")
        return JobOutcome(result={"slideId": "sld_fake", "pptxRef": pptx.ref, "deckRef": deck.ref,
                                  "elementCount": 1, "reviewFlagCount": 0, "costUsd": 0.0})

    return handle


def fake_stitch_handler(store: FakeGeneralService) -> Callable[[JobContext], Awaitable[JobOutcome]]:
    async def handle(job: JobContext) -> JobOutcome:
        parts = job.record.inputs.get("parts") or []
        pptx = await store.blobs.put(job.owner, tiny_pptx(len(parts)), PPTX_CONTENT_TYPE)
        return JobOutcome(result={"pptxRef": pptx.ref, "slideCount": len(parts)})

    return handle


@contextmanager
def darwin_env(tmp_path: Path, *, model_steps: list[Any] | None = None, fake_export: bool = True,
               storyline: StorylineSettings | None = None, **runtime_kwargs: Any) -> Iterator[DarwinEnv]:
    store = FakeGeneralService()
    redis = RedisClient(fakeredis.FakeAsyncRedis(decode_responses=True))
    issuer = FakeIssuer()
    model = ScriptedStorylineModel(*(model_steps or []))
    images = FakeImageGenerator()
    settings = build_settings(auth_issuer=issuer.issuer, auth_audience=issuer.audience)
    runtime = build_runtime(settings, redis, store, storyline_model=model, images=images,
                            ai=build_ai_settings(), darwin=build_darwin_settings(),
                            storyline=storyline or StorylineSettings(_env_file=None),  # type: ignore[call-arg]
                            scratch=tmp_path, **runtime_kwargs)
    if fake_export:
        deps = PipelineDeps(blobs=store.blobs, scratch_root=tmp_path, settings=build_ai_settings())
        runtime.handlers[darwin_exports.SLIDE_JOB] = fake_export_handler(store)
        # The deck job resolves its parts for real; only the stitch itself is faked.
        runtime.handlers[darwin_exports.DECK_JOB] = deck_handler(deps, store, stitch=fake_stitch_handler(store))
    app = create_app(settings, redis, store)
    app.state.darwin = runtime
    app.state.token_verifier = issuer.verifier()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield DarwinEnv(client=client, store=store, issuer=issuer, runtime=runtime, model=model, images=images)
