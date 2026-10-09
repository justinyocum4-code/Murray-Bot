"""Voice listening for Murray: capture speech, transcribe with Groq Whisper,
reply in Murray's voice. Gives him ears to go with his mouth."""
import asyncio
import io
import os
import struct
import time
import wave

import aiohttp
import discord

try:
    from discord.ext import voice_recv
    HAVE_VOICE_RECV = True
except ImportError:
    HAVE_VOICE_RECV = False

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Simple energy-based voice activity detection, tuned for Discord PCM.
SILENCE_TIMEOUT = 1.6   # seconds of quiet = end of utterance
MIN_SPEECH = 0.8        # ignore blips shorter than this
POLL = 0.2              # sink poll interval
ENERGY_THRESH = 900     # RMS-ish threshold for 16-bit PCM


def _rms(pcm_bytes):
    if not pcm_bytes:
        return 0
    n = len(pcm_bytes) // 2
    if n == 0:
        return 0
    vals = struct.unpack(f"<{n}h", pcm_bytes[:n * 2])
    return (sum(v * v for v in vals) / n) ** 0.5


def _pcm_to_wav(pcm_bytes, sample_rate=48000, channels=2):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)
    return buf.getvalue()


async def transcribe(wav_bytes):
    """Send WAV audio to Groq Whisper. Returns text or None."""
    key = os.environ.get("GROQ_API_KEY", "")
    if not key or not wav_bytes:
        return None
    try:
        form = aiohttp.FormData()
        form.add_field("model", "whisper-large-v3")
        form.add_field("file", wav_bytes, filename="speech.wav",
                       content_type="audio/wav")
        form.add_field("response_format", "json")
        async with aiohttp.ClientSession() as s:
            async with s.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {key}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status != 200:
                    print(f"whisper failed: {r.status}", flush=True)
                    return None
                data = await r.json()
                text = (data.get("text") or "").strip()
                return text or None
    except Exception as e:  # noqa: BLE001
        print(f"whisper error: {e}", flush=True)
        return None


class Listener:
    """Attaches to a voice client, transcribes utterances, calls back."""

    def __init__(self, bot, voice_client, on_utterance):
        self.bot = bot
        self.vc = voice_client
        self.on_utterance = on_utterance  # async fn(user_id, text)
        self.sink = None
        self.task = None
        self.running = False
        self._speaking = False      # someone currently talking
        self._speech_start = 0
        self._last_voice = 0
        self._buf = bytearray()

    def start(self):
        if not HAVE_VOICE_RECV:
            print("voice_recv not installed, listening disabled", flush=True)
            return False
        if self.running:
            return True
        self.sink = voice_recv.BasicSink(event=asyncio.Event())
        try:
            self.vc.listen(self.sink)
        except Exception as e:  # noqa: BLE001
            print(f"listen failed: {e}", flush=True)
            return False
        self.running = True
        self.task = asyncio.create_task(self._loop())
        print("Murray is listening…", flush=True)
        return True

    def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            self.task = None
        try:
            if self.vc:
                self.vc.stop_listening()
        except Exception:  # noqa: BLE001
            pass
        self.sink = None

    async def _loop(self):
        try:
            while self.running:
                await asyncio.sleep(POLL)
                # Don't listen to ourselves.
                if self.vc.is_playing():
                    self._reset()
                    # Drain the sink so our own voice doesn't queue up.
                    if self.sink:
                        for u in list(self.sink.audio_data.keys()):
                            self.sink.audio_data.pop(u, None)
                    continue
                if not self.sink:
                    continue
                # Merge all users' fresh audio.
                chunk = bytearray()
                for user_id in list(self.sink.audio_data.keys()):
                    ad = self.sink.audio_data.pop(user_id, None)
                    if ad is None:
                        continue
                    # Skip the bot itself.
                    if self.bot.user and user_id == self.bot.user.id:
                        continue
                    try:
                        raw = ad.file.getvalue()
                    except Exception:  # noqa: BLE001
                        continue
                    if raw:
                        chunk += raw
                # Downmix stereo->mono for energy check (take every 4th byte
                # pair = left channel).
                energy = _rms(bytes(chunk))
                now = time.time()
                if energy > ENERGY_THRESH:
                    if not self._speaking:
                        self._speaking = True
                        self._speech_start = now
                        self._buf = bytearray()
                    self._last_voice = now
                    self._buf += chunk
                elif self._speaking:
                    # Still collect a little trailing audio.
                    self._buf += chunk
                    if now - self._last_voice > SILENCE_TIMEOUT:
                        dur = self._last_voice - self._speech_start
                        pcm = bytes(self._buf)
                        speaker = "someone"
                        self._reset()
                        if dur >= MIN_SPEECH and len(pcm) > 8000:
                            await self._handle(pcm)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"listener loop error: {e}", flush=True)

    def _reset(self):
        self._speaking = False
        self._buf = bytearray()
        self._last_voice = 0
        self._speech_start = 0

    async def _handle(self, pcm):
        wav = _pcm_to_wav(pcm)
        text = await transcribe(wav)
        if not text or len(text) < 3:
            return
        # Ignore obvious non-speech artifacts.
        low = text.lower().strip()
        if low in ("you", "thank you", ".", "...", "bye"):
            return
        print(f"heard: {text[:80]}", flush=True)
        try:
            await self.on_utterance(text)
        except Exception as e:  # noqa: BLE001
            print(f"utterance handler error: {e}", flush=True)
