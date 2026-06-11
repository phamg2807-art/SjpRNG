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
        
        # Ensure file exists
        if not os.path.exists(path):
            with open(path, 'w') as f:
                json.dump({}, f)
        
        # Load with error handling
        try:
            with open(path, 'r') as f:
                content = json.load(f)
                GAME_DATA[name] = content
                print(f"✅ Loaded {file}")
        except json.JSONDecodeError:
            print(f"⚠️ {file} was corrupted/empty. Resetting to empty object.")
            GAME_DATA[name] = {}
            with open(path, 'w') as f:
                json.dump({}, f)

# ─── Combat Math Logic ───────────────────────────────────────────────────────
def calculate_damage(player, weapon, enemy):
    base_dmg = weapon.get('dmg', 5)
    w_type = weapon.get('type', 'physical')
    
    if w_type == 'physical':
        total_dmg = base_dmg * (1 + (player.get('st', 10) / 100))
    elif w_type == 'magical':
        total_dmg = base_dmg * (1 + (player.get('mn', 10) / 100))
    else:
        total_dmg = base_dmg
        
    enemy_df = enemy.get('df', 0)
    final_dmg = max(1, total_dmg - enemy_df)
    
    return round(final_dmg)

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
                    hp INT DEFAULT 100, st INT DEFAULT 10, df INT DEFAULT 10, mn INT DEFAULT 10,
                    unallocated_points INT DEFAULT 0,
                    gold INT DEFAULT 0, xp INT DEFAULT 0, level INT DEFAULT 1,
                    current_map INT DEFAULT 1, current_stage INT DEFAULT 1
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS parties (
                    party_id SERIAL PRIMARY KEY,
                    leader_id BIGINT UNIQUE,
                    members BIGINT[],
                    in_dungeon BOOLEAN DEFAULT FALSE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    id SERIAL PRIMARY KEY, name TEXT, rarity TEXT, min_level INT, stats JSONB
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    id SERIAL PRIMARY KEY, player_id BIGINT REFERENCES players(user_id),
                    item_id INT REFERENCES items(id), equipped BOOLEAN DEFAULT FALSE
                );
            """)
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

# ─── UI Components ────────────────────────────────────────────────────────────
class DungeonView(View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="🔄 Refresh Status", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: Button):
        # Fetch current player data from DB
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM players WHERE user_id = %s", (interaction.user.id,))
                player = cur.fetchone()

        if not player:
            await interaction.response.send_message("❌ You are not in the dungeon! Type `-dungeon` to join.", ephemeral=True)
            return

        # Simple 10-char HP bar
        hp_percent = max(0, min(10, int(player['hp'] / 10))) 
        hp_bar = "█" * hp_percent + "░" * (10 - hp_percent)

        embed = discord.Embed(
            title="⚔️ Dungeon: Forest of Echoes", 
            description=f"Status: **Fighting**", 
            color=discord.Color.green()
        )
        embed.add_field(name="HP", value=f"{hp_bar} {player['hp']} HP", inline=False)
        embed.add_field(name="Level", value=f"Lv.{player['level']}", inline=True)
        embed.add_field(name="Stage", value=f"{player['current_stage']}/5", inline=True)
        embed.set_footer(text="Data updated in real-time.")
        
        await interaction.response.edit_message(embed=embed)

# ─── Idle Combat Loop ─────────────────────────────────────────────────────────
@tasks.loop(seconds=30.0)
async def idle_combat_loop():
    # Combat processing logic goes here
    pass

# ─── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    load_game_data()
    idle_combat_loop.start()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

# ─── Commands ─────────────────────────────────────────────────────────────────
@bot.command()
async def help(ctx):
    help_text = """
    **⚔️ Dungeon Quest Bot - Commands**
    
    `-dungeon`: Enter the dungeon and start idle farming.
    `-stats`: View your current stats and unallocated points.
    `-upgrade [stat] [amount]`: Upgrade your HP, ST, DF, or MN.
    `-party_create`: Create a party to tackle dungeons with friends.
    `-shop`: View the daily shop rotation.

    **Instructions:**
    1. **Builds:** Spend points using `-upgrade`. ST increases physical weapon power, MN increases magical weapon power.
    2. **Combat:** Your party fights automatically while in a dungeon.
    3. **Economy:** Sell items in the market or buy from the global shop.
    """
    try:
        await ctx.author.send(help_text)
        await ctx.send("✅ Check your DMs for the help guide!")
    except discord.Forbidden:
        await ctx.send("❌ I couldn't send you a DM. Please enable DMs from server members.")

@bot.command()
async def stats(ctx):
    await ctx.send("Your Stats: HP 100 | ST 10 | DF 10 | MN 10 | Points: 5")

@bot.command()
async def upgrade(ctx, stat: str, amount: int):
    await ctx.send(f"Upgraded {stat} by {amount}!")

@bot.command()
async def party_create(ctx):
    await ctx.send("Party created! Invite friends with -invite")

@bot.command()
async def dungeon(ctx):
    # Ensure player exists in DB for this demo
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO players (user_id) VALUES (%s) 
                ON CONFLICT (user_id) DO NOTHING
            """, (ctx.author.id,))
        conn.commit()

    embed = discord.Embed(
        title="⚔️ Dungeon: Forest of Echoes",
        description="Your party is currently fighting in the forest.",
        color=discord.Color.green()
    )
    embed.add_field(name="Status", value="Fighting...", inline=False)
    embed.add_field(name="Party HP", value="[██████████] 100%", inline=True)
    embed.add_field(name="Progress", value="Stage 1/5", inline=True)
    
    await ctx.send(embed=embed, view=DungeonView())

# ─── Run ──────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
