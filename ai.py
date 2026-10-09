"""Groq chat helper for Murray."""
import os

import aiohttp

URL = "https://api.groq.com/openai/v1/chat/completions"


async def _pick_model():
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return
    # Try models in order; yield the first that works.
    # llama-3.3-70b-versatile was shut down by Groq Aug 2026 — removed.
    for model in ("openai/gpt-oss-20b", "openai/gpt-oss-120b",
                  "qwen/qwen3-32b"):
        yield model


async def ai_chat(system, user_text, max_tokens=300, temperature=0.7,
               frequency_penalty=0.5, history=None):
    """Freeform character chat via Groq. Returns reply text or None.
    history: optional list of {"role": ..., "content": ...} dicts for context.
    """
    import sys
    key = os.environ.get("GROQ_API_KEY")
    if not key or not (user_text or "").strip():
        return None
    async for model in _pick_model():
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text[:1000]},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "frequency_penalty": frequency_penalty,
            }
            # gpt-oss are reasoning models: keep reasoning out of the reply.
            # GPT-OSS uses include_reasoning (not reasoning_format).
            if "gpt-oss" in model:
                payload["reasoning_effort"] = "low"
                payload["include_reasoning"] = False
            elif "qwen" in model:
                payload["reasoning_format"] = "hidden"
            # Attach recent conversation history for context.
            messages = [{"role": "system", "content": system}]
            if history:
                messages.extend(history[-12:])
            messages.append({"role": "user",
                             "content": user_text[:1000]})
            payload["messages"] = messages
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    URL,
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
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
