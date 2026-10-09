"""The export routes end to end on the REAL pipeline: pptx-submit -> design_and_export (image mode, a scripted
design provider, the real engine in Chromium) -> pptx-result; then pptx-deck-submit -> stitch -> pptx-deck-result.

The contract harness runs these routes on a fast fake export job; this proves the inputs the routes build
are what the real job accepts. Nothing is paid: the design provider is scripted (tests/fakes/llm.py).
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from pptx import Presentation

from app.core import engine_service, preview
from tests.core.design.conftest import stub_services
from tests.core.slides.test_pipeline import designer
from tests.fakes.darwin import darwin_env
from tests.fakes.image_gen import tiny_png
from tests.fakes.llm import resolver
from tests.fakes.renderer import FakeRenderer


@pytest.fixture(scope="module", autouse=True)
def close_browsers() -> Iterator[None]:
    yield
    engine_service.shutdown()
    preview.shutdown()


def test_a_slide_and_a_deck_export_through_the_real_pipeline(tmp_path: Path) -> None:
    with darwin_env(tmp_path, fake_export=False, resolve=resolver(designer()), services=stub_services(),
                    renderer_factory=FakeRenderer) as env:
        deck = env.seed_deck("alice", slides=2)
        jobs = []
        for n in ("1", "2"):
            r = env.client.post("/api/pptx-submit", json={"deckId": deck, "slide": n}, headers=env.auth("alice"))
            assert r.status_code == 200, r.text
            jobs.append(r.json()["jobId"])
        assert env.run_jobs() == 2
        for job_id in jobs:
            status = env.client.get(f"/api/pptx-status?jobId={job_id}", headers=env.auth("alice")).json()
            assert status == {"status": "done", "done": True, "failed": False}, env.job("alice", job_id).error
        slide = env.client.get(f"/api/pptx-result?jobId={jobs[0]}", headers=env.auth("alice"))
        assert slide.status_code == 200 and len(Presentation(io.BytesIO(slide.content)).slides) == 1

        r = env.client.post("/api/pptx-deck-submit", json={"deckId": deck, "jobIds": jobs, "presentationTitle": "Q3"},
                            headers=env.auth("alice"))
        deck_job = r.json()["deckJobId"]
        assert env.run_jobs() == 1
        assert env.client.get(f"/api/pptx-deck-status?deckJobId={deck_job}", headers=env.auth("alice")).json()["done"]
        stitched = env.client.get(f"/api/pptx-deck-result?deckJobId={deck_job}", headers=env.auth("alice"))
        assert stitched.headers["content-disposition"] == 'attachment; filename="deck.pptx"'
        assert len(Presentation(io.BytesIO(stitched.content)).slides) == 2

        # image-to-slide runs the same job in image mode, polled through pptx-status
        r = env.client.post("/api/image-to-slide", files={"file": ("x.png", tiny_png(320, 180), "image/png")},
                            headers=env.auth("alice"))
        assert r.status_code == 202
        env.run_jobs()
        assert env.client.get(f"/api/pptx-status?jobId={r.json()['jobId']}",
                              headers=env.auth("alice")).json()["done"] is True
