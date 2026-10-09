"""Supabase-backed character store for the AI character dashboard."""
import os

import aiohttp

TABLE = "murray_characters"


def _cfg():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    return url, key


def _headers():
    _, key = _cfg()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def configured():
    url, key = _cfg()
    return bool(url and key)


async def list_characters():
    url, _ = _cfg()
    if not configured():
        return []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{url}/rest/v1/{TABLE}?select=*&order=updated_at.desc",
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    return []
                return await r.json()
    except Exception:  # noqa: BLE001
        return []


async def get_active_character():
    chars = await list_characters()
    for c in chars:
        if c.get("is_active"):
            return c
    return chars[0] if chars else None


async def save_character(data, char_id=None):
    """Create or update a character. Returns the saved row or None."""
    url, _ = _cfg()
    if not configured():
        return None
    # Only allow known fields.
    allowed = ("name", "avatar_url", "tagline", "greeting", "personality",
               "example_dialogue", "scenario", "tags", "visibility",
               "is_active", "voice_clip_url", "voice_note", "voice_channel_id",
               "text_channel_id", "speech_style", "quirks", "never_says",
               "banned_phrases")
    payload = {k: data.get(k) for k in allowed if k in data}
    # If this one is being activated, deactivate the others first.
    try:
        async with aiohttp.ClientSession() as s:
            if payload.get("is_active"):
                others = await list_characters()
                for o in others:
                    if str(o.get("id")) != str(char_id or "") and o.get("is_active"):
                        await s.patch(
                            f"{url}/rest/v1/{TABLE}?id=eq.{o['id']}",
                            headers=_headers(), json={"is_active": False},
                            timeout=aiohttp.ClientTimeout(total=15))
            if char_id:
                async with s.patch(
                    f"{url}/rest/v1/{TABLE}?id=eq.{char_id}",
                    headers=_headers(), json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status not in (200, 204):
                        return None
                    rows = await r.json() if r.status == 200 else []
                    return rows[0] if rows else {"id": char_id, **payload}
            else:
                async with s.post(
                    f"{url}/rest/v1/{TABLE}", headers=_headers(), json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status not in (200, 201):
                        return None
                    rows = await r.json()
                    return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


async def delete_character(char_id):
    url, _ = _cfg()
    if not configured():
        return False
    try:
        async with aiohttp.ClientSession() as s:
            async with s.delete(
                f"{url}/rest/v1/{TABLE}?id=eq.{char_id}",
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                return r.status in (200, 204)
    except Exception:  # noqa: BLE001
        return False


MURRAY_SEED = {
    "name": "Old Man Murray",
    "avatar_url": "",
    "tagline": "Grumpy old metalhead",
    "greeting": "What do you want, kid? Make it quick.",
    "personality": (
        "You are Old Man Murray, a grumpy old metalhead in his 60s. "
        "You've been listening since Black Sabbath was new. "
        "You call everyone 'kid'. "
        "You think most music after 1991 is garbage, but you say it with "
        "grudging affection, not real hate. "
        "You worship Sabbath, Priest, Maiden, Motorhead. Vinyl only. "
        "Streaming is for cowards. "
        "You roast people's music taste playfully — tease them, don't wound them. "
        "You're grumpy but lovable, like a sitcom grandpa."
    ),
    "example_dialogue": (
        "Kid: I love this new band!\n"
        "Murray: New? Kid, I've got socks older than that band. "
        "Come back when they've survived three drummers."
    ),
    "scenario": "Hanging out on a metal Discord server, holding court.",
    "tags": "metal, grumpy, funny",
    "visibility": "private",
    "is_active": True,
    "voice_clip_url": "",
    "voice_note": "",
}


async def ensure_seed():
    """Insert Murray as the first character if the table is empty."""
    chars = await list_characters()
    if not chars and configured():
        await save_character(dict(MURRAY_SEED))


def build_system_prompt(char):
    """Turn a character row into a system prompt for the AI."""
    name = char.get("name") or "Murray"
    parts = [f"You are {name}."]
    if char.get("tagline"):
        parts.append(char["tagline"])
    if char.get("personality"):
        parts.append(char["personality"])
    if char.get("speech_style"):
        parts.append(f"Speech style: {char['speech_style']}")
    if char.get("quirks"):
        parts.append(f"Quirks (use sparingly, not every reply): {char['quirks']}")
    if char.get("never_says"):
        parts.append(f"NEVER do this: {char['never_says']}")
    if char.get("banned_phrases"):
        parts.append(f"NEVER use these words or phrases: {char['banned_phrases']}")
    if char.get("scenario"):
        parts.append(f"Scenario: {char['scenario']}")
    if char.get("example_dialogue"):
        parts.append(f"Example dialogue:\n{char['example_dialogue']}")
    parts.append(
        "Rules you NEVER break:\n"
        "- Keep replies short: 1-3 sentences.\n"
        "- Be playful, never cruel. Never mock someone's body, disability, "
        "race, gender, sexuality, or anything personal. No slurs, ever.\n"
        "- If someone is upset or asks you to stop, drop the act and be kind.\n"
        "- Never claim to be a real person."
    )
    return "\n".join(parts)


def apply_banned_phrases(text, char):
    """Strip banned phrases from a reply (code-enforced, not just prompted).

    Returns the cleaned text. If cleaning would leave nothing meaningful,
    returns the original text unchanged.
    """
    import re
    if not text or not char:
        return text
    banned = char.get("banned_phrases") or ""
    phrases = [p.strip() for p in banned.replace("\n", ",").split(",")
               if p.strip()]
    if not phrases:
        return text
    cleaned = text
    for phrase in phrases:
        cleaned = re.sub(re.escape(phrase), "", cleaned,
                         flags=re.IGNORECASE)
    # Tidy up leftover whitespace and stray punctuation.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"([,.!?;:]){2,}", r"\1", cleaned)
    cleaned = re.sub(r"^[,.!?;:\s]+", "", cleaned).strip()
    cleaned = cleaned[0].upper() + cleaned[1:] if cleaned else cleaned
    # Don't return an empty husk — fall back to the original.
    if len(cleaned) < 3:
        return text
    return cleaned
