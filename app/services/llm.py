"""
Provider-agnostic LLM router.

Deliberately built on raw httpx calls instead of the OpenAI/Anthropic/Google
SDKs: three less dependencies to version-pin, no vendor lock on the request
shape, and it makes the actual wire protocol of each provider visible instead
of hidden behind a client object. Each provider has wildly different request
shapes (system prompt placement, message format, auth header) — this module
normalizes them behind one `LLMRouter.complete()` call.

Fallback strategy: try providers in `settings.llm_provider_order`, skipping
any without a key configured. If a provider call fails (timeout, 5xx, rate
limit) the router logs it and falls through to the next one rather than
failing the whole request — this is the same pattern you'd want in
production if any single model vendor has an outage.
"""
import json
import re
import logging
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger("fass_flow.llm")

DEFAULT_MODELS = {
    "anthropic": "claude-3-5-haiku-20241022",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-1.5-flash",
    "deepseek": "deepseek-chat",
}

# Screenshot transcription needs a vision-capable model. Claude 3.5 Haiku
# (the text default above) doesn't accept image input via the API, so
# vision calls override to Sonnet for Anthropic specifically; OpenAI's
# gpt-4o-mini and Gemini 1.5 Flash already handle images, so they're
# unchanged from the text defaults. DeepSeek's public API has no
# vision-capable chat model, so it's deliberately left out of this map —
# the router's fallthrough (`else: continue`) skips it for vision calls
# and falls to the next configured provider instead of erroring.
VISION_MODELS = {
    "anthropic": "claude-3-5-sonnet-20241022",
    "openai": DEFAULT_MODELS["openai"],
    "gemini": DEFAULT_MODELS["gemini"],
}

TIMEOUT = httpx.Timeout(60.0, connect=10.0)


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str


class LLMUnavailableError(RuntimeError):
    """Raised when every configured provider failed or none are configured."""


class LLMRouter:
    def __init__(self):
        self._keys = {
            "anthropic": settings.anthropic_api_key,
            "openai": settings.openai_api_key,
            "gemini": settings.gemini_api_key,
            "deepseek": settings.deep_seek_api_key,
        }
        self._order = [p.strip() for p in settings.llm_provider_order.split(",") if p.strip()]

    def available_providers(self) -> list[str]:
        return [p for p in self._order if self._keys.get(p)]

    async def complete(self, system: str, prompt: str, max_tokens: int = 1200) -> LLMResult:
        """Try each available provider in order; return the first success."""
        providers = self.available_providers()
        if not providers:
            raise LLMUnavailableError(
                "No LLM provider configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
                "or GEMINI_API_KEY in the backend environment."
            )

        last_err = None
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for provider in providers:
                try:
                    if provider == "anthropic":
                        text = await self._call_anthropic(client, system, prompt)
                    elif provider == "openai":
                        text = await self._call_openai(client, system, prompt)
                    elif provider == "gemini":
                        text = await self._call_gemini(client, system, prompt)
                    elif provider == "deepseek":
                        text = await self._call_deepseek(client, system, prompt)
                    else:
                        continue
                    return LLMResult(text=text, provider=provider, model=DEFAULT_MODELS[provider])
                except Exception as e:  # noqa: BLE001 — intentionally broad: any provider failure should fall through
                    logger.warning("LLM provider %s failed: %s", provider, e)
                    last_err = e
                    continue

        raise LLMUnavailableError(f"All configured LLM providers failed. Last error: {last_err}")

    async def complete_vision(
        self, system: str, prompt: str, images: list[dict], max_tokens: int = 2000
    ) -> LLMResult:
        """Like complete(), but for image input. `images` is a list of
        {"data": base64_str, "media_type": "image/png"} dicts. Used for
        screenshot transcription, where there's no text to send the
        regular text-only providers — every fallback step here also needs
        a vision-capable model, hence VISION_MODELS instead of DEFAULT_MODELS."""
        providers = self.available_providers()
        if not providers:
            raise LLMUnavailableError(
                "No LLM provider configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
                "or GEMINI_API_KEY in the backend environment."
            )

        last_err = None
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for provider in providers:
                try:
                    if provider == "anthropic":
                        text = await self._call_anthropic_vision(client, system, prompt, images, max_tokens)
                    elif provider == "openai":
                        text = await self._call_openai_vision(client, system, prompt, images, max_tokens)
                    elif provider == "gemini":
                        text = await self._call_gemini_vision(client, system, prompt, images)
                    else:
                        continue
                    return LLMResult(text=text, provider=provider, model=VISION_MODELS[provider])
                except Exception as e:  # noqa: BLE001 — same fallthrough rationale as complete()
                    logger.warning("Vision provider %s failed: %s", provider, e)
                    last_err = e
                    continue

        raise LLMUnavailableError(f"All configured vision providers failed. Last error: {last_err}")

    async def _call_anthropic_vision(
        self, client: httpx.AsyncClient, system: str, prompt: str, images: list[dict], max_tokens: int
    ) -> str:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]}}
            for img in images
        ]
        content.append({"type": "text", "text": prompt})
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self._keys["anthropic"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": VISION_MODELS["anthropic"],
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": content}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return "".join(block.get("text", "") for block in data.get("content", []))

    async def _call_openai_vision(
        self, client: httpx.AsyncClient, system: str, prompt: str, images: list[dict], max_tokens: int
    ) -> str:
        content = [{"type": "text", "text": prompt}]
        for img in images:
            content.append({"type": "image_url", "image_url": {"url": f"data:{img['media_type']};base64,{img['data']}"}})
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._keys['openai']}",
                "content-type": "application/json",
            },
            json={
                "model": VISION_MODELS["openai"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.0,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def _call_gemini_vision(
        self, client: httpx.AsyncClient, system: str, prompt: str, images: list[dict]
    ) -> str:
        model = VISION_MODELS["gemini"]
        parts = [{"inline_data": {"mime_type": img["media_type"], "data": img["data"]}} for img in images]
        parts.append({"text": prompt})
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": self._keys["gemini"]},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"temperature": 0.0},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    async def _call_anthropic(self, client: httpx.AsyncClient, system: str, prompt: str) -> str:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self._keys["anthropic"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": DEFAULT_MODELS["anthropic"],
                "max_tokens": 1200,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return "".join(block.get("text", "") for block in data.get("content", []))

    async def _call_openai(self, client: httpx.AsyncClient, system: str, prompt: str) -> str:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._keys['openai']}",
                "content-type": "application/json",
            },
            json={
                "model": DEFAULT_MODELS["openai"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def _call_deepseek(self, client: httpx.AsyncClient, system: str, prompt: str) -> str:
        # DeepSeek's API is OpenAI-compatible (same request/response shape,
        # different base URL + model name), so this mirrors _call_openai
        # exactly rather than needing its own wire format.
        resp = await client.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {self._keys['deepseek']}",
                "content-type": "application/json",
            },
            json={
                "model": DEFAULT_MODELS["deepseek"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def _call_gemini(self, client: httpx.AsyncClient, system: str, prompt: str) -> str:
        model = DEFAULT_MODELS["gemini"]
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": self._keys["gemini"]},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]


def extract_json(text: str) -> dict:
    """LLMs frequently wrap JSON in markdown fences or add stray prose
    around it. Pull out the first {...} block and parse it, rather than
    requiring exact raw JSON output."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in LLM output: {text[:200]}")
    return json.loads(match.group(0))


llm_router = LLMRouter()
