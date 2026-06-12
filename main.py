import os
import sys
import discord
import asyncio
from discord.ext import commands
from threading import Thread
from flask import Flask
from database import init_db
from tasks import start_tasks

sys.stdout.reconfigure(line_buffering=True)

# ─── Flask Keep-Alive ─────────────────────────────────────────────────────────
app = Flask('')
@app.route('/')
def home(): return "CoinBot is alive!"
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── Bot Setup ────────────────────────────────────────────────────────────────
TOKEN = os.getenv('DISCORD_TOKEN') or exit("ERROR: DISCORD_TOKEN missing!")
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

COGS = ['cogs.economy', 'cogs.coins', 'cogs.trading', 'cogs.marketplace', 'cogs.profile', 'cogs.leaderboard', 'cogs.admin']

@bot.event
async def on_ready():
    init_db()
    start_tasks(bot)
    for cog in COGS:
        await bot.load_extension(cog)
    print(f"✅ CoinBot online as {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    # Award 1 credit per message
    from database import award_credit
    award_credit(message.author.id, message.author.name)
    await bot.process_commands(message)

bot.run(TOKEN)
