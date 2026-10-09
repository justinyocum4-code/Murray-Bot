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


async def list_characters(bot_id=None):
    url, _ = _cfg()
    if not configured():
        return []
    try:
        filt = f"&bot_id=eq.{bot_id}" if bot_id else "&bot_id=is.null"
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{url}/rest/v1/{TABLE}?select=*&order=updated_at.desc{filt}",
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    return []
                return await r.json()
    except Exception:  # noqa: BLE001
        return []


async def get_active_character(bot_id=None):
    chars = await list_characters(bot_id)
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
               "banned_phrases", "banned_topics", "reply_length",
               "character_memory", "temperature", "repetition_penalty",
               "bot_id")
    payload = {k: data.get(k) for k in allowed if k in data}
    # Normalize empty bot_id to None (the default/env bot).
    if not payload.get("bot_id"):
        payload["bot_id"] = None
    # If this one is being activated, deactivate the others for the same bot.
    try:
        async with aiohttp.ClientSession() as s:
            if payload.get("is_active"):
                others = await list_characters(payload.get("bot_id"))
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


BOT_TABLE = "murray_bots"


async def list_bots():
    """All bot rows, newest first."""
    url, _ = _cfg()
    if not configured():
        return []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{url}/rest/v1/{BOT_TABLE}?select=*&order=created_at.desc",
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    return []
                return await r.json()
    except Exception:  # noqa: BLE001
        return []


async def save_bot(data, bot_id=None):
    """Create or update a bot row. Returns the saved row or None."""
    url, _ = _cfg()
    if not configured():
        return None
    allowed = ("name", "discord_token", "is_active")
    payload = {k: data.get(k) for k in allowed if k in data}
    try:
        async with aiohttp.ClientSession() as s:
            if bot_id:
                async with s.patch(
                    f"{url}/rest/v1/{BOT_TABLE}?id=eq.{bot_id}",
                    headers=_headers(), json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status not in (200, 204):
                        return None
                    rows = await r.json() if r.status == 200 else []
                    return rows[0] if rows else {"id": bot_id, **payload}
            else:
                async with s.post(
                    f"{url}/rest/v1/{BOT_TABLE}", headers=_headers(),
                    json=payload, timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status not in (200, 201):
                        return None
                    rows = await r.json()
                    return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


async def delete_bot(bot_id):
    url, _ = _cfg()
    if not configured():
        return False
    try:
        async with aiohttp.ClientSession() as s:
            async with s.delete(
                f"{url}/rest/v1/{BOT_TABLE}?id=eq.{bot_id}",
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
    if char.get("banned_topics"):
        parts.append(
            "Never bring up these topics on your own — only discuss them if "
            f"the other person mentions them first: {char['banned_topics']}")
    length = (char.get("reply_length") or "medium").lower()
    if length == "short":
        parts.append("Keep replies very short: 1 sentence.")
    elif length == "long":
        parts.append("You can give fuller replies: up to 4-5 sentences when it fits.")
    else:
        parts.append("Keep replies natural length: 1-3 sentences.")
    if char.get("character_memory"):
        parts.append(f"Things you remember about people:\n{char['character_memory']}")
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


def get_max_tokens(char):
    """Map the reply_length setting to a token budget."""
    length = ((char or {}).get("reply_length") or "medium").lower()
    return {"short": 120, "long": 500}.get(length, 300)


def get_temperature(char):
    """Read the temperature setting, clamped to 0.0-1.5."""
    try:
        t = float((char or {}).get("temperature") or 0.7)
    except (TypeError, ValueError):
        t = 0.7
    return max(0.0, min(1.5, t))


def get_repetition_penalty(char):
    """Read the repetition penalty, clamped to 0.0-2.0. Maps to frequency_penalty."""
    try:
        p = float((char or {}).get("repetition_penalty") or 0.5)
    except (TypeError, ValueError):
        p = 0.5
    return max(0.0, min(2.0, p))


LORE_TABLE = "murray_lorebook"


async def list_lorebook(character_id):
    """All lorebook entries for a character, newest first."""
    url, _ = _cfg()
    if not configured() or not character_id:
        return []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{url}/rest/v1/{LORE_TABLE}?character_id=eq.{character_id}"
                "&select=*&order=created_at.desc",
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    return []
                return await r.json()
    except Exception:  # noqa: BLE001
        return []


async def save_lorebook(data, entry_id=None):
    """Create or update a lorebook entry. Returns the saved row or None."""
    url, _ = _cfg()
    if not configured():
        return None
    allowed = ("character_id", "keywords", "content", "is_active")
    payload = {k: data.get(k) for k in allowed if k in data}
    try:
        async with aiohttp.ClientSession() as s:
            if entry_id:
                async with s.patch(
                    f"{url}/rest/v1/{LORE_TABLE}?id=eq.{entry_id}",
                    headers=_headers(), json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status not in (200, 204):
                        return None
                    rows = await r.json() if r.status == 200 else []
                    return rows[0] if rows else {"id": entry_id, **payload}
            else:
                async with s.post(
                    f"{url}/rest/v1/{LORE_TABLE}", headers=_headers(),
                    json=payload, timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status not in (200, 201):
                        return None
                    rows = await r.json()
                    return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


async def delete_lorebook(entry_id):
    url, _ = _cfg()
    if not configured():
        return False
    try:
        async with aiohttp.ClientSession() as s:
            async with s.delete(
                f"{url}/rest/v1/{LORE_TABLE}?id=eq.{entry_id}",
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                return r.status in (200, 204)
    except Exception:  # noqa: BLE001
        return False


async def get_relevant_lore(message, character_id):
    """Return lorebook contents whose keywords appear in the message."""
    entries = await list_lorebook(character_id)
    if not entries or not message:
        return ""
    msg = message.lower()
    hits = []
    for e in entries:
        if not e.get("is_active"):
            continue
        keywords = [k.strip().lower()
                    for k in (e.get("keywords") or "").replace("\n", ",").split(",")
                    if k.strip()]
        if any(kw in msg for kw in keywords):
            content = (e.get("content") or "").strip()
            if content:
                hits.append(content)
    return "\n".join(hits)


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
