"""Deck fixtures for the generation routes' tests (generate, status, refine, media, quick).

Built on `DarwinEnv` (tests/fakes/darwin.py), through the routes where that is the point and through the
store where a state is only a precondition:

    deck = generated_deck(env, "alice")            # POST /api/generate + the job: slides done, images stored
    empty = empty_deck(env, "alice")               # a deck with no slides: Darwin's "no job state yet"
    payload = storyline_payload(slides=3)          # a valid /api/generate body
    brand = tile_brand(env, "alice")               # a brand with a workzone, furniture and a Layout master
"""

from __future__ import annotations

from typing import Any

from app.core.storage.models import BrandCreate, DeckCreate, SlideStatusPatch, VersionCreate
from tests.fakes.darwin import DarwinEnv
from tests.fakes.image_gen import tiny_png

UNKNOWN = "3fa85f64-5717-4562-b3fc-2c963f66afa6"

#: A content slide that passes Darwin's schema (3-4 bullets) and a framework from the library.
CONTENT_SLIDE = {"title": "Margins grew 4 pts on pricing", "type": "framework", "framework": "value chain",
                 "description": "Pricing drove most of the gain.", "bullets": ["Price +3%", "Mix +1 pt", "Costs flat"]}


def storyline_payload(slides: int = 2, **extra: Any) -> dict[str, Any]:
    """A valid `/api/generate` body: a title slide, then content slides."""
    body: list[dict[str, Any]] = [{"number": 1, "title": "Q3 Results", "type": "title", "framework": "title slide",
                                   "description": "Opening", "bullets": []}]
    body += [{"number": n, **CONTENT_SLIDE} for n in range(2, slides + 1)]
    return {"presentationTitle": "Q3 2026 Financial Results", "slides": body, "inputs": {"topic": "Q3"}, **extra}


def storyline_reply(slides: int = 2) -> dict[str, Any]:
    """What the scripted storyline model answers for a quick job (no `inputs`)."""
    payload = storyline_payload(slides)
    return {"presentationTitle": payload["presentationTitle"], "slides": payload["slides"]}


def generated_deck(env: DarwinEnv, subject: str, *, slides: int = 2, run: bool = True, **extra: Any) -> str:
    env.user(subject)
    r = env.client.post("/api/generate", json=storyline_payload(slides, **extra), headers=env.auth(subject))
    assert r.status_code == 202, r.text
    if run:
        env.run_jobs()
    return str(r.json()["deckId"])


def empty_deck(env: DarwinEnv, subject: str, title: str = "Empty") -> str:
    env.user(subject)

    async def create() -> str:
        return (await env.store.decks.create(env.ctx(subject), DeckCreate(title=title), [])).id

    return env.call(create)


def put_version(env: DarwinEnv, subject: str, deck_id: str, number: int, data: bytes | None = None,
                instruction: str | None = None) -> int:
    """Append an image version (a PNG by default) to a slide; returns its number."""

    async def run() -> int:
        ctx = env.ctx(subject)
        ref = (await env.store.blobs.put(ctx, data if data is not None else tiny_png(), "image/png")).ref
        version = await env.store.slides.append_version(ctx, deck_id, number,
                                                        VersionCreate(mode="image", image_ref=ref,
                                                                      instruction=instruction))
        return version.v

    return env.call(run)


def set_slide(env: DarwinEnv, subject: str, deck_id: str, number: int, **patch: Any) -> None:
    env.call(env.store.slides.update_status, env.ctx(subject), deck_id, number, SlideStatusPatch(**patch))


def slide(env: DarwinEnv, subject: str, deck_id: str, number: int) -> Any:
    return env.call(env.store.slides.get, env.ctx(subject), deck_id, number)


def tile_brand(env: DarwinEnv, subject: str, *, master: bytes | None = None) -> str:
    """A brand whose content slides render as workzone tiles, with a Layout master PNG (16:9)."""
    env.user(subject)
    kit = {"primaryColor": "#123456", "accentColor": "#abcdef", "masterKey": "master",
           "workzone": {"left": 0.05, "top": 0.2, "width": 0.9, "height": 0.7}, "furnitureArchetypes": ["content"]}

    async def create() -> str:
        ctx = env.ctx(subject)
        brand = await env.store.brands.create(ctx, BrandCreate(name="Tiles", kit=kit))
        await env.store.brands.put_asset(ctx, brand.id, "master", master or tiny_png(32, 18, (200, 30, 30)),
                                         "image/png")
        return brand.id

    return env.call(create)


def memo(env: DarwinEnv) -> dict[str, Any]:
    """A scratch dict on the environment, for a probe's `run` to hand ids to its `check`."""
    return env.__dict__.setdefault("_memo", {})  # type: ignore[no-any-return]


def fill_image_cap(env: DarwinEnv) -> None:
    """Use up today's global image slots (`caps.reserve_image_slot` then refuses every image)."""
    from datetime import UTC, datetime

    from app.core.darwin.caps import IMAGE_KEY_PREFIX

    redis = env.runtime.queue._redis  # the runtime's Redis (fakeredis in tests)
    env.call(redis.set, IMAGE_KEY_PREFIX + datetime.now(UTC).date().isoformat(), "1000000")
