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
        path = os.path.join(data_dir, file)
        if os.path.exists(path):
            with open(path, 'r') as f:
                GAME_DATA[file.replace('.json', '')] = json.load(f)
            print(f"✅ Loaded {file}")
        else:
            with open(path, 'w') as f:
                json.dump({}, f)
            print(f"⚠️ Created empty {file}")

# ─── Flask Keep-Alive ─────────────────────────────────────────────────────────
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── DB Pool ──────────────────────────────────────────────────────────────────
DB_URL = os.getenv('DATABASE_URL') or exit("ERROR: DATABASE_URL missing!")
_pool = None

def get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(
            2, 10,
            dsn=DB_URL,
            cursor_factory=RealDictCursor,
            sslmode='require',
            connect_timeout=10
        )
    return _pool

class _Conn:
    def __enter__(self):
        self.conn = get_pool().getconn()
        return self.conn
    def __exit__(self, exc_type, *_):
        if exc_type:
            self.conn.rollback()
        get_pool().putconn(self.conn)

def get_conn():
    return _Conn()

# ─── Redis Setup ──────────────────────────────────────────────────────────────
REDIS_URL = os.getenv('REDIS_URL') or exit("ERROR: REDIS_URL missing!")
rd = redis.from_url(REDIS_URL, decode_responses=True)

# ─── DB Init ──────────────────────────────────────────────────────────────────
def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Enhanced Player Table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    user_id BIGINT PRIMARY KEY,
                    hp INT DEFAULT 100, st INT DEFAULT 10, df INT DEFAULT 10, mn INT DEFAULT 10,
                    unallocated_points INT DEFAULT 0,
                    gold INT DEFAULT 0, xp INT DEFAULT 0, level INT DEFAULT 1,
                    current_map INT DEFAULT 1, current_stage INT DEFAULT 1
                );
            """)
            # Parties Table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS parties (
                    party_id SERIAL PRIMARY KEY,
                    leader_id BIGINT UNIQUE,
                    members BIGINT[],
                    in_dungeon BOOLEAN DEFAULT FALSE
                );
            """)
            # Items Master List
            cur.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    id SERIAL PRIMARY KEY, name TEXT, rarity TEXT, min_level INT, stats JSONB
                );
            """)
            # Inventory
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    id SERIAL PRIMARY KEY, player_id BIGINT REFERENCES players(user_id),
                    item_id INT REFERENCES items(id), equipped BOOLEAN DEFAULT FALSE
                );
            """)
            # Market
            cur.execute("""
                CREATE TABLE IF NOT EXISTS market (
                    id SERIAL PRIMARY KEY, seller_id BIGINT REFERENCES players(user_id),
                    item_id INT REFERENCES items(id), price INT,
                    listed_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
        conn.commit()
    print("✅ Database tables ready")

# ─── Bot Setup ────────────────────────────────────────────────────────────────
TOKEN = os.getenv('DISCORD_TOKEN') or exit("ERROR: DISCORD_TOKEN missing!")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

# ─── Idle Combat Loop ─────────────────────────────────────────────────────────
@tasks.loop(seconds=30.0)
async def idle_combat_loop():
    # Logic: 
    # 1. Fetch all active parties from Redis/DB
    # 2. Iterate through parties
    # 3. Simulate combat (Damage vs Defense)
    # 4. Update status in Redis
    # 5. Distribute XP/Gold
    pass

# ─── UI Components ────────────────────────────────────────────────────────────
class DungeonView(View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="Attack", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("Combat is idle! Your party is fighting automatically.", ephemeral=True)

# ─── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    load_game_data()
    idle_combat_loop.start() # Start the engine
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

# ─── Commands ─────────────────────────────────────────────────────────────────
@bot.command()
async def stats(ctx):
    # Example command to view stats and unallocated points
    await ctx.send("Your Stats: HP 100 | ST 10 | DF 10 | MN 10 | Points: 5")

@bot.command()
async def upgrade(ctx, stat: str, amount: int):
    # Example logic for manual stat distribution
    await ctx.send(f"Upgraded {stat} by {amount}!")

@bot.command()
async def party_create(ctx):
    # Logic to create party in SQL/Redis
    await ctx.send("Party created! Invite friends with -invite")

@bot.command()
async def dungeon(ctx):
    embed = discord.Embed(
        title="⚔️ Dungeon: Idle Mode Active",
        description="Your party is currently fighting in the forest.",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed, view=DungeonView())

# ─── Run ──────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
