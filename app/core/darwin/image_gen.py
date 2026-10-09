"""The image generation port: gpt-image behind a protocol (decision D28: keep OpenAI behind a port).

Ported from Darwin `_shared/openai.ts` (`generateImage`, `editImage`). The routes and jobs depend on
`ImageGenerator` only; `OpenAIImageGenerator` is the one real implementation, over the official
`openai` SDK (pinned in requirements.txt), and `tests/fakes/image_gen.py` is the test double. No
test may build a real OpenAI client (the autouse `no_paid_model_calls` fixture refuses it).

What carries over from Darwin:

* model gpt-image-2, quality medium, one image, the full-slide size 2560x1440 by default
  (`DARWIN_SLIDE_SIZE`); a custom size needs gpt-image-2 and is refused locally otherwise;
* `transparent=True` asks for a transparent PNG (content-only workzone tiles);
* `edit()` sends reference images (the brand master), referenced in the prompt as "Image 1...";
* retries on 429, 5xx and network failures; a permanent client error (400 content policy, 401)
  fails at once. The SDK does this itself (`OPENAI_MAX_RETRIES`, exponential backoff, honouring
  Retry-After), replacing Darwin's hand-written loop.

Errors: `ImageGenError(status, message)`, as Darwin's. `message` is the provider's text (a
content-policy refusal is shown to the user as the slide's error, as Darwin did), cut to 300
characters; it never carries the key. `retryable` says whether a later attempt may succeed.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import openai

from app.config.darwin import DarwinSettings, darwin_settings

_log = logging.getLogger(__name__)

DARWIN_SLIDE_SIZE = "2560x1440"
MAX_MESSAGE = 300


class ImageGenError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message[:MAX_MESSAGE]

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


@dataclass(frozen=True)
class GeneratedImage:
    png: bytes
    model: str


@runtime_checkable
class ImageGenerator(Protocol):
    """Generate or edit one slide image. Both raise `ImageGenError`."""

    async def generate(self, prompt: str, *, size: str = DARWIN_SLIDE_SIZE,
                       transparent: bool = False) -> GeneratedImage: ...

    async def edit(self, prompt: str, images: list[bytes], *, size: str = DARWIN_SLIDE_SIZE,
                   transparent: bool = False) -> GeneratedImage: ...


def check_size(model: str, size: str) -> None:
    """`assertModelSupportsSize`: only gpt-image-2 accepts an arbitrary WIDTHxHEIGHT."""
    if size != DARWIN_SLIDE_SIZE and model != "gpt-image-2":
        raise ImageGenError(400, f"Custom image size {size} requires gpt-image-2 (model is {model})")


def _status_of(exc: Exception) -> int:
    if isinstance(exc, openai.APIStatusError):
        return int(exc.status_code)
    return 502  # connection error, timeout: a retry may succeed


def _message_of(exc: Exception) -> str:
    if isinstance(exc, openai.APIStatusError):
        body = exc.body if isinstance(exc.body, dict) else {}
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        message = err.get("message") if isinstance(err, dict) else None
        return str(message) if message else f"HTTP {exc.status_code}"
    return "Image generation failed (the image service could not be reached)"


class OpenAIImageGenerator:
    """`ImageGenerator` over the OpenAI SDK. One instance per process (it holds the HTTP client)."""

    def __init__(self, settings: DarwinSettings | None = None, client: Any = None) -> None:
        self.settings = settings if settings is not None else darwin_settings
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            key = self.settings.openai_key
            if not key:
                raise ImageGenError(503, "Image generation is not configured (OPENAI_API_KEY).")
            self._client = openai.AsyncOpenAI(api_key=key, timeout=self.settings.openai_request_timeout_s,
                                              max_retries=self.settings.openai_max_retries)
        return self._client

    def _params(self, prompt: str, size: str, transparent: bool) -> dict[str, Any]:
        model = self.settings.openai_image_model
        check_size(model, size)
        params: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1, "size": size,
                                  "quality": self.settings.openai_image_quality}
        if transparent:
            params.update(background="transparent", output_format="png")
        return params

    @staticmethod
    def _decode(response: Any, model: str) -> GeneratedImage:
        data = getattr(response, "data", None) or []
        b64 = getattr(data[0], "b64_json", None) if data else None
        if not b64:
            raise ImageGenError(502, "No image data returned")
        try:
            return GeneratedImage(png=base64.b64decode(b64, validate=True), model=model)
        except ValueError as exc:
            raise ImageGenError(502, "The image data could not be decoded") from exc

    async def generate(self, prompt: str, *, size: str = DARWIN_SLIDE_SIZE,
                       transparent: bool = False) -> GeneratedImage:
        params = self._params(prompt, size, transparent)
        try:
            response = await self.client.images.generate(**params)
        except openai.OpenAIError as exc:
            raise ImageGenError(_status_of(exc), _message_of(exc)) from exc
        return self._decode(response, params["model"])

    async def edit(self, prompt: str, images: list[bytes], *, size: str = DARWIN_SLIDE_SIZE,
                   transparent: bool = False) -> GeneratedImage:
        if not images:
            raise ImageGenError(400, "An edit needs at least one reference image")
        params = self._params(prompt, size, transparent)
        files = [(f"image_{i + 1}.png", img, "image/png") for i, img in enumerate(images)]
        try:
            response = await self.client.images.edit(image=files, **params)
        except openai.OpenAIError as exc:
            raise ImageGenError(_status_of(exc), _message_of(exc)) from exc
        return self._decode(response, params["model"])
