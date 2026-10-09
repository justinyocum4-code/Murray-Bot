"""Groq chat helper for Murray."""
import os

import aiohttp

URL = "https://api.groq.com/openai/v1/chat/completions"


async def _pick_model():
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return
    # Try models in order; yield the first that works.
    for model in ("openai/gpt-oss-20b", "openai/gpt-oss-120b",
                  "llama-3.3-70b-versatile", "qwen3-32b"):
        yield model


async def ai_chat(system, user_text, max_tokens=300, temperature=0.7,
               frequency_penalty=0.5):
    """Freeform character chat via Groq. Returns reply text or None."""
    import sys
    key = os.environ.get("GROQ_API_KEY")
    if not key or not (user_text or "").strip():
        return None
    async for model in _pick_model():
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    URL,
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user_text[:1000]},
                        ],
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "frequency_penalty": frequency_penalty,
                    },
                    timeout=timeout,
                ) as resp:
                    if resp.status != 200:
                        print(f"ai_chat: {model} returned {resp.status}",
                              flush=True, file=sys.stderr)
                        continue
                    data = await resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            if text:
                return text
            print(f"ai_chat: {model} returned empty text",
                  flush=True, file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"ai_chat: {model} failed: {type(e).__name__}: {e}",
                  flush=True, file=sys.stderr)
            continue
    print("ai_chat: all models failed", flush=True, file=sys.stderr)
    return None
