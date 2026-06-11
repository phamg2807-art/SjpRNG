import os
import sys
import json
import redis
import discord
import psycopg2
from flask import Flask
from threading import Thread
from discord.ext import commands
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
            # Create empty file if not exists
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    user_id BIGINT PRIMARY KEY,
                    hp INT DEFAULT 100,
                    st INT DEFAULT 10,
                    df INT DEFAULT 10,
                    mn INT DEFAULT 10,
                    gold INT DEFAULT 0,
                    xp INT DEFAULT 0,
                    level INT DEFAULT 1,
                    current_map INT DEFAULT 1,
                    current_stage INT DEFAULT 1
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    rarity TEXT,
                    min_level INT,
                    stats JSONB
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    id SERIAL PRIMARY KEY,
                    player_id BIGINT REFERENCES players(user_id),
                    item_id INT REFERENCES items(id),
                    equipped BOOLEAN DEFAULT FALSE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS market (
                    id SERIAL PRIMARY KEY,
                    seller_id BIGINT REFERENCES players(user_id),
                    item_id INT REFERENCES items(id),
                    price INT,
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

# ─── UI Components ────────────────────────────────────────────────────────────
class DungeonView(View):
    def __init__(self):
        super().__init__(timeout=60)
    
    @discord.ui.button(label="Attack", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("You attacked the monster!", ephemeral=True)

    @discord.ui.button(label="Heal", style=discord.ButtonStyle.success)
    async def heal(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("You recovered 20 HP!", ephemeral=True)

# ─── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    load_game_data() # Load JSON files here
    try:
        rd.set("test", "hello")
        val = rd.get("test")
        print(f"✅ Redis test: {val}")
    except Exception as e:
        print(f"❌ Redis error: {e}")
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

# ─── Commands ─────────────────────────────────────────────────────────────────
@bot.command()
async def dungeon(ctx):
    # Example: How to access your data
    # monster = GAME_DATA['enemies'].get('slime') 
    embed = discord.Embed(
        title="⚔️ Dungeon: Forest of Echoes",
        description="Stage 1/5 | Monster: Slime",
        color=discord.Color.red()
    )
    await ctx.send(embed=embed, view=DungeonView())

# ─── Run ──────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
