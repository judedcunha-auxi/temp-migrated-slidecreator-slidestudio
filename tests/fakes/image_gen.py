"""A scriptable `ImageGenerator` (app/core/darwin/image_gen.py): no call leaves the process, nothing is paid.

Every call is recorded (`calls`). By default each returns a tiny valid PNG; `fail_with` makes the
next calls raise an `ImageGenError` (e.g. a 400 content-policy refusal or a 429).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from PIL import Image

from app.core.darwin.image_gen import DARWIN_SLIDE_SIZE, GeneratedImage, ImageGenError


def tiny_png(width: int = 16, height: int = 9, color: tuple[int, int, int] = (31, 58, 95)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


@dataclass
class ImageCall:
    kind: str  # "generate" | "edit"
    prompt: str
    size: str
    transparent: bool
    images: int = 0


@dataclass
class FakeImageGenerator:
    model: str = "fake-gpt-image"
    calls: list[ImageCall] = field(default_factory=list)
    fail_with: ImageGenError | None = None

    def _answer(self, call: ImageCall) -> GeneratedImage:
        self.calls.append(call)
        if self.fail_with is not None:
            raise self.fail_with
        return GeneratedImage(png=tiny_png(), model=self.model)

    async def generate(self, prompt: str, *, size: str = DARWIN_SLIDE_SIZE,
                       transparent: bool = False) -> GeneratedImage:
        return self._answer(ImageCall("generate", prompt, size, transparent))

    async def edit(self, prompt: str, images: list[bytes], *, size: str = DARWIN_SLIDE_SIZE,
                   transparent: bool = False) -> GeneratedImage:
        return self._answer(ImageCall("edit", prompt, size, transparent, images=len(images)))
