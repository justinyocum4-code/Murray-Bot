"""Free TTS for Murray using Microsoft Edge voices (no API key needed)."""
import io

# Gruff older male voice. Other options: en-US-DavisNeural, en-US-TonyNeural.
VOICE = "en-US-GuyNeural"


async def text_to_speech(voice_id, text):
    """Turn text into MP3 bytes. Returns bytes/None. voice_id is ignored
    (kept for compatibility) — uses the built-in gruff voice."""
    if not (text or "").strip():
        return None
    try:
        import edge_tts
    except ImportError:
        print("edge_tts not installed", flush=True)
        return None
    try:
        communicate = edge_tts.Communicate(text[:500], VOICE)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        data = buf.getvalue()
        return data if data else None
    except Exception as e:  # noqa: BLE001
        print(f"edge tts failed: {e}", flush=True)
        return None


# Stubs kept for compatibility — ElevenLabs is gone.
async def create_cloned_voice(name, audio_bytes, filename="voice.mp3"):
    return None


async def list_voices():
    return []
