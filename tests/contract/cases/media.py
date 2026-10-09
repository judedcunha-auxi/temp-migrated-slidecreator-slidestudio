"""Contract cases: /api/image, /api/pdf, /api/pdf-deck (data/api-{image,pdf,pdf-deck}.json)."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image

from tests.contract.cases.common import add_auth, broken
from tests.contract.harness import Probe, RouteCases
from tests.fakes.darwin import DarwinEnv
from tests.fakes.darwin_decks import (
    UNKNOWN,
    empty_deck,
    generated_deck,
    memo,
    put_version,
    set_slide,
    tile_brand,
)
from tests.fakes.image_gen import tiny_png


def get(path: str, subject: str = "alice", method: str = "GET") -> Any:
    return lambda env: env.client.request(method, path, headers=env.auth(subject))


def with_deck(template: str, *, subject: str = "alice", refined: bool = False, slides: int = 2,
              method: str = "GET") -> Any:
    """GET `template` with `{deck}` filled with a fresh seeded deck of alice's."""
    def run(env: DarwinEnv) -> Any:
        deck_id = env.seed_deck("alice", slides=slides, refined=refined)
        memo(env)["deck"] = deck_id
        return env.client.request(method, template.format(deck=deck_id), headers=env.auth(subject))
    return run


def _png_of(env: DarwinEnv, r: Any) -> None:
    assert r.content.startswith(b"\x89PNG")


def _size(expected: tuple[int, int]) -> Any:
    def check(_env: DarwinEnv, r: Any) -> None:
        with Image.open(io.BytesIO(r.content)) as img:
            assert img.size == expected
    return check


# ---------------------------------------------------------------------------------------------- image
image = RouteCases("/api/image")
add_auth(image, "GET", f"/api/image?deckId={UNKNOWN}&slide=1")


def _refined_picture(env: DarwinEnv) -> Any:
    deck_id = env.seed_deck("alice", slides=1)
    put_version(env, "alice", deck_id, 1, tiny_png(20, 10, (0, 200, 0)), instruction="greener")
    return env.client.get(f"/api/image?deckId={deck_id}&slide=1&v=2", headers=env.auth("alice"))


def _tile(env: DarwinEnv) -> Any:
    brand = tile_brand(env, "alice")
    deck_id = generated_deck(env, "alice", slides=2, inputs={"topic": "Q3", "brandId": brand})
    return env.client.get(f"/api/image?deckId={deck_id}&slide=2", headers=env.auth("alice"))


def _tile_check(env: DarwinEnv, r: Any) -> None:
    _size((2560, 1440))(env, r)  # the tile composited on the brand master
    assert any(c.size == "2304x1008" for c in env.images.calls)  # slide 2 rendered at the workzone's size


image.add("R0", Probe("the original picture", with_deck("/api/image?deckId={deck}&slide=1"), _size((16, 9))),
          Probe("a refinement (v=2)", _refined_picture, _size((20, 10))),
          Probe("v=1, v=0, v=abc and v=1.5 all read the original",
                with_deck("/api/image?deckId={deck}&slide=1&v=abc&variant=A", refined=True), _size((16, 9))),
          Probe("slide=' 1.0' is Number(' 1.0') = 1", with_deck("/api/image?deckId={deck}&slide=%201.0")),
          Probe("POST acts as GET", with_deck("/api/image?deckId={deck}&slide=2", method="POST"), _png_of),
          Probe("a content tile is composited on the brand's master (2560x1440)", _tile, _tile_check))
image.add("E400 Bad params", Probe("no deckId", get("/api/image?slide=1")),
          Probe("no slide", get(f"/api/image?deckId={UNKNOWN}")),
          Probe("slide=0", get(f"/api/image?deckId={UNKNOWN}&slide=0")),
          Probe("slide=1.5", get(f"/api/image?deckId={UNKNOWN}&slide=1.5")),
          Probe("slide=abc", get(f"/api/image?deckId={UNKNOWN}&slide=abc")),
          Probe("empty deckId", get("/api/image?deckId=&slide=1")))
image.add("E403 Not your deck", Probe("unknown uuid", get(f"/api/image?deckId={UNKNOWN}&slide=1")),
          Probe("someone else's deck", with_deck("/api/image?deckId={deck}&slide=1", subject="bob")))


def _generating(env: DarwinEnv) -> Any:
    deck_id = generated_deck(env, "alice", run=False)
    return env.client.get(f"/api/image?deckId={deck_id}&slide=1", headers=env.auth("alice"))


image.add("E404 Image not found", Probe("a slide past the deck", with_deck("/api/image?deckId={deck}&slide=9")),
          Probe("a version never rendered", with_deck("/api/image?deckId={deck}&slide=1&v=3")),
          Probe("still generating", _generating))
image.add("E500 Internal error", Probe("non-uuid deckId", get("/api/image?deckId=deck-1&slide=1")),
          Probe("storage down", lambda env: (broken(env, "decks", "get"),
                                             get(f"/api/image?deckId={UNKNOWN}&slide=1")(env))[1]))

# ------------------------------------------------------------------------------------------------ pdf
pdf = RouteCases("/api/pdf")
add_auth(pdf, "GET", f"/api/pdf?deckId={UNKNOWN}&slide=1")


def _pdf_check(name: str) -> Any:
    def check(env: DarwinEnv, r: Any) -> None:
        assert r.content.startswith(b"%PDF") and b"/MediaBox [0 0 16 9]" in r.content
        assert r.headers["content-disposition"] == f'attachment; filename="{name}"'
        assert "cache-control" not in r.headers  # every download is counted
        exports = env.call(env.store.exports.list_for_deck, env.ctx("alice"), memo(env)["deck"])
        assert [e.kind for e in exports] == ["pdf"]
    return check


pdf.add("R0", Probe("slide 1", with_deck("/api/pdf?deckId={deck}&slide=1"), _pdf_check("slide_1.pdf")),
        Probe("slide=01 is slide_1.pdf", with_deck("/api/pdf?deckId={deck}&slide=01"), _pdf_check("slide_1.pdf")),
        Probe("v=2 of a refined slide", with_deck("/api/pdf?deckId={deck}&slide=2&v=2", refined=True),
              _pdf_check("slide_2.pdf")))
pdf.add("E400 Bad params", Probe("no deckId", get("/api/pdf?slide=1")),
        Probe("slide=-1", get(f"/api/pdf?deckId={UNKNOWN}&slide=-1")))
pdf.add("E403 Not your deck", Probe("unknown uuid", get(f"/api/pdf?deckId={UNKNOWN}&slide=1")),
        Probe("someone else's deck", with_deck("/api/pdf?deckId={deck}&slide=1", subject="bob")))
pdf.add("E404 Image not found", Probe("a slide past the deck", with_deck("/api/pdf?deckId={deck}&slide=7")),
        Probe("a version never rendered", with_deck("/api/pdf?deckId={deck}&slide=1&v=4")))


def _not_png(env: DarwinEnv) -> Any:
    deck_id = env.seed_deck("alice", slides=1)
    put_version(env, "alice", deck_id, 1, b"GIF89a not a png")
    return env.client.get(f"/api/pdf?deckId={deck_id}&slide=1&v=2", headers=env.auth("alice"))


pdf.add("E500 Internal error", Probe("non-uuid deckId", get("/api/pdf?deckId=deck-1&slide=1")),
        Probe("the stored picture is not a PNG (embedPng threw)", _not_png))

# ------------------------------------------------------------------------------------------- pdf-deck
deck_pdf = RouteCases("/api/pdf-deck")
add_auth(deck_pdf, "GET", f"/api/pdf-deck?deckId={UNKNOWN}")


def _deck_pdf_check(env: DarwinEnv, r: Any) -> None:
    assert r.content.startswith(b"%PDF") and r.content.count(b"/Type /Page ") == 3
    assert r.headers["content-disposition"] == 'attachment; filename="Seeded.pdf"'
    assert "cache-control" not in r.headers


def _titled(env: DarwinEnv) -> Any:
    deck_id = generated_deck(env, "alice", slides=2)
    return env.client.get(f"/api/pdf-deck?deckId={deck_id}", headers=env.auth("alice"))


def _title_check(_env: DarwinEnv, r: Any) -> None:
    # "Q3 2026 Financial Results" keeps letters, digits, spaces; an error slide would be skipped
    assert r.headers["content-disposition"] == 'attachment; filename="Q3 2026 Financial Results.pdf"'


def _partly_done(env: DarwinEnv) -> Any:
    deck_id = env.seed_deck("alice", slides=3)
    set_slide(env, "alice", deck_id, 2, status="error", error="boom")
    return env.client.get(f"/api/pdf-deck?deckId={deck_id}", headers=env.auth("alice"))


def _two_pages(_env: DarwinEnv, r: Any) -> None:
    assert r.content.count(b"/Type /Page ") == 2


deck_pdf.add("R0", Probe("every done slide", with_deck("/api/pdf-deck?deckId={deck}", slides=3), _deck_pdf_check),
             Probe("the filename from the title", _titled, _title_check),
             Probe("an errored slide is left out", _partly_done, _two_pages))
deck_pdf.add("E400 Missing deckId", Probe("no deckId", get("/api/pdf-deck")),
             Probe("empty deckId", get("/api/pdf-deck?deckId=")))
deck_pdf.add("E403 Not your deck", Probe("unknown uuid", get(f"/api/pdf-deck?deckId={UNKNOWN}")),
             Probe("someone else's deck", with_deck("/api/pdf-deck?deckId={deck}", subject="bob")))
deck_pdf.add("E404 Deck not found", Probe("a deck with no job state", lambda env: env.client.get(
    f"/api/pdf-deck?deckId={empty_deck(env, 'alice')}", headers=env.auth("alice"))))


def _nothing_done(env: DarwinEnv) -> Any:
    deck_id = generated_deck(env, "alice", run=False)
    return env.client.get(f"/api/pdf-deck?deckId={deck_id}", headers=env.auth("alice"))


deck_pdf.add("E400 No finished slides to export", Probe("nothing generated yet", _nothing_done))


def _pictures_gone(env: DarwinEnv) -> Any:
    deck_id = env.seed_deck("alice", slides=2)

    async def drop() -> None:
        ctx = env.ctx("alice")
        for s in await env.store.slides.list_for_deck(ctx, deck_id):
            for v in s.versions:
                await env.store.blobs.delete(ctx, str(v.image_ref))

    env.call(drop)
    return env.client.get(f"/api/pdf-deck?deckId={deck_id}", headers=env.auth("alice"))


deck_pdf.add("E404 No slide images found", Probe("done slides whose pictures are gone", _pictures_gone))
deck_pdf.add("E500 Internal error", Probe("non-uuid deckId", get("/api/pdf-deck?deckId=deck-1")))

ROUTES = [image, pdf, deck_pdf]
