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
    from discord.ext.voice_recv import opus as _vr_opus
    HAVE_VOICE_RECV = True

    # One corrupted voice packet must not kill the router thread.
    _orig_decode_packet = _vr_opus.PacketDecoder._decode_packet

    def _safe_decode_packet(self, packet):
        try:
            return _orig_decode_packet(self, packet)
        except Exception as e:  # noqa: BLE001
            print(f"opus decode skipped bad packet: {e}", flush=True)
            return packet, b""

    _vr_opus.PacketDecoder._decode_packet = _safe_decode_packet
except ImportError:
    HAVE_VOICE_RECV = False

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

SILENCE_TIMEOUT = 1.6   # seconds of quiet = end of utterance
MIN_SPEECH = 0.8        # ignore blips shorter than this
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


async def transcribe(audio_bytes, filename="speech.wav"):
    """Send audio to Groq Whisper. Returns text or None."""
    key = os.environ.get("GROQ_API_KEY", "")
    if not key or not audio_bytes:
        return None
    try:
        form = aiohttp.FormData()
        form.add_field("model", "whisper-large-v3")
        form.add_field("file", audio_bytes, filename=filename)
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
    """Callback-based listener: audio packets arrive via BasicSink callback,
    get queued, and an async loop does VAD + transcription."""

    def __init__(self, bot, voice_client, on_utterance):
        self.bot = bot
        self.vc = voice_client
        self.on_utterance = on_utterance  # async fn(text)
        self.sink = None
        self.task = None
        self.running = False
        self.queue = None
        self._speaking = False
        self._speech_start = 0
        self._last_voice = 0
        self._buf = bytearray()

    def _on_packet(self, user, voice_data):
        # Runs in the voice thread — just enqueue, don't block.
        if not self.running or self.queue is None:
            return
        if self.bot.user and user is not None and user.id == self.bot.user.id:
            return
        pcm = getattr(voice_data, "pcm", b"") or b""
        if not pcm:
            return
        try:
            self.queue.put_nowait(pcm)
        except asyncio.QueueFull:
            pass

    def start(self):
        if not HAVE_VOICE_RECV:
            print("voice_recv not installed, listening disabled", flush=True)
            return False
        if self.running:
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        self.queue = asyncio.Queue(maxsize=400)
        # Wrap the sync callback so it can feed the async queue.
        def cb(user, voice_data, _loop=loop):
            pcm = getattr(voice_data, "pcm", b"") or b""
            if not pcm or not self.running:
                return
            if (self.bot.user and user is not None
                    and user.id == self.bot.user.id):
                return
            _loop.call_soon_threadsafe(self._feed, pcm)
        self.sink = voice_recv.BasicSink(event=cb)
        try:
            self.vc.listen(self.sink)
        except Exception as e:  # noqa: BLE001
            print(f"listen failed: {e}", flush=True)
            return False
        self.running = True
        self.task = loop.create_task(self._loop())
        print("Murray is listening…", flush=True)
        return True

    def _feed(self, pcm):
        if self.queue is not None:
            try:
                self.queue.put_nowait(pcm)
            except asyncio.QueueFull:
                pass

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
        self.queue = None

    async def _loop(self):
        try:
            while self.running:
                # Don't process our own voice.
                if self.vc.is_playing():
                    self._drain()
                    self._reset()
                    await asyncio.sleep(0.3)
                    continue
                try:
                    pcm = await asyncio.wait_for(self.queue.get(), timeout=0.3)
                except asyncio.TimeoutError:
                    # Check for end-of-utterance on silence.
                    if (self._speaking and time.time() - self._last_voice
                            > SILENCE_TIMEOUT):
                        await self._finish_utterance()
                    continue
                energy = _rms(pcm)
                now = time.time()
                if energy > ENERGY_THRESH:
                    if not self._speaking:
                        self._speaking = True
                        self._speech_start = now
                        self._buf = bytearray()
                    self._last_voice = now
                    self._buf += pcm
                elif self._speaking:
                    self._buf += pcm
                    if now - self._last_voice > SILENCE_TIMEOUT:
                        await self._finish_utterance()
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"listener loop error: {e}", flush=True)

    def _drain(self):
        if self.queue is not None:
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    def _reset(self):
        self._speaking = False
        self._buf = bytearray()
        self._last_voice = 0
        self._speech_start = 0

    async def _finish_utterance(self):
        dur = self._last_voice - self._speech_start
        pcm = bytes(self._buf)
        self._reset()
        if dur < MIN_SPEECH or len(pcm) < 8000:
            return
        wav = _pcm_to_wav(pcm)
        # Debug: save the raw audio so we can hear what Murray hears.
        try:
            with open("/tmp/murray_last_heard.wav", "wb") as f:
                f.write(wav)
        except Exception:
            pass
        text = await transcribe(wav)
        if not text or len(text) < 3:
            return
        low = text.lower().strip()
        if low in ("you", "thank you", ".", "...", "bye"):
            return
        print(f"heard: {text[:80]}", flush=True)
        try:
            await self.on_utterance(text)
        except Exception as e:  # noqa: BLE001
            print(f"utterance handler error: {e}", flush=True)
