"""Old Man Murray — grumpy old metalhead AI Discord bot. Standalone."""
import asyncio
import os
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

from ai import ai_chat

MURRAY_SYSTEM = (
    "You are Old Man Murray, a grumpy old metalhead in his 60s hanging out "
    "on a metal Discord server. You've been listening since Black Sabbath "
    "was new. Your personality:\n"
    "- You call everyone 'kid'.\n"
    "- You think most music after 1991 is garbage, but you say it with "
    "grudging affection, not real hate.\n"
    "- You worship Sabbath, Priest, Maiden, Motorhead. Vinyl only. "
    "Streaming is for cowards.\n"
    "- You roast people's music taste playfully — tease them, don't wound them.\n"
    "- You're grumpy but lovable, like a sitcom grandpa.\n"
    "Rules you NEVER break:\n"
    "- Keep replies short: 1-3 sentences.\n"
    "- Roast music taste only. Never mock someone's body, disability, race, "
    "gender, sexuality, or anything personal. No slurs, ever.\n"
    "- If someone is upset or asks you to stop, drop the act and be kind.\n"
    "- Never claim to be a real person.\n"
    "Talk like a grumpy old roadie, not a chatbot."
)


class MurrayBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self._cooldown = {}

    async def setup_hook(self):
        # Health endpoint for Render.
        app = web.Application()
        app.router.add_get("/health", lambda r: web.Response(text="ok"))
        app.router.add_get("/", lambda r: web.Response(text="ok"))
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", "8000"))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        print(f"health endpoint listening on 0.0.0.0:{port}", flush=True)

    async def on_ready(self):
        print(f"logged in as {self.user} ({self.user.id})", flush=True)
        try:
            synced = await self.tree.sync()
            print(f"synced {len(synced)} global command(s)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"command sync failed: {e}", flush=True)

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
                reply = await ai_chat(
                    MURRAY_SYSTEM,
                    f"{message.author.display_name} says: {content}")
                if reply:
                    await message.reply(reply[:1500], mention_author=False)
        except Exception as e:  # noqa: BLE001
            print(f"Murray reply failed: {e}", file=sys.stderr, flush=True)


bot = MurrayBot()


@bot.tree.command(name="murray",
                  description="Ask Old Man Murray something.")
@app_commands.describe(question="What do you want to ask him?")
async def murray_cmd(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    reply = await ai_chat(
        MURRAY_SYSTEM,
        f"{interaction.user.display_name} asks: {question[:500]}")
    if not reply:
        await interaction.followup.send(
            "Murray grunted and went back to sleep. Try again in a bit.")
        return
    await interaction.followup.send(reply[:1500])


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN is not set.", file=sys.stderr, flush=True)
        sys.exit(1)
    bot.run(token)
