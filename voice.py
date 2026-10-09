"""Free TTS for Murray using Microsoft Edge voices (no API key needed)."""
import io

# Older male voice, aged up: lower pitch + slower rate.
VOICE = "en-US-GuyNeural"
PITCH = "-25Hz"  # deeper = older
RATE = "-8%"     # slower = older


async def text_to_speech(voice_id, text):
    """Turn text into MP3 bytes. Returns bytes/None. voice_id is ignored
    (kept for compatibility) — uses the aged-up built-in voice."""
    if not (text or "").strip():
        return None
    try:
        import edge_tts
    except ImportError:
        print("edge_tts not installed", flush=True)
        return None
    try:
        communicate = edge_tts.Communicate(
            text[:500], VOICE, pitch=PITCH, rate=RATE)
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
