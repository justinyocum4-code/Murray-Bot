"""Old Man Murray — AI character Discord bot + character dashboard."""
import asyncio
import io
import hashlib
import json
import os
import secrets
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

from ai import ai_chat
import characters
import voice as voice_mod
import listen as listen_mod
try:
    from discord.ext import voice_recv
    HAVE_VR = True
except ImportError:
    HAVE_VR = False

SESSIONS = {}  # token -> expiry
BOTS = {}  # bot_id (None = env-token default bot) -> MurrayBot instance
_WEB_STARTED = False


def bust_cache(bot_id=None):
    """Clear the character cache for one bot (or all with "all")."""
    if bot_id == "all":
        for b in BOTS.values():
            b._char_cache = None
        return
    b = BOTS.get(bot_id)
    if b:
        b._char_cache = None


def _dashboard_password():
    return os.environ.get("DASHBOARD_PASSWORD", "")


def _authed(request):
    tok = request.cookies.get("murray_dash")
    return tok in SESSIONS and SESSIONS[tok] > time.time()


def _require_auth(handler):
    async def wrapper(request):
        if not _dashboard_password():
            return web.Response(
                text="Set DASHBOARD_PASSWORD on Render first.", status=500)
        if not _authed(request):
            raise web.HTTPFound("/dashboard/login")
        return await handler(request)
    return wrapper


async def _load_dashboard_html():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "dashboard.html"), encoding="utf-8") as f:
        return f.read()


class MurrayBot(commands.Bot):
    def __init__(self, bot_id=None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self._bot_id = bot_id
        self._cooldown = {}
        self._char_cache = None
        self._char_cache_at = 0
        self._listeners = {}  # guild_id -> listen_mod.Listener
        self._history = {}  # (guild_id, channel_id) -> [msg dicts]

    async def get_character(self):
        # Cache the active character for 60s so every message isn't a DB hit.
        now = time.time()
        if self._char_cache and now - self._char_cache_at < 60:
            return self._char_cache
        char = await characters.get_active_character(self._bot_id)
        if char:
            self._char_cache = char
            self._char_cache_at = now
        return char or self._char_cache

    async def setup_hook(self):
        async def _debug_audio(request):
            # Serve the last captured utterance so we can hear what he hears.
            try:
                with open("/tmp/murray_last_heard.wav", "rb") as f:
                    data = f.read()
                return web.Response(body=data, content_type="audio/wav")
            except FileNotFoundError:
                return web.Response(text="no audio captured yet", status=404)

        app = web.Application()
        app.router.add_get("/health", lambda r: web.Response(text="ok"))
        app.router.add_get("/", lambda r: web.Response(text="ok"))
        app.router.add_get("/debug_audio", _debug_audio)

        # ---- Dashboard pages ----
        async def login_page(request):
            html = await _load_dashboard_html()
            # Simple login form injected at top when not authed.
            return web.Response(text=LOGIN_HTML, content_type="text/html")

        async def login_post(request):
            data = await request.post()
            if (_dashboard_password() and data.get("password")
                    == _dashboard_password()):
                tok = secrets.token_hex(16)
                SESSIONS[tok] = time.time() + 86400 * 30
                resp = web.HTTPFound("/dashboard")
                resp.set_cookie("murray_dash", tok, max_age=86400 * 30,
                                httponly=True, samesite="Lax")
                raise resp
            raise web.HTTPFound("/dashboard/login?bad=1")

        @_require_auth
        async def dashboard_page(request):
            return web.Response(text=await _load_dashboard_html(),
                                content_type="text/html")

        @_require_auth
        async def api_list(request):
            bot_id = request.query.get("bot_id") or None
            chars = await characters.list_characters(bot_id)
            # Don't leak anything sensitive; rows are Justin's own.
            return web.json_response({"characters": chars})

        @_require_auth
        async def api_save(request):
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                return web.json_response({"error": "bad json"}, status=400)
            char_id = body.get("id")
            if not body.get("bot_id"):
                body["bot_id"] = None
            saved = await characters.save_character(body, char_id)
            if not saved:
                return web.json_response(
                    {"error": "save failed (check Supabase setup)"},
                    status=500)
            # Bust the right bot's cache so the new personality takes effect.
            bust_cache(saved.get("bot_id"))
            return web.json_response({"ok": True, "character": saved})

        @_require_auth
        async def api_delete(request):
            char_id = request.match_info["char_id"]
            ok = await characters.delete_character(char_id)
            if ok:
                bust_cache("all")
            return web.json_response({"ok": ok})

        @_require_auth
        async def api_test_chat(request):
            """Test a personality without saving: {character fields..., message}."""
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                return web.json_response({"error": "bad json"}, status=400)
            message = (body.get("message") or "").strip()
            if not message:
                return web.json_response({"error": "empty message"}, status=400)
            # Build a throwaway character dict from the form values.
            char = {k: body.get(k, "") for k in (
                "name", "tagline", "personality", "speech_style", "quirks",
                "never_says", "banned_phrases", "banned_topics",
                "reply_length", "character_memory", "scenario",
                "example_dialogue")}
            system = characters.build_system_prompt(char)
            reply = await ai_chat(
                system, f"{char.get('name') or 'Murray'} talking to Tester: {message}",
                max_tokens=characters.get_max_tokens(char))
            if reply:
                reply = characters.apply_banned_phrases(reply, char)
            return web.json_response({"ok": True, "reply": reply or ""})

        @_require_auth
        async def api_lore_list(request):
            character_id = request.query.get("character_id", "")
            entries = await characters.list_lorebook(character_id)
            return web.json_response({"entries": entries})

        @_require_auth
        async def api_lore_save(request):
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                return web.json_response({"error": "bad json"}, status=400)
            entry_id = body.get("id")
            saved = await characters.save_lorebook(body, entry_id)
            if not saved:
                return web.json_response({"error": "save failed"}, status=500)
            return web.json_response({"ok": True, "entry": saved})

        @_require_auth
        async def api_lore_delete(request):
            entry_id = request.match_info["entry_id"]
            ok = await characters.delete_lorebook(entry_id)
            return web.json_response({"ok": ok})

        @_require_auth
        async def api_bot_list(request):
            bots = await characters.list_bots()
            safe = [{k: v for k, v in b.items() if k != "discord_token"}
                    for b in bots]
            return web.json_response({"bots": safe})

        @_require_auth
        async def api_bot_save(request):
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                return web.json_response({"error": "bad json"}, status=400)
            bot_id = body.get("id")
            saved = await characters.save_bot(body, bot_id)
            if not saved:
                return web.json_response({"error": "save failed"}, status=500)
            return web.json_response({"ok": True, "bot": {
                k: v for k, v in saved.items() if k != "discord_token"}})

        @_require_auth
        async def api_bot_delete(request):
            bot_id = request.match_info["bot_id"]
            ok = await characters.delete_bot(bot_id)
            return web.json_response({"ok": ok})

        @_require_auth
        async def api_test_key(request):
            """Test the GROQ_API_KEY and report the exact result."""
            key = os.environ.get("GROQ_API_KEY", "")
            if not key:
                return web.json_response({"ok": False, "error": "GROQ_API_KEY is not set on Render"})
            result = {"ok": False, "key_prefix": key[:7] + "…", "key_length": len(key)}
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": "openai/gpt-oss-20b",
                              "messages": [{"role": "user", "content": "Say OK"}],
                              "max_tokens": 10},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as r:
                        result["status"] = r.status
                        try:
                            body = await r.json()
                        except Exception:  # noqa: BLE001
                            body = (await r.text())[:500]
                        if r.status == 200:
                            result["ok"] = True
                            try:
                                result["reply"] = body["choices"][0]["message"]["content"][:200]
                            except Exception:  # noqa: BLE001
                                result["reply"] = str(body)[:200]
                        else:
                            result["error"] = str(body)[:500]
            except Exception as e:  # noqa: BLE001
                result["error"] = f"{type(e).__name__}: {e}"
            return web.json_response(result)

        app.router.add_get("/dashboard/login", login_page)
        app.router.add_post("/dashboard/login", login_post)
        app.router.add_get("/dashboard", dashboard_page)
        app.router.add_get("/api/characters", api_list)
        app.router.add_post("/api/characters", api_save)
        app.router.add_delete("/api/characters/{char_id}", api_delete)
        app.router.add_post("/api/test-chat", api_test_chat)
        app.router.add_get("/api/lorebook", api_lore_list)
        app.router.add_post("/api/lorebook", api_lore_save)
        app.router.add_delete("/api/lorebook/{entry_id}", api_lore_delete)
        app.router.add_get("/api/bots", api_bot_list)
        app.router.add_post("/api/bots", api_bot_save)
        app.router.add_delete("/api/bots/{bot_id}", api_bot_delete)
        app.router.add_get("/api/test-key", api_test_key)

        global _WEB_STARTED
        if not _WEB_STARTED:
            _WEB_STARTED = True
            runner = web.AppRunner(app)
            await runner.setup()
            port = int(os.environ.get("PORT", "8000"))
            await web.TCPSite(runner, "0.0.0.0", port).start()
            print(f"web listening on 0.0.0.0:{port}", flush=True)
            # Seed Murray on first run (needs Supabase configured).
            asyncio.create_task(characters.ensure_seed())

    async def _ensure_voice(self):
        """Create Murray's cloned voice on first run if needed."""
        await asyncio.sleep(5)  # let the bot finish starting
        if (os.environ.get("ELEVENLABS_VOICE_ID")
                or not os.environ.get("ELEVENLABS_API_KEY")):
            return
        here = os.path.dirname(os.path.abspath(__file__))
        clip = os.path.join(here, "murray-voice.mp3")
        if not os.path.exists(clip):
            print("voice clip not found, skipping clone", flush=True)
            return
        print("creating Murray's cloned voice…", flush=True)
        with open(clip, "rb") as f:
            voice_id = await voice_mod.create_cloned_voice(
                "Old Man Murray", f.read())
        if voice_id:
            print(f"MURRAY_VOICE_ID={voice_id}", flush=True)
            print("Add that as ELEVENLABS_VOICE_ID on Render.", flush=True)
            return
        # Cloning needs a paid plan — fall back to listing stock voices
        # so we can pick an elderly one.
        print("clone unavailable, listing stock voices…", flush=True)
        voices = await voice_mod.list_voices()
        for v in voices:
            labels = v.get("labels", {})
            print(f"VOICE name={v.get('name')} id={v.get('voice_id')} "
                  f"age={labels.get('age')} gender={labels.get('gender')} "
                  f"use={v.get('category')}", flush=True)

    async def get_text_lounge_id(self):
        char = await self.get_character()
        if char and char.get("text_channel_id"):
            try:
                return int(char["text_channel_id"])
            except (TypeError, ValueError):
                return None
        return None

    async def get_hangout_id(self):
        char = await self.get_character()
        if char and char.get("voice_channel_id"):
            try:
                return int(char["voice_channel_id"])
            except (TypeError, ValueError):
                return None
        return None

    async def _join_hangout(self, guild):
        """Connect to Murray's dedicated voice channel. Returns vc or None."""
        hangout_id = await self.get_hangout_id()
        if not hangout_id:
            return None
        channel = guild.get_channel(hangout_id)
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            return None
        vc = guild.voice_client
        try:
            if vc is not None and vc.is_connected():
                if vc.channel.id == hangout_id:
                    return vc
                await vc.move_to(channel)
            else:
                cls = voice_recv.VoiceRecvClient if HAVE_VR else discord.VoiceClient
                await channel.connect(cls=cls)
            return guild.voice_client
        except Exception as e:  # noqa: BLE001
            print(f"hangout join failed: {e}", flush=True)
            return None

    def _start_listening(self, guild, vc):
        old = self._listeners.get(guild.id)
        if old:
            old.stop()

        async def on_utterance(text, _guild=guild):
            reply = await self._respond("someone in voice", text,
                                        _guild.id, None)
            if reply:
                await _speak_in_voice(_guild, reply)

        listener = listen_mod.Listener(self, vc, on_utterance)
        if listener.start():
            self._listeners[guild.id] = listener
            return True
        return False

    async def on_ready(self):
        print(f"logged in as {self.user} ({self.user.id})", flush=True)
        for guild in self.guilds:
            vc = await self._join_hangout(guild)
            if vc:
                if self._start_listening(guild, vc):
                    print(f"hangout active in {guild.name}", flush=True)
        try:
            synced = await self.tree.sync()
            print(f"synced {len(synced)} global command(s)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"command sync failed: {e}", flush=True)

    async def _respond(self, name, text, guild_id=None, channel_id=None):
        char = await self.get_character()
        system = (characters.build_system_prompt(char)
                  if char else characters.build_system_prompt(
                      characters.MURRAY_SEED))
        display = (char or {}).get("name") or name
        max_tokens = characters.get_max_tokens(char)
        # Inject any lorebook entries whose keywords match this message.
        lore = ""
        if char and char.get("id"):
            lore = await characters.get_relevant_lore(text, char["id"])
        if lore:
            system = system + f"\nRelevant background:\n{lore}"
        # Recent conversation history so he stays on topic.
        history = self._history.get((guild_id, channel_id), [])
        reply = await ai_chat(system, f"{display} talking to {name}: {text}",
                             max_tokens=max_tokens,
                             temperature=characters.get_temperature(char),
                             frequency_penalty=characters.get_repetition_penalty(char),
                             history=history)
        if reply and char:
            reply = characters.apply_banned_phrases(reply, char)
        if not reply:
            # Never go silent — fall back to an in-character shrug.
            reply = "Bah, lost my train of thought there. Run that by me again."
        # Remember this exchange for next time.
        if guild_id is not None and channel_id is not None:
            key = (guild_id, channel_id)
            hist = self._history.setdefault(key, [])
            hist.append({"role": "user",
                         "content": f"{name}: {text[:500]}"})
            hist.append({"role": "assistant", "content": reply[:500]})
            self._history[key] = hist[-20:]
        return reply

    async def _handle_voice_note(self, message, attachment):
        """Download a voice note, transcribe with Whisper, reply as Murray."""
        try:
            async with message.channel.typing():
                data = await attachment.read()
                if not data or len(data) > 25 * 1024 * 1024:
                    return
                text = await listen_mod.transcribe(
                    data, filename=attachment.filename or "note.ogg")
                if not text:
                    await message.reply(
                        "Couldn't make out that voice note, kid. Try again.",
                        mention_author=False)
                    return
                reply = await self._respond(
                    message.author.display_name,
                    f"[voice note transcript: {text}]",
                    message.guild.id if message.guild else None,
                    message.channel.id)
                if reply:
                    # Murray answers voice notes with a voice message.
                    audio_bytes = None
                    try:
                        audio_bytes = await voice_mod.text_to_speech(
                            "", reply[:500])
                    except Exception:
                        audio_bytes = None
                    files = []
                    if audio_bytes:
                        files.append(discord.File(
                            io.BytesIO(audio_bytes),
                            filename="murray-voice-message.mp3"))
                    await message.reply(
                        reply[:1500],
                        mention_author=False,
                        files=files if files else None)
                    await _speak_in_voice(message.guild, reply)
        except Exception as e:  # noqa: BLE001
            print(f"voice note failed: {e}", flush=True)

    async def on_message(self, message):
        if message.guild is None or message.author.bot:
            return
        content = (message.content or "").strip()
        mentioned = self.user in message.mentions
        addressed = content.lower().startswith("murray")
        lounge_id = await self.get_text_lounge_id()
        in_lounge = lounge_id is not None and message.channel.id == lounge_id
        # Voice notes: audio attachments get transcribed and answered.
        audio = None
        for att in message.attachments:
            ct = (att.content_type or "").lower()
            name = (att.filename or "").lower()
            if (ct.startswith("audio/") or
                    name.endswith((".ogg", ".mp3", ".m4a", ".wav",
                                   ".oga", ".webm", ".flac"))):
                audio = att
                break
        if audio is not None and (mentioned or addressed or in_lounge
                                   or not content):
            await self._handle_voice_note(message, audio)
            return
        if not content or content.startswith(("/", "!")):
            return
        if not (mentioned or addressed or in_lounge):
            return
        now = time.time()
        key = (message.guild.id, message.author.id)
        if now - self._cooldown.get(key, 0) < 10:
            return
        self._cooldown[key] = now
        try:
            async with message.channel.typing():
                reply = await self._respond(
                    message.author.display_name, content,
                    message.guild.id, message.channel.id)
                if reply:
                    await message.reply(reply[:1500], mention_author=False)
                    await _speak_in_voice(message.guild, reply)
        except Exception as e:  # noqa: BLE001
            print(f"reply failed: {e}", file=sys.stderr, flush=True)


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Character Dashboard — Login</title>
<style>body{background:#111;color:#f5c518;font-family:sans-serif;
padding:2em;max-width:28em;margin:auto}input,button{font-size:1.1em;
padding:.5em;margin:.3em 0;width:100%;box-sizing:border-box}
button{background:#f5c518;color:#111;border:none;font-weight:bold}</style>
</head><body><h1>Character Dashboard</h1>
<form method="post" action="/dashboard/login">
<label>Password<br><input type="password" name="password" autofocus></label>
<button type="submit">Log in</button></form></body></html>"""

FFMPEG = "./ffmpeg" if os.path.exists("./ffmpeg") else "ffmpeg"


async def _speak_in_voice(guild, text):
    """If the bot is in a voice channel in this guild, speak text there."""
    if not (text or "").strip():
        return
    vc = guild.voice_client
    if vc is None or not vc.is_connected():
        return
    audio = await voice_mod.text_to_speech("", text)
    if not audio:
        return
    tmp = "/tmp/murray_reply.mp3"
    with open(tmp, "wb") as f:
        f.write(audio)
    # Wait for any current audio to finish, then play.
    while vc.is_playing():
        await asyncio.sleep(0.5)
    try:
        vc.play(discord.FFmpegPCMAudio(tmp, executable=FFMPEG))
    except Exception as e:  # noqa: BLE001
        print(f"voice play failed: {e}", flush=True)


def register_commands(bot):
    """Register slash commands on a bot instance."""
    @bot.tree.command(name="murray", description="Ask the character something.")
    @app_commands.describe(question="What do you want to ask?")
    async def murray_cmd(interaction: discord.Interaction, question: str):
        await interaction.response.defer()
        reply = await interaction.client._respond(interaction.user.display_name,
                                   question[:500],
                                   interaction.guild.id if interaction.guild else None,
                                   interaction.channel.id if interaction.channel else None)
        if not reply:
            await interaction.followup.send(
                "Hmm, no answer came back. Try again in a bit.")
            return
        await interaction.followup.send(reply[:1500])


    @bot.tree.command(name="join",
                      description="Bring the character to his hangout channel.")
    async def join_cmd(interaction: discord.Interaction):
        await interaction.response.defer()
        vc = await interaction.client._join_hangout(interaction.guild)
        if vc is None:
            await interaction.followup.send(
                "No hangout set. Use /sethangout to pick my channel first.")
            return
        if interaction.client._start_listening(interaction.guild, vc):
            await interaction.followup.send(
                "Alright, I'm in my hangout and listening. Come talk to me.")
        else:
            await interaction.followup.send("Alright, I'm in my hangout.")


    @bot.tree.command(name="leave",
                      description="Send the character out of voice.")
    async def leave_cmd(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_connected():
            await interaction.response.send_message(
                "I'm not in voice.", ephemeral=True)
            return
        old = interaction.client._listeners.pop(interaction.guild.id, None)
        if old:
            old.stop()
        await vc.disconnect()
        await interaction.response.send_message("Fine, I'm leaving.")


    @bot.tree.command(name="say",
                      description="Make the character say something out loud.")
    @app_commands.describe(text="What should he say?")
    async def say_cmd(interaction: discord.Interaction, text: str):
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_connected():
            await interaction.response.send_message(
                "Get me into a voice channel with /join first.",
                ephemeral=True)
            return
        await interaction.response.defer()
        await _speak_in_voice(interaction.guild, text[:500])
        await interaction.followup.send("Said it.")


    @bot.tree.command(name="sethangout",
                      description="Pick Murray's dedicated voice channel.")
    @app_commands.describe(channel="His hangout — he'll only talk here.")
    async def sethangout_cmd(interaction: discord.Interaction,
                             channel: discord.VoiceChannel):
        await interaction.response.defer(ephemeral=True)
        char = await interaction.client.get_character()
        if char and char.get("id"):
            saved = await characters.save_character(
                {"voice_channel_id": str(channel.id)}, char["id"])
            if saved:
                interaction.client._char_cache = None
                # Move him there now.
                old_vc = interaction.guild.voice_client
                if old_vc and old_vc.is_connected():
                    try:
                        await old_vc.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                old_l = interaction.client._listeners.pop(interaction.guild.id, None)
                if old_l:
                    old_l.stop()
                vc = await interaction.client._join_hangout(interaction.guild)
                if vc and interaction.client._start_listening(interaction.guild, vc):
                    await interaction.followup.send(
                        f"My hangout is {channel.name}. Come find me there, kid.")
                elif vc:
                    await interaction.followup.send(
                        f"My hangout is {channel.name}.")
                else:
                    await interaction.followup.send(
                        "Saved, but I couldn't join it. Check my permissions.")
                return
        await interaction.followup.send("Couldn't save that. Try again.")


    @bot.tree.command(name="setlounge",
                      description="Pick Murray's text lounge — he answers here.")
    @app_commands.describe(channel="His text channel — no mention needed.")
    async def setlounge_cmd(interaction: discord.Interaction,
                            channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        char = await interaction.client.get_character()
        if char and char.get("id"):
            saved = await characters.save_character(
                {"text_channel_id": str(channel.id)}, char["id"])
            if saved:
                interaction.client._char_cache = None
                await interaction.followup.send(
                    f"My lounge is {channel.name}. Just type, kid — I'm listening.")
                return
        await interaction.followup.send("Couldn't save that. Try again.")



async def _bot_watcher():
    """Poll for new/removed bots so dashboard changes take effect live."""
    while True:
        await asyncio.sleep(60)
        try:
            seen = set()
            for brow in await characters.list_bots():
                bid = brow["id"]
                tok = (brow.get("discord_token") or "").strip()
                if brow.get("is_active") and tok:
                    seen.add(bid)
                    if bid not in BOTS:
                        b = MurrayBot(bot_id=bid)
                        register_commands(b)
                        BOTS[bid] = b
                        print(f"starting new Discord bot {bid}…", flush=True)
                        asyncio.create_task(b.start(tok))
            # Stop bots that were deactivated or deleted.
            for bid in list(BOTS):
                if bid is not None and bid not in seen:
                    b = BOTS.pop(bid)
                    print(f"stopping Discord bot {bid}…", flush=True)
                    try:
                        await b.close()
                    except Exception:
                        pass
        except Exception as e:  # noqa: BLE001
            print(f"bot watcher: {e}", flush=True)


async def main():
    """Start all configured Discord bots (web server starts via setup_hook)."""
    to_start = []
    env_token = os.environ.get("DISCORD_TOKEN")
    if env_token:
        to_start.append((None, env_token))
    for brow in await characters.list_bots():
        tok = (brow.get("discord_token") or "").strip()
        if brow.get("is_active") and tok:
            to_start.append((brow["id"], tok))
    if not to_start:
        print("No Discord bots configured.", flush=True)
    for bot_id, token in to_start:
        b = MurrayBot(bot_id=bot_id)
        register_commands(b)
        BOTS[bot_id] = b
        print(f"starting Discord bot {bot_id or 'default'}…", flush=True)
        asyncio.create_task(b.start(token))
    asyncio.create_task(_bot_watcher())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
