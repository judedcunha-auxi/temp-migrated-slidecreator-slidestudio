"""Where table text lands in a render, line by line, against the browser's reference (tests only).

For every line box of every table cell, and every line of the text elements laid over a table
(`chipText`, `blockChipText`, `bandText`, `caption`, from `extras["x-wpa"]`), the ink-centroid offset
`dx`/`dy` between the two pictures. Ported from Slide Studio's fidelity tooling (dropped from the
service) for the `renderer_full` tests in `tests/engine/emit/test_tables_fidelity.py`. Needs numpy.

`ink_centroid` measures darkness against the crop's brightest decile, so white text on a dark header
would be measured by its background; such a crop (median below mid-grey in the reference) is inverted
in both pictures first. A line with no ink in either picture has no offset.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.engine.ir import IR, Box

TEXT_ROLES = ("chipText", "blockChipText", "bandText", "caption")
GLYPH_BAND_EM = 1.6


def ink_centroid(image: Any, box: Box, *, pad: int = 2) -> tuple[float, float] | None:
    """The darkness-weighted centre of one line box, in page px. None when the crop has no ink."""
    import numpy as np

    left, top = max(0, int(box.x) - pad), max(0, int(box.y) - pad)
    right = min(image.width, int(box.x + box.w) + pad + 1)
    bottom = min(image.height, int(box.y + box.h) + pad + 1)
    if right <= left or bottom <= top:
        return None
    crop = np.asarray(image.convert("L").crop((left, top, right, bottom)), dtype=np.float64)
    if crop.size == 0:
        return None
    ink = np.clip(float(np.percentile(crop, 90)) - crop, 0.0, None)
    total = float(ink.sum())
    if total < 1.0:
        return None
    rows = np.arange(ink.shape[0], dtype=np.float64)[:, None]
    columns = np.arange(ink.shape[1], dtype=np.float64)[None, :]
    return float((ink * columns).sum() / total) + left, float((ink * rows).sum() / total) + top


def _dark(image: Any, box: Box) -> bool:
    import numpy as np

    left, top = max(0, int(box.x)), max(0, int(box.y))
    right, bottom = min(image.width, int(box.x + box.w) + 1), min(image.height, int(box.y + box.h) + 1)
    if right <= left or bottom <= top:
        return False
    crop = np.asarray(image.convert("L").crop((left, top, right, bottom)), dtype=np.float64)
    return bool(crop.size) and float(np.median(crop)) < 128.0


def _glyph_band(box: Box, size_px: float) -> Box:
    band = GLYPH_BAND_EM * size_px
    if size_px <= 0 or box.h <= band:
        return box
    return Box(box.x, box.y + (box.h - band) / 2.0, box.w, band)


def _lines(ir: IR) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for element in ir.elements:
        if element.kind == "table":
            for cell in element.cells or []:
                for paragraph in cell.get("paragraphs") or []:
                    for line in paragraph.get("lines") or []:
                        out.append({"table": element.name or element.id, "cell": [cell.get("r"), cell.get("c")],
                                    "role": "cell", "box": line.get("box"), "runs": line.get("runs") or []})
            continue
        tag = (element.extras or {}).get("x-wpa") or {}
        if element.kind != "text" or tag.get("role") not in TEXT_ROLES:
            continue
        for paragraph in element.paragraphs or []:
            for line in paragraph.get("lines") or []:
                out.append({"table": tag.get("table"), "cell": tag.get("cell"), "role": tag.get("role"),
                            "box": line.get("box"), "runs": line.get("runs") or []})
    return out


def measure(ir: IR, reference: Path, render: Path) -> list[dict[str, Any]]:
    """One sample per line box: `dx`/`dy` of the render's ink centroid against the reference's."""
    from PIL import Image, ImageOps

    samples: list[dict[str, Any]] = []
    with Image.open(reference) as ref_file, Image.open(render) as out_file:
        ref, out = ref_file.convert("RGB"), out_file.convert("RGB")
        ref_inverted, out_inverted = ImageOps.invert(ref), ImageOps.invert(out)
        for line in _lines(ir):
            box = Box.from_json(line["box"])
            if box is None or box.w <= 0 or box.h <= 0:
                continue
            runs = line.pop("runs")
            box = _glyph_band(box, max((float(run.get("sizePx") or 0.0) for run in runs), default=0.0))
            dark = _dark(ref, box)
            before = ink_centroid(ref_inverted if dark else ref, box)
            after = ink_centroid(out_inverted if dark else out, box)
            samples.append({
                **line,
                "text": "".join(str(run.get("text") or "") for run in runs)[:40],
                "dx": None if not before or not after else round(after[0] - before[0], 3),
                "dy": None if not before or not after else round(after[1] - before[1], 3),
            })
    return samples
