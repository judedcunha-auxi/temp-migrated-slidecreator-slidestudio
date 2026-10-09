"""core/darwin/image_gen: the OpenAI adapter over a stand-in client (no real client may be built)."""

from __future__ import annotations

import base64
from typing import Any

import httpx2 as httpx
import openai
import pytest

from app.core.darwin.image_gen import DARWIN_SLIDE_SIZE, ImageGenerator, ImageGenError, OpenAIImageGenerator
from tests.conftest import build_darwin_settings
from tests.fakes.image_gen import FakeImageGenerator, tiny_png

pytestmark = pytest.mark.asyncio


class _Images:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def generate(self, **params: Any) -> Any:
        self.calls.append(("generate", params))
        return self._play()

    async def edit(self, **params: Any) -> Any:
        self.calls.append(("edit", params))
        return self._play()

    def _play(self) -> Any:
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class _Client:
    def __init__(self, answer: Any) -> None:
        self.images = _Images(answer)


class _Resp:
    def __init__(self, b64: str | None) -> None:
        self.data = [type("D", (), {"b64_json": b64})()] if b64 is not None else []


def _status_error(status: int, message: str) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/images/generations")
    response = httpx.Response(status, request=request, json={"error": {"message": message}})
    return openai.APIStatusError(message, response=response, body={"error": {"message": message}})


def _png_b64() -> str:
    return base64.b64encode(tiny_png()).decode()


async def test_generate_sends_darwins_parameters_and_decodes_the_png():
    client = _Client(_Resp(_png_b64()))
    gen = OpenAIImageGenerator(build_darwin_settings(), client=client)
    image = await gen.generate("A slide")
    assert image.png == tiny_png() and image.model == "gpt-image-2"
    kind, params = client.images.calls[0]
    assert kind == "generate"
    assert params == {"model": "gpt-image-2", "prompt": "A slide", "n": 1, "size": DARWIN_SLIDE_SIZE,
                      "quality": "medium"}


async def test_transparent_tiles_and_edits_with_reference_images():
    client = _Client(_Resp(_png_b64()))
    gen = OpenAIImageGenerator(build_darwin_settings(), client=client)
    await gen.generate("tile", size="1536x864", transparent=True)
    await gen.edit("Image 1 is the master", [tiny_png(), tiny_png()])
    assert client.images.calls[0][1]["background"] == "transparent"
    assert client.images.calls[0][1]["output_format"] == "png"
    files = client.images.calls[1][1]["image"]
    assert [f[0] for f in files] == ["image_1.png", "image_2.png"] and files[0][2] == "image/png"


async def test_an_edit_needs_a_reference_image():
    with pytest.raises(ImageGenError):
        await OpenAIImageGenerator(build_darwin_settings(), client=_Client(None)).edit("x", [])


async def test_a_custom_size_needs_gpt_image_2():
    gen = OpenAIImageGenerator(build_darwin_settings(openai_image_model="gpt-image-1"), client=_Client(None))
    with pytest.raises(ImageGenError) as err:
        await gen.generate("x", size="1024x512")
    assert err.value.status == 400 and not err.value.retryable


@pytest.mark.parametrize(("raised", "status", "retryable"), [
    (_status_error(400, "Your request was rejected by the safety system."), 400, False),
    (_status_error(429, "Rate limit"), 429, True),
    (_status_error(503, "Overloaded"), 503, True),
    (openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com")), 502, True),
])
async def test_sdk_errors_become_image_gen_errors(raised: Exception, status: int, retryable: bool):
    gen = OpenAIImageGenerator(build_darwin_settings(), client=_Client(raised))
    with pytest.raises(ImageGenError) as err:
        await gen.generate("x")
    assert (err.value.status, err.value.retryable) == (status, retryable)


async def test_no_image_data_is_a_502():
    gen = OpenAIImageGenerator(build_darwin_settings(), client=_Client(_Resp(None)))
    with pytest.raises(ImageGenError) as err:
        await gen.generate("x")
    assert err.value.status == 502


async def test_no_key_is_a_clear_error_and_no_client_is_built():
    with pytest.raises(ImageGenError) as err:
        await OpenAIImageGenerator(build_darwin_settings()).generate("x")
    assert err.value.status == 503 and "OPENAI_API_KEY" in err.value.message


async def test_a_real_client_is_refused_in_tests():
    gen = OpenAIImageGenerator(build_darwin_settings(openai_api_key="placeholder-openai-key"))
    with pytest.raises(AssertionError, match="paid call"):
        await gen.generate("x")


async def test_the_fake_satisfies_the_port():
    fake = FakeImageGenerator()
    assert isinstance(fake, ImageGenerator)
    await fake.edit("p", [b"x"])
    assert fake.calls[0].images == 1
