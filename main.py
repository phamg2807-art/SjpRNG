import os
import sys
import json
import redis
import discord
import psycopg2
import asyncio
from flask import Flask
from threading import Thread
from discord.ext import commands, tasks
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from discord.ui import Button, View

sys.stdout.reconfigure(line_buffering=True)

# ─── Game Data (Global Storage) ───────────────────────────────────────────────
GAME_DATA = {}

def load_game_data():
    global GAME_DATA
    data_dir = "data"
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
        print(f"📁 Created {data_dir} directory.")
    
    files = ['enemies.json', 'weapons.json', 'armors.json', 'maps.json']
    for file in files:
        name = file.replace('.json', '')
        path = os.path.join(data_dir, file)
        if not os.path.exists(path):
            with open(path, 'w') as f:
                json.dump({}, f)
        try:
            with open(path, 'r') as f:
                GAME_DATA[name] = json.load(f)
                print(f"✅ Loaded {file}")
        except json.JSONDecodeError:
            print(f"⚠️ {file} was corrupted. Resetting.")
            GAME_DATA[name] = {}
            with open(path, 'w') as f:
                json.dump({}, f)

# ─── Flask Keep-Alive ─────────────────────────────────────────────────────────
app = Flask('')
@app.route('/')
def home(): return "Bot is alive!"
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── DB & Redis ───────────────────────────────────────────────────────────────
DB_URL = os.getenv('DATABASE_URL') or exit("ERROR: DATABASE_URL missing!")
_pool = ThreadedConnectionPool(2, 10, dsn=DB_URL, cursor_factory=RealDictCursor, sslmode='require')

def get_conn(): return _pool.getconn()
def put_conn(conn): _pool.putconn(conn)

REDIS_URL = os.getenv('REDIS_URL') or exit("ERROR: REDIS_URL missing!")
rd = redis.from_url(REDIS_URL, decode_responses=True)

# ─── Bot Setup ────────────────────────────────────────────────────────────────
TOKEN = os.getenv('DISCORD_TOKEN') or exit("ERROR: DISCORD_TOKEN missing!")
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

# ─── UI Components ────────────────────────────────────────────────────────────
class DungeonView(View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="🔄 Refresh Status", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: Button):
        # Create an updated embed
        embed = discord.Embed(title="⚔️ Dungeon: Forest of Echoes", color=discord.Color.blue())
        embed.add_field(name="Status", value="Fighting... (Idle)", inline=False)
        embed.add_field(name="Party HP", value="[██████████] 100%", inline=True)
        embed.add_field(name="Progress", value="Stage 1/5", inline=True)
        
        await interaction.response.edit_message(embed=embed)

# ─── Idle Combat Loop ─────────────────────────────────────────────────────────
@tasks.loop(seconds=30.0)
async def idle_combat_loop():
    # Combat logic will be added here
    pass

# ─── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    load_game_data()
    idle_combat_loop.start()
    print(f"✅ Logged in as {bot.user}")

# ─── Commands ─────────────────────────────────────────────────────────────────
@bot.command()
async def help(ctx):
    help_text = "**⚔️ Dungeon Quest - Commands**\n`-dungeon`: Enter dungeon\n`-stats`: Check stats\n`-upgrade`: Use points\n`-party_create`: Create party\n`-shop`: Daily items"
    try: 
        await ctx.author.send(help_text)
        await ctx.send("✅ Check your DMs!")
    except: await ctx.send("❌ Enable DMs to see help!")

@bot.command()
async def dungeon(ctx):
    embed = discord.Embed(
        title="⚔️ Dungeon: Forest of Echoes", 
        description="Your party is exploring the depths.", 
        color=discord.Color.blue()
    )
    embed.add_field(name="Status", value="Fighting...", inline=False)
    embed.add_field(name="Party HP", value="[██████████] 100%", inline=True)
    embed.add_field(name="Progress", value="Stage 1/5", inline=True)
    
    await ctx.send(embed=embed, view=DungeonView())

# ─── Run ──────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
