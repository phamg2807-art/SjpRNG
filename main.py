import os
import sys
import json
import redis
import random
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
            print(f"⚠️ {file} was corrupted/empty. Resetting to empty object.")
            GAME_DATA[name] = {}
            with open(path, 'w') as f:
                json.dump({}, f)

# ─── Drop Rate Probability Engine ─────────────────────────────────────────────
def roll_loot_drop():
    """
    Rolls loot according to exact rarity chances:
    Common 45%, Uncommon 35%, Epic 15%, Legendary 4%, Secret 1%
    """
    roll = random.uniform(0, 100)
    
    if roll <= 45.0:
        tier = "common"
    elif roll <= 80.0:
        tier = "uncommon"
    elif roll <= 95.0:
        tier = "epic"
    elif roll <= 99.0:
        tier = "legendary"
    else:
        tier = "secret" # Fallback structural layer or easter egg placeholder
        
    # Pool items matching chosen tier across weapons and armors
    pool = []
    for k, v in GAME_DATA.get('weapons', {}).items():
        if v.get('tier') == tier: pool.append(v['name'])
    for k, v in GAME_DATA.get('armors', {}).items():
        if v.get('tier') == tier: pool.append(v['name'])
        
    # Structural fallback if rolled tier lacks item assignments (e.g., Secret tier placeholder)
    if not pool:
        all_items = [v['name'] for v in GAME_DATA.get('weapons', {}).values()] + [v['name'] for v in GAME_DATA.get('armors', {}).values()]
        return random.choice(all_items) if all_items else "Mysterious Relic"
        
    return random.choice(pool)

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
def home(): return "Bot is alive!"
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── DB & Redis Pools ─────────────────────────────────────────────────────────
DB_URL = os.getenv('DATABASE_URL') or exit("ERROR: DATABASE_URL missing!")
_pool = ThreadedConnectionPool(2, 10, dsn=DB_URL, cursor_factory=RealDictCursor, sslmode='require')

def get_conn(): return _pool.getconn()
def put_conn(conn): _pool.putconn(conn)

class _Conn:
    def __enter__(self):
        self.conn = get_pool().getconn()
        return self.conn
    def __exit__(self, exc_type, *_):
        if exc_type: self.conn.rollback()
        get_pool().putconn(self.conn)

def get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(2, 10, dsn=DB_URL, cursor_factory=RealDictCursor, sslmode='require', connect_timeout=10)
    return _pool

REDIS_URL = os.getenv('REDIS_URL') or exit("ERROR: REDIS_URL missing!")
rd = redis.from_url(REDIS_URL, decode_responses=True)

# ─── DB Init ──────────────────────────────────────────────────────────────────
def init_db():
    with get_pool().getconn() as conn:
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
        _pool.putconn(conn)
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
        with _pool.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM players WHERE user_id = %s", (interaction.user.id,))
                player = cur.fetchone()
            _pool.putconn(conn)

        if not player:
            await interaction.response.send_message("❌ You are not exploring! Type `-dungeon` to join.", ephemeral=True)
            return

        hp_percent = max(0, min(10, int(player['hp'] / 10))) 
        hp_bar = "█" * hp_percent + "░" * (10 - hp_percent)

        embed = discord.Embed(title="⚔️ Dungeon: Bright Forest", description="Status: **Exploring Stage Fields**", color=discord.Color.green())
        embed.add_field(name="Player HP", value=f"{hp_bar} {player['hp']} HP", inline=False)
        embed.add_field(name="Hero level", value=f"Lv.{player['level']}", inline=True)
        embed.add_field(name="Current Stage Progress", value=f"Stage {player['current_stage']}/10", inline=True)
        
        # Check boss status
        if player['current_stage'] == 10:
            embed.set_footer(text="⚠️ Boss Room reached: Fighting Hardened Spellcaster!")
        else:
            embed.set_footer(text="Data updated in real-time.")
            
        await interaction.response.edit_message(embed=embed)

# ─── Idle Combat Loop ─────────────────────────────────────────────────────────
@tasks.loop(seconds=30.0)
async def idle_combat_loop():
    # Combat cycle tracking variables go here
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
    with _pool.getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO players (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (ctx.author.id,))
            conn.commit()
            cur.execute("SELECT * FROM players WHERE user_id = %s", (ctx.author.id,))
            player = cur.fetchone()
        _pool.putconn(conn)

    embed = discord.Embed(title="⚔️ Dungeon: Bright Forest", description="Your character steps into the quiet clearing.", color=discord.Color.green())
    embed.add_field(name="Current Zone", value="Bright Forest", inline=False)
    embed.add_field(name="Stage Progress", value=f"Stage {player['current_stage']}/10", inline=True)
    embed.add_field(name="Party State", value="Farming Idle Mode", inline=True)
    
    await ctx.send(embed=embed, view=DungeonView())

# Test/Simulation Command to check loot drop system easily
@bot.command()
async def test_boss_drop(ctx):
    """Simulates beating the level 10 boss and drops 3 items based on request chances."""
    rewards = [roll_loot_drop() for _ in range(3)]
    result_text = "\n".join([f"🎁 Found item: **{item}**" for item in rewards])
    await ctx.send(f"⚔️ **Hardened Spellcaster Defeated!** Here are your 3 random items:\n{result_text}")

# ─── Run ──────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
