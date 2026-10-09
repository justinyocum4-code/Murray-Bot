"""Old Man Murray — AI character Discord bot + character dashboard."""
import asyncio
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

SESSIONS = {}  # token -> expiry


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
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self._cooldown = {}
        self._char_cache = None
        self._char_cache_at = 0

    async def get_character(self):
        # Cache the active character for 60s so every message isn't a DB hit.
        now = time.time()
        if self._char_cache and now - self._char_cache_at < 60:
            return self._char_cache
        char = await characters.get_active_character()
        if char:
            self._char_cache = char
            self._char_cache_at = now
        return char or self._char_cache

    async def setup_hook(self):
        app = web.Application()
        app.router.add_get("/health", lambda r: web.Response(text="ok"))
        app.router.add_get("/", lambda r: web.Response(text="ok"))

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
            chars = await characters.list_characters()
            # Don't leak anything sensitive; rows are Justin's own.
            return web.json_response({"characters": chars})

        @_require_auth
        async def api_save(request):
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                return web.json_response({"error": "bad json"}, status=400)
            char_id = body.get("id")
            saved = await characters.save_character(body, char_id)
            if not saved:
                return web.json_response(
                    {"error": "save failed (check Supabase setup)"},
                    status=500)
            # Bust the bot's cache so the new personality takes effect.
            self._char_cache = None
            return web.json_response({"ok": True, "character": saved})

        @_require_auth
        async def api_delete(request):
            char_id = request.match_info["char_id"]
            ok = await characters.delete_character(char_id)
            if ok:
                self._char_cache = None
            return web.json_response({"ok": ok})

        app.router.add_get("/dashboard/login", login_page)
        app.router.add_post("/dashboard/login", login_post)
        app.router.add_get("/dashboard", dashboard_page)
        app.router.add_get("/api/characters", api_list)
        app.router.add_post("/api/characters", api_save)
        app.router.add_delete("/api/characters/{char_id}", api_delete)

        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", "8000"))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        print(f"web listening on 0.0.0.0:{port}", flush=True)
        # Seed Murray on first run (needs Supabase configured).
        asyncio.create_task(characters.ensure_seed())
        asyncio.create_task(self._ensure_voice())

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

    async def on_ready(self):
        print(f"logged in as {self.user} ({self.user.id})", flush=True)
        try:
            synced = await self.tree.sync()
            print(f"synced {len(synced)} global command(s)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"command sync failed: {e}", flush=True)

    async def _respond(self, name, text):
        char = await self.get_character()
        system = (characters.build_system_prompt(char)
                  if char else characters.build_system_prompt(
                      characters.MURRAY_SEED))
        display = (char or {}).get("name") or name
        return await ai_chat(system, f"{display} talking to {name}: {text}")

    async def on_message(self, message):
        if message.guild is None or message.author.bot:
            return
        content = (message.content or "").strip()
        if not content or content.startswith(("/", "!")):
            return
        mentioned = self.user in message.mentions
        addressed = content.lower().startswith("murray")
        if not (mentioned or addressed):
            return
        now = time.time()
        key = (message.guild.id, message.author.id)
        if now - self._cooldown.get(key, 0) < 10:
            return
        self._cooldown[key] = now
        try:
            async with message.channel.typing():
                reply = await self._respond(
                    message.author.display_name, content)
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
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "")
    if not voice_id or not (text or "").strip():
        return
    vc = guild.voice_client
    if vc is None or not vc.is_connected():
        return
    audio = await voice_mod.text_to_speech(voice_id, text)
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


bot = MurrayBot()


@bot.tree.command(name="murray", description="Ask the character something.")
@app_commands.describe(question="What do you want to ask?")
async def murray_cmd(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    reply = await bot._respond(interaction.user.display_name,
                               question[:500])
    if not reply:
        await interaction.followup.send(
            "Hmm, no answer came back. Try again in a bit.")
        return
    await interaction.followup.send(reply[:1500])


@bot.tree.command(name="join",
                  description="Bring the character into your voice channel.")
async def join_cmd(interaction: discord.Interaction):
    user_vc = (interaction.user.voice.channel
               if interaction.user.voice else None)
    if user_vc is None:
        await interaction.response.send_message(
            "Join a voice channel first, then call me in.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        vc = interaction.guild.voice_client
        if vc is not None and vc.is_connected():
            await vc.move_to(user_vc)
        else:
            await user_vc.connect()
        await interaction.followup.send("Alright, I'm here. What?")
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"Couldn't join: {e}")


@bot.tree.command(name="leave",
                  description="Send the character out of voice.")
async def leave_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_connected():
        await interaction.response.send_message(
            "I'm not in voice.", ephemeral=True)
        return
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


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN is not set.", file=sys.stderr, flush=True)
        sys.exit(1)
    bot.run(token)
