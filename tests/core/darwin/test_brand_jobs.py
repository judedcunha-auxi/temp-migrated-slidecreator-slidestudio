"""The brand jobs end to end on fakes (feature/darwin-brands): the .pptx import (`darwin.brand_pptx`), the
guidelines extraction (`darwin.brand_guidelines`) and the preview (`darwin.brand_preview`). They were
Darwin's unauthenticated `-background` functions (C12); here they run as the submitting user."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.core.darwin import brand_preview
from app.core.darwin.image_gen import ImageGenError
from app.core.storage.ports import NotFound
from app.core.storyline.ports import ModelUnavailable
from tests.contract.cases.brands import B64_PDF, PDF, fake_layout_renderer
from tests.fakes.darwin import DarwinEnv, darwin_env, tiny_pptx
from tests.fakes.image_gen import tiny_png


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 4), (200, 10, 10)).save(out, format="JPEG")
    return out.getvalue()


def _import(env: DarwinEnv, brand_id: str | None, data: bytes | None = None) -> str:
    fields = {"brandId": brand_id} if brand_id else {}
    r = env.client.post("/api/brand-pptx", headers=env.auth("alice"), data=fields,
                        files={"file": ("t.pptx", data or tiny_pptx(), "application/octet-stream")})
    assert r.status_code == 202, r.text
    return str(r.json()["jobId"])


def _status(env: DarwinEnv, route: str, job_id: str) -> dict[str, Any]:
    r = env.client.get(f"/api/{route}?jobId={job_id}", headers=env.auth("alice"))
    assert r.status_code == 200, r.text
    return dict(r.json())


# ---------------------------------------------------------------------------------- .pptx import
def test_pptx_import_extracts_the_logo_as_png_and_deletes_the_upload(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    def extract(data: bytes, *, capture_all_layouts: bool = False, **_: Any) -> dict[str, Any]:
        assert capture_all_layouts
        return {"headingFont": "Georgia", "bodyFont": None, "layouts": [], "workzone": {"left": 0, "top": 0.1,
                                                                                         "right": 1, "bottom": 0.9},
                "masterImages": [{"role": "logo_mark", "imageBase64": base64.b64encode(_jpeg()).decode()}],
                "capturedFurniture": None}

    monkeypatch.setattr("app.core.brand.extract.extract", extract)
    with darwin_env(tmp_path) as env:
        fake_layout_renderer(env)
        brand_id = env.seed_brand("alice")
        job_id = _import(env, brand_id)
        upload_ref = env.job("alice", job_id).inputs["pptxRef"]
        env.run_jobs()
        fields = _status(env, "brand-pptx-status", job_id)["fields"]
        owner = env.brand("alice", brand_id).owner_id
        assert fields["logoKey"] == f"brand/{owner}/{brand_id}/logo.png"
        assert fields["headingFont"] == "Georgia" and "bodyFont" not in fields  # null is left out
        assert fields["workzone"] == {"left": 0.0, "top": 0.1, "width": 1.0, "height": 0.8}
        assert (env.asset("alice", brand_id, "logo") or b"")[:4] == b"\x89PNG"  # the JPEG converted
        with pytest.raises(NotFound):
            env.call(env.store.blobs.get, env.ctx("alice"), upload_ref)  # Darwin deleted the temp upload too


def test_pptx_import_rechecks_edit_access_when_it_runs(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        brand_id = env.seed_brand("alice")
        job_id = _import(env, brand_id)
        assert env.client.delete(f"/api/brands?id={brand_id}", headers=env.auth("alice")).status_code == 200
        env.run_jobs()
        assert _status(env, "brand-pptx-status", job_id) == {"status": "error", "error": "Brand not found"}


def test_pptx_import_without_a_renderer_still_imports(tmp_path: Path) -> None:
    """Layout previews are best-effort: no PptxRender configured means no previews, not a failed import."""
    with darwin_env(tmp_path, renderer_factory=lambda: None) as env:
        brand_id = env.seed_brand("alice")
        job_id = _import(env, brand_id)
        env.run_jobs()
        body = _status(env, "brand-pptx-status", job_id)
        assert body["status"] == "done" and "layoutPreviews" not in body["fields"]
        assert body["fields"]["furnitureArchetypes"]  # the furniture still came through


# ---------------------------------------------------------------------------------- guidelines
def test_guidelines_extraction_is_ledgered_and_the_parked_pdf_deleted(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        env.model.steps.append({"company": "Acme"})
        r = env.client.post("/api/brand-extract", headers=env.auth("alice"), json={"b64": B64_PDF})
        job_id = r.json()["jobId"]
        pdf_ref = env.job("alice", job_id).inputs["pdfRef"]
        assert env.call(env.store.blobs.get, env.ctx("alice"), pdf_ref).data == PDF
        env.run_jobs()
        assert _status(env, "brand-extract-status", job_id) == {"status": "done", "fields": {"company": "Acme"}}
        with pytest.raises(NotFound):
            env.call(env.store.blobs.get, env.ctx("alice"), pdf_ref)
        day = env.store.clock.now.date()
        spend = env.call(env.store.usage.spend_for_user, env.ctx("alice"), day)
        assert spend.events == 1 and spend.cost_usd > 0
        assert env.model.requests[0].purpose == "brand_guidelines"


def test_guidelines_retries_an_unavailable_model_then_gives_darwins_text(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        env.model.steps.extend([ModelUnavailable("overloaded"), ModelUnavailable("overloaded")])
        brand_id = env.seed_brand("alice")
        r = env.client.post("/api/brand-extract", headers=env.auth("alice"), json={"b64": B64_PDF, "brandId": brand_id})
        job_id = r.json()["jobId"]
        env.run_jobs()
        assert _status(env, "brand-extract-status", job_id) == {"status": "error", "error": "Extraction failed"}
        assert env.model.calls == 2
        assert env.asset("alice", brand_id, "guidelines") == PDF  # a brand's guidelines are kept


def test_the_guidelines_cap_is_off_by_default_and_a_429_when_on(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        for _ in range(3):
            assert env.client.post("/api/brand-extract", headers=env.auth("alice"),
                                   json={"b64": B64_PDF}).status_code == 202
        env.runtime.darwin = env.runtime.darwin.model_copy(update={"brand_extract_cap_enabled": True,
                                                                   "brand_extracts_per_user_per_day": 1})
        assert env.client.post("/api/brand-extract", headers=env.auth("alice"), json={"b64": B64_PDF}).status_code == 202
        r = env.client.post("/api/brand-extract", headers=env.auth("alice"), json={"b64": B64_PDF})
        assert (r.status_code, r.json()) == (429, {"error": "Daily guidelines extraction limit reached, try again "
                                                            "tomorrow."})


# ------------------------------------------------------------------------------------- preview
def _preview(env: DarwinEnv, brand_id: str) -> dict[str, Any]:
    r = env.client.post("/api/brand-preview", headers=env.auth("alice"), json={"brandId": brand_id})
    assert r.status_code == 202, r.text
    env.run_jobs()
    return _status(env, "brand-preview-status", str(r.json()["jobId"]))


KIT = {"primaryColor": "#0B2D4F", "accentColor": "#E94E1B", "company": "Acme", "styleTemplate": "Use {primaryColor}.",
       "typographyScale": {"title": {"sizePt": 28, "bold": True}},
       "allColors": [{"role": "accent1", "hex": "#0B2D4F"}, {"role": "accent2", "hex": "#123456"}]}


def test_preview_generates_once_then_serves_the_cache(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        brand_id = env.seed_brand("alice", kit=KIT)
        first = _preview(env, brand_id)
        assert first["status"] == "done" and base64.b64decode(first["image"]) == tiny_png()
        prompt = env.images.calls[0].prompt
        assert "Use #0B2D4F." in prompt and "Company reference: Acme" in prompt
        assert "headings at 28pt bold" in prompt and "#123456 (accent2)" in prompt and "#0B2D4F (accent1)" not in prompt
        assert prompt.rstrip().endswith("brace/bracket grouping shapes.")
        assert _preview(env, brand_id)["status"] == "done"
        assert len(env.images.calls) == 1  # unchanged kit: cached, no image slot spent
        env.client.patch("/api/brands", headers=env.auth("alice"), json={"brandId": brand_id,
                                                                         "kit": {"accentColor": "#000000"}})
        assert _preview(env, brand_id)["status"] == "done" and len(env.images.calls) == 2
        spend = env.call(env.store.usage.spend_for_user, env.ctx("alice"), env.store.clock.now.date())
        assert spend.events == 2 and spend.cost_usd == pytest.approx(0.16)  # 2 x EST_COST_PER_IMAGE


def test_preview_with_a_master_edits_it(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        brand_id = env.seed_brand("alice", kit={"masterKey": "brand/x/y/master.png"}, assets={"master": tiny_png()})
        assert _preview(env, brand_id)["status"] == "done"
        call = env.images.calls[0]
        assert call.kind == "edit" and call.images == 1 and call.prompt.startswith(brand_preview.MASTER_INSTRUCTION)


def test_preview_cap_and_provider_errors_are_job_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with darwin_env(tmp_path) as env:
        brand_id = env.seed_brand("alice")
        env.images.fail_with = ImageGenError(400, "Your request was rejected by the safety system.")
        assert _preview(env, brand_id) == {"status": "error",
                                           "error": "Your request was rejected by the safety system."}

        async def no_slot(*_a: Any, **_k: Any) -> bool:
            return False

        monkeypatch.setattr(brand_preview, "reserve_image_slot", no_slot)
        env.images.fail_with = None
        assert _preview(env, brand_id) == {"status": "error",
                                           "error": "Daily image limit reached — try again tomorrow."}
