"""Image stage — OpenRouter chat-completions with image modality.

OpenRouter does not expose the OpenAI ``/images/generations`` endpoint.
Image generation rides on chat-completions for image-capable models like
``google/gemini-2.5-flash-image-preview`` and ``openai/gpt-image-1``: the
request sends ``modalities=["image","text"]`` and the response carries
the rendered PNG as a base64 ``data:image/png;base64,...`` URI under
``choices[0].message.images[0].image_url.url``.

The bytes are written to ``output_path``; :class:`GeneratedImage.path`
is the API-relative URL ``/api/v1/artifacts/{brand_id}/{run_id}.png``
per FR-023 — bytes are *never* inlined into the API payload.

Hard-fails on OpenRouter errors, malformed responses, or save failures
(FR-021).
"""

from __future__ import annotations

import base64
import io
import logging
import os
import time
from pathlib import Path
from typing import Any

from agents.schemas import AdCopy, CampaignRequest, GeneratedImage, ModelCall
from agents.stages import StageError

logger = logging.getLogger("aura.stages.image")

_DEFAULT_MODEL = "google/gemini-2.5-flash-image-preview"
_DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _make_image_client() -> Any:
    """Construct the OpenAI-compatible client pointed at OpenRouter.

    Patched in tests via monkey-patch so that
    ``client.chat.completions.create(...)`` returns a canned response.
    """

    from openai import OpenAI

    api_key = os.getenv("OPENROUTER_API_KEY")
    base_url = os.getenv("OPENROUTER_BASE_URL") or _DEFAULT_OPENROUTER_BASE_URL
    if not api_key:
        raise StageError("openrouter: OPENROUTER_API_KEY not set")
    return OpenAI(api_key=api_key, base_url=base_url)


def _model_name() -> str:
    return os.getenv("AURA_IMAGE_MODEL", _DEFAULT_MODEL)


def _build_prompt(ad_copy: AdCopy, request: CampaignRequest) -> str:
    return (
        f"Marketing visual for {request.platform} ad. "
        f"Headline: {ad_copy.headline}. Primary message: {ad_copy.primary_text}. "
        f"Audience: {request.target_audience}. "
        "Photorealistic, high quality, brand-safe, well-composed, square 1:1 aspect ratio."
    )


def _negative_prompt_from_feedback(prior_critic_feedback: str | None) -> str:
    """Translate the critic's free-text feedback into negative-prompt fragments.

    The critic's clarity / brand_fit callouts are the most actionable for the
    image stage. We pull short hints out of the feedback string verbatim so a
    later attempt can avoid the same composition pitfalls.
    """

    if not prior_critic_feedback:
        return ""
    snippet = prior_critic_feedback.strip().replace("\n", " ")
    if len(snippet) > 240:
        snippet = snippet[:240] + "..."
    return f"avoid issues from previous attempt: {snippet}"


def _api_image_url(brand_id: str, run_id: str) -> str:
    return f"/api/v1/artifacts/{brand_id}/{run_id}.png"


def _decode_data_url(url: str) -> bytes:
    """Parse ``data:image/<fmt>;base64,<...>`` into raw image bytes."""

    if not isinstance(url, str) or not url.startswith("data:image/"):
        snippet = (url[:80] + "...") if isinstance(url, str) and len(url) > 80 else url
        raise StageError(f"openrouter image: unexpected image url: {snippet!r}")
    try:
        _header, b64 = url.split(",", 1)
    except ValueError as exc:
        raise StageError(f"openrouter image: malformed data url: {exc}") from exc
    try:
        return base64.b64decode(b64)
    except (ValueError, TypeError) as exc:
        raise StageError(f"openrouter image: invalid base64 payload: {exc}") from exc


def _png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            return int(img.width), int(img.height)
    except Exception as exc:  # noqa: BLE001
        raise StageError(f"openrouter image: could not decode image: {exc}") from exc


def _extract_image_url(response: Any) -> str:
    """Pull the image data URL out of the chat-completions response.

    Handles both the real OpenAI SDK shape (where unknown fields land on
    ``message.model_extra`` *and* — under ``extra='allow'`` — also as
    attributes) and the test-stub shape (plain ``SimpleNamespace`` with
    ``images`` attached directly).
    """

    try:
        choice = response.choices[0]
        msg = choice.message

        images = getattr(msg, "images", None)
        if images is None:
            extra = getattr(msg, "model_extra", None) or {}
            images = extra.get("images")
        if not images:
            raise StageError("openrouter image: no images in response")

        first = images[0]
        if isinstance(first, dict):
            image_url_obj = first.get("image_url")
        else:
            image_url_obj = getattr(first, "image_url", None)
        if image_url_obj is None:
            raise StageError("openrouter image: response missing image_url")

        if isinstance(image_url_obj, dict):
            url = image_url_obj.get("url")
        else:
            url = getattr(image_url_obj, "url", None)
    except StageError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise StageError(f"openrouter image: response shape unexpected: {exc}") from exc

    if not isinstance(url, str) or not url:
        raise StageError("openrouter image: image url was empty")
    return url


def run_image(
    ad_copy: AdCopy,
    request: CampaignRequest,
    *,
    output_path: Path,
    prior_critic_feedback: str | None = None,
    image_url: str | None = None,
) -> tuple[GeneratedImage, list[ModelCall]]:
    """Generate a marketing image and persist it as PNG at ``output_path``.

    ``prior_critic_feedback`` (US3) is appended to the prompt and surfaced as
    a negative-prompt fragment so retried attempts steer clear of issues the
    critic flagged. ``image_url`` lets the caller pin the API-relative URL
    independent of ``output_path`` — the runner uses this to keep the URL
    canonical (``{run_id}.png``) even when intermediate attempts write to
    per-attempt files.
    """

    prompt = _build_prompt(ad_copy, request)
    if prior_critic_feedback:
        prompt = (
            f"{prompt} Address the previous attempt's critique: {prior_critic_feedback.strip()}"
        )
    negative_prompt = _negative_prompt_from_feedback(prior_critic_feedback)
    model = _model_name()

    started_at = time.monotonic()
    try:
        client = _make_image_client()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"modalities": ["image", "text"]},
        )
    except StageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StageError(f"openrouter image: {exc}") from exc

    latency_ms = max(0, int((time.monotonic() - started_at) * 1000))

    data_url = _extract_image_url(response)
    png_bytes = _decode_data_url(data_url)
    width, height = _png_dimensions(png_bytes)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_path.write_bytes(png_bytes)
    except OSError as exc:
        raise StageError(f"openrouter image: write failed: {exc}") from exc

    if image_url is None:
        run_id = output_path.stem
        image_url = _api_image_url(request.brand_id, run_id)

    return (
        GeneratedImage(
            path=image_url,
            prompt=prompt,
            negative_prompt=negative_prompt,
            dimensions=(width, height),
        ),
        [
            ModelCall(
                provider="openrouter",
                model=model,
                op="chat.completions",
                latency_ms=latency_ms,
            )
        ],
    )


__all__ = ["run_image"]
