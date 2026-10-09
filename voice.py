"""ElevenLabs voice cloning + TTS for Murray."""
import os

import aiohttp

API = "https://api.elevenlabs.io/v1"


def _key():
    return os.environ.get("ELEVENLABS_API_KEY", "")


async def create_cloned_voice(name, audio_bytes, filename="voice.mp3"):
    """Upload a clip and create an instant-cloned voice. Returns voice_id."""
    key = _key()
    if not key or not audio_bytes:
        return None
    try:
        form = aiohttp.FormData()
        form.add_field("name", name)
        form.add_field("files", audio_bytes,
                       filename=filename, content_type="audio/mpeg")
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{API}/voices/add",
                headers={"xi-api-key": key},
                data=form,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as r:
                if r.status != 200:
                    print(f"voice clone failed: {r.status}", flush=True)
                    return None
                data = await r.json()
                return data.get("voice_id")
    except Exception as e:  # noqa: BLE001
        print(f"voice clone error: {e}", flush=True)
        return None


async def text_to_speech(voice_id, text):
    """Turn text into MP3 bytes using the cloned voice. Returns bytes/None."""
    key = _key()
    if not key or not voice_id or not (text or "").strip():
        return None
    # ElevenLabs free tier: keep it short.
    text = text[:500]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{API}/text-to-speech/{voice_id}",
                headers={"xi-api-key": key,
                         "Content-Type": "application/json"},
                json={"text": text,
                      "model_id": "eleven_multilingual_v2"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status != 200:
                    print(f"tts failed: {r.status}", flush=True)
                    return None
                return await r.read()
    except Exception as e:  # noqa: BLE001
        print(f"tts error: {e}", flush=True)
        return None
