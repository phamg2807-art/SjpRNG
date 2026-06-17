import os
import time
import asyncio
import random
import math
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands, tasks
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
import redis  # optional, only if you want to use Redis

# -------------------- Flask Web Server (for Render) --------------------
app = Flask('SjpFish')

@app.route('/')
def home():
    return "SjpFish is alive!"

port = int(os.getenv('PORT', 8080))
Thread(target=lambda: app.run(host='0.0.0.0', port=port), daemon=True).start()

# -------------------- Environment & Database --------------------
DB_URL = os.getenv('DATABASE_URL')
TOKEN = os.getenv('DISCORD_TOKEN')
REDIS_URL = os.getenv('REDIS_URL')  # optional

if not DB_URL or not TOKEN:
    print("ERROR: Missing DATABASE_URL or DISCORD_TOKEN.")
    exit(1)

# Optional Redis client (not used yet, but ready)
redis_client = None
if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        print("Redis connected.")
    except Exception as e:
        print(f"Redis connection failed: {e}")

_pool = None

def get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(4, 20, dsn=DB_URL,
                                       cursor_factory=RealDictCursor,
                                       sslmode='require', connect_timeout=10)
    return _pool

def db():
    return get_pool().getconn()

def release(conn):
    try:
        get_pool().putconn(conn)
    except Exception:
        pass

# -------------------- Discord Bot Setup --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Channel IDs (exactly as you specified)
CHANNELS = {
    'info': 1516791844678271156,
    'world': 1516791545599103127,
    'merchant': 1516791889750397018,
    'fisher_shore': 1516793333790408845
}

ROLES = {'player': 1515614836653031475}  # ensure this ID is correct

# -------------------- Location & Fish Data (unchanged) --------------------
LOCATIONS = {
    '1-fisher-shore': {
        'name': '🏖️ Fisher Shore',
        'description': 'Starter area – Safe fishing',
        'max_depth': 20,
        'price_multiplier': 1.0,
        'weight_range': (0.2, 25),
        'native_fish': ['Bristlemouths', 'Peruvian Anchoveta', 'Capelin',
                        'Alaska Pollock', 'Nile Tilapia', 'Atlantic Herring']
    }
}

RARITIES = {
    'Common':    (6,     50,     0.45),
    'Uncommon':  (32,    92,     0.35),
    'Epic':      (100,   1232,   0.15),
    'Legendary': (1220,  12123,  0.04),
    'Mythical':  (12321, 1233123,0.009),
    'Godlike':   (213213, 511454267, 0.0009),
    'Secret':    (5145422, 4135432543234, 0.0001)
}

MUTATIONS = {
    'Shiny':     (1.5,  1/5),
    'Golden':    (2.0,  1/10),
    'Diamond':   (5.0,  1/20),
    'Chromatic': (10.0, 1/50)
}

GLOBAL_FISH = [
    {'name': 'Common Carp',      'weight_min': 2,   'weight_max': 12,  'depth_min': 2,   'depth_max': 6,   'rarity': 'Common'},
    {'name': 'Silver Carp',      'weight_min': 3,   'weight_max': 18,  'depth_min': 4,   'depth_max': 8,   'rarity': 'Common'},
    {'name': 'Lanternfish',      'weight_min': 0.3, 'weight_max': 3,   'depth_min': 300, 'depth_max': 1500,'rarity': 'Common'},
    {'name': 'Pacific Sardine',  'weight_min': 1,   'weight_max': 4,   'depth_min': 40,  'depth_max': 500, 'rarity': 'Uncommon'},
]

NATIVE_FISH = [
    {'name': 'Bristlemouths',      'weight_min': 1.2, 'weight_max': 6,   'depth_min': 12,  'depth_max': 20,   'rarity': 'Common'},
    {'name': 'Peruvian Anchoveta', 'weight_min': 0.8, 'weight_max': 2,   'depth_min': 12,  'depth_max': 280,  'rarity': 'Common'},
    {'name': 'Capelin',            'weight_min': 0.5, 'weight_max': 2,   'depth_min': 15,  'depth_max': 1200, 'rarity': 'Common'},
    {'name': 'Alaska Pollock',     'weight_min': 1.2, 'weight_max': 8,   'depth_min': 18,  'depth_max': 230,  'rarity': 'Uncommon'},
    {'name': 'Nile Tilapia',       'weight_min': 1.4, 'weight_max': 9,   'depth_min': 19,  'depth_max': 1220, 'rarity': 'Uncommon'},
    {'name': 'Atlantic Herring',   'weight_min': 3.5, 'weight_max': 11,  'depth_min': 6,   'depth_max': 1500, 'rarity': 'Epic'},
]

ALL_FISH = GLOBAL_FISH + NATIVE_FISH
FISH_DEF = {f['name']: f for f in ALL_FISH}

# -------------------- Helper Functions (unchanged) --------------------
def calculate_price(fish_def, weight, mutation=None):
    rarity = fish_def['rarity']
    base_min, base_max, _ = RARITIES[rarity]
    w_min = fish_def['weight_min']
    w_max = fish_def['weight_max']
    ratio = 0.5 if w_max == w_min else (weight - w_min) / (w_max - w_min)
    ratio = max(0, min(1, ratio))
    price = base_min + (base_max - base_min) * ratio
    if mutation:
        price *= MUTATIONS[mutation][0]
    return int(round(price))

def get_fish_for_location(location_key):
    pool = list(GLOBAL_FISH)
    loc = LOCATIONS.get(location_key)
    if loc:
        for f in NATIVE_FISH:
            if f['name'] in loc.get('native_fish', []):
                pool.append(f)
    return pool

def roll_mutation():
    r = random.random()
    cum = 0.0
    for name, (mult, chance) in MUTATIONS.items():
        cum += chance
        if r < cum:
            return name
    return None

# -------------------- Database Initialisation with Migrations --------------------
def init_database():
    conn = db()
    try:
        cur = conn.cursor()

        # Create players table if not exists (with correct columns)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS players (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                current_location TEXT DEFAULT '1-fisher-shore',
                fish_caught INTEGER DEFAULT 0,
                coins BIGINT DEFAULT 0,
                experience INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                last_fish_time TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Check and add missing columns (in case table existed with old schema)
        # We'll use a simple approach: try to add each column, ignore if already exists.
        columns_to_add = [
            ("username", "TEXT"),
            ("current_location", "TEXT DEFAULT '1-fisher-shore'"),
            ("fish_caught", "INTEGER DEFAULT 0"),
            ("coins", "BIGINT DEFAULT 0"),
            ("experience", "INTEGER DEFAULT 0"),
            ("level", "INTEGER DEFAULT 1"),
            ("last_fish_time", "TIMESTAMP DEFAULT NULL"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        ]
        for col, definition in columns_to_add:
            try:
                cur.execute(f"ALTER TABLE players ADD COLUMN IF NOT EXISTS {col} {definition}")
            except Exception as e:
                print(f"Note: Could not add column {col} (may already exist): {e}")

        # Create caught_fish table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS caught_fish (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES players(user_id),
                fish_name TEXT,
                weight REAL,
                rarity TEXT,
                mutation TEXT,
                base_price BIGINT,
                final_price BIGINT,
                location TEXT,
                caught_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create merchant_stock (optional)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS merchant_stock (
                id SERIAL PRIMARY KEY,
                item_name TEXT UNIQUE,
                item_type TEXT,
                price INTEGER,
                quantity INTEGER DEFAULT 10,
                rarity TEXT,
                is_black_market BOOLEAN DEFAULT FALSE,
                last_restock TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        print("Database initialised and schema up-to-date.")
    except Exception as e:
        print(f"DB init error: {e}")
        conn.rollback()
    finally:
        release(conn)

# -------------------- Background Tasks (unchanged) --------------------
info_message_id = None
world_message_id = None

@tasks.loop(seconds=30)
async def update_info_channel():
    if info_message_id is None:
        return
    channel = bot.get_channel(CHANNELS['info'])
    if not channel:
        return
    try:
        msg = await channel.fetch_message(info_message_id)
    except:
        return

    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM caught_fish")
        total_fish = cur.fetchone()['total'] or 0

        cur.execute("SELECT COUNT(*) as total FROM players")
        total_players = cur.fetchone()['total'] or 0

        cur.execute("""
            SELECT COUNT(DISTINCT user_id) as active
            FROM caught_fish
            WHERE caught_at > NOW() - INTERVAL '1 hour'
        """)
        active_players = cur.fetchone()['active'] or 0

        cur.execute("""
            SELECT p.username, COUNT(cf.id) as fish_count
            FROM players p
            LEFT JOIN caught_fish cf ON p.user_id = cf.user_id
            GROUP BY p.user_id, p.username
            ORDER BY fish_count DESC
            LIMIT 1
        """)
        top = cur.fetchone()
        top_fisher = f"{top['username']} ({top['fish_count']} fish)" if top else "None"
    except Exception as e:
        print(f"Stats error: {e}")
        return
    finally:
        release(conn)

    embed = discord.Embed(title="🎣 SJpFISH – Fishing Adventure", color=discord.Color.blue())
    embed.add_field(name="📊 Live Statistics",
                    value=f"🐟 Total Fish: {total_fish}\n👥 Players: {total_players}\n🟢 Active: {active_players}\n🏆 Top: {top_fisher}",
                    inline=False)
    loc = LOCATIONS['1-fisher-shore']
    embed.add_field(name="📍 Fisher Shore Stats",
                    value=f"Max Depth: {loc['max_depth']}m\nMultiplier: {loc['price_multiplier']}x\nWeight: {loc['weight_range'][0]}–{loc['weight_range'][1]}kg",
                    inline=False)
    embed.add_field(name="📘 How to Play",
                    value="1. Join Game\n2. Fish!\n3. Inventory\n4. Merchant (soon)",
                    inline=False)

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🎣 Join Game", style=discord.ButtonStyle.success, custom_id="join_game"))
    view.add_item(discord.ui.Button(label="📖 Guide", style=discord.ButtonStyle.secondary, custom_id="guide"))
    view.add_item(discord.ui.Button(label="🎣 Fish!", style=discord.ButtonStyle.primary, custom_id="fish"))
    view.add_item(discord.ui.Button(label="🎒 Inventory", style=discord.ButtonStyle.secondary, custom_id="inventory"))
    await msg.edit(embed=embed, view=view)

@tasks.loop(seconds=60)
async def update_world_channel():
    if world_message_id is None:
        return
    channel = bot.get_channel(CHANNELS['world'])
    if not channel:
        return
    try:
        msg = await channel.fetch_message(world_message_id)
    except:
        return

    embed = discord.Embed(title="🗺️ SJPFISH WORLD MAP", color=discord.Color.green())
    loc = LOCATIONS['1-fisher-shore']
    embed.add_field(name=f"{loc['name']}",
                    value=f"{loc['description']}\nMax Depth: {loc['max_depth']}m\nMultiplier: {loc['price_multiplier']}x",
                    inline=False)
    embed.set_footer(text="Click below to move here.")

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="📍 Go to Fisher Shore", style=discord.ButtonStyle.primary, custom_id="move_1-fisher-shore"))
    await msg.edit(embed=embed, view=view)

# -------------------- Bot Events (unchanged) --------------------
@bot.event
async def on_ready():
    print(f'{bot.user} connected!')
    init_database()
    await setup_info_channel()
    await setup_world_channel()
    update_info_channel.start()
    update_world_channel.start()

async def setup_info_channel():
    global info_message_id
    channel = bot.get_channel(CHANNELS['info'])
    if not channel:
        return
    async for msg in channel.history(limit=50):
        if msg.author == bot.user and "SJPFISH" in msg.content.upper():
            info_message_id = msg.id
            await update_info_channel()
            return
    embed = discord.Embed(title="Loading stats...", color=discord.Color.blue())
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Join Game", style=discord.ButtonStyle.success, custom_id="join_game"))
    view.add_item(discord.ui.Button(label="Guide", style=discord.ButtonStyle.secondary, custom_id="guide"))
    view.add_item(discord.ui.Button(label="Fish", style=discord.ButtonStyle.primary, custom_id="fish"))
    view.add_item(discord.ui.Button(label="Inventory", style=discord.ButtonStyle.secondary, custom_id="inventory"))
    msg = await channel.send(embed=embed, view=view)
    info_message_id = msg.id

async def setup_world_channel():
    global world_message_id
    channel = bot.get_channel(CHANNELS['world'])
    if not channel:
        return
    async for msg in channel.history(limit=50):
        if msg.author == bot.user and "WORLD MAP" in msg.content.upper():
            world_message_id = msg.id
            await update_world_channel()
            return
    embed = discord.Embed(title="🗺️ World Map", color=discord.Color.green())
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Go to Fisher Shore", style=discord.ButtonStyle.primary, custom_id="move_1-fisher-shore"))
    msg = await channel.send(embed=embed, view=view)
    world_message_id = msg.id

# -------------------- Interaction Handlers (unchanged) --------------------
@bot.event
async def on_interaction(interaction):
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = interaction.data.get('custom_id')
    if custom_id == "inv_filter_select":
        selected = interaction.data['values'][0]
        await show_inventory(interaction, filter=selected, page=0)
        return
    if custom_id == "join_game":
        await join_game(interaction)
    elif custom_id == "guide":
        await send_guide(interaction)
    elif custom_id == "inventory":
        await show_inventory(interaction, page=0)
    elif custom_id == "fish":
        await fish_action(interaction)
    elif custom_id.startswith("move_"):
        await move_location(interaction, custom_id.replace("move_", ""))
    elif custom_id.startswith("inv_page_"):
        await show_inventory(interaction, page=int(custom_id.replace("inv_page_", "")))

# -------------------- Core Functions (unchanged except small fixes) --------------------
async def join_game(interaction):
    user = interaction.user
    guild = interaction.guild
    role = guild.get_role(ROLES['player'])
    if role:
        await user.add_roles(role)
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO players (user_id, username, current_location)
            VALUES (%s, %s, '1-fisher-shore')
            ON CONFLICT (user_id) DO UPDATE
            SET username = EXCLUDED.username
        """, (user.id, user.name))
        conn.commit()
        embed = discord.Embed(title="✅ Welcome!", description="You are now a fisher!", color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        conn.rollback()
    finally:
        release(conn)

async def send_guide(interaction):
    embed = discord.Embed(title="📖 Quick Guide", description="• Fish button\n• Inventory\n• Mutations\n• Location", color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def fish_action(interaction):
    user = interaction.user
    conn = db()
    msg = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE user_id = %s", (user.id,))
        player = cur.fetchone()
        if not player:
            await interaction.response.send_message("Join first!", ephemeral=True)
            return

        if player['last_fish_time']:
            cd = (time.time() - player['last_fish_time'].timestamp())
            if cd < 30:
                await interaction.response.send_message(f"⏳ {int(30-cd)}s remaining.", ephemeral=True)
                return

        await interaction.response.send_message("🎣 Casting...")
        msg = await interaction.original_response()
        await asyncio.sleep(1.5)
        await msg.edit(content="🌊 Waiting...")
        await asyncio.sleep(2)
        await msg.edit(content="🐟 Pulling!")
        await asyncio.sleep(1)
        await msg.edit(content="🎣 Reeling...")
        await asyncio.sleep(1.5)

        location_key = player['current_location']
        loc_data = LOCATIONS.get(location_key) or LOCATIONS['1-fisher-shore']
        pool = get_fish_for_location(location_key)
        if not pool:
            await msg.edit(content="❌ No fish here.")
            return

        weights = [RARITIES[f['rarity']][2] for f in pool]
        chosen = random.choices(pool, weights=weights, k=1)[0]
        weight = random.uniform(chosen['weight_min'], chosen['weight_max'])
        depth = random.uniform(chosen['depth_min'], chosen['depth_max'])
        mutation = roll_mutation()
        price = calculate_price(chosen, weight, mutation)

        cur.execute("""
            INSERT INTO caught_fish (user_id, fish_name, weight, rarity, mutation, base_price, final_price, location)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (user.id, chosen['name'], weight, chosen['rarity'], mutation, price, price, location_key))

        cur.execute("""
            UPDATE players
            SET fish_caught = fish_caught + 1,
                coins = coins + %s,
                experience = experience + %s,
                last_fish_time = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (price, price//10, user.id))
        conn.commit()

        embed = discord.Embed(title="🎣 Caught!", color=discord.Color.gold())
        embed.add_field(name="Fish", value=f"**{chosen['name']}**", inline=True)
        embed.add_field(name="Weight", value=f"{weight:.2f}kg", inline=True)
        embed.add_field(name="Rarity", value=f"⭐ {chosen['rarity']}", inline=True)
        if mutation:
            embed.add_field(name="Mutation", value=f"✨ {mutation} (x{MUTATIONS[mutation][0]})", inline=True)
        embed.add_field(name="Value", value=f"💰 {price}", inline=True)
        embed.add_field(name="Depth", value=f"{depth:.1f}m", inline=True)
        embed.set_footer(text=loc_data['name'])
        await msg.edit(content=None, embed=embed)

    except Exception as e:
        try:
            if msg:
                await msg.edit(content=f"❌ Error: {e}")
            else:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        except:
            pass
        conn.rollback()
    finally:
        release(conn)

async def move_location(interaction, location):
    if location not in LOCATIONS:
        await interaction.response.send_message("Invalid location.", ephemeral=True)
        return
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE players SET current_location = %s WHERE user_id = %s", (location, interaction.user.id))
        conn.commit()
        await interaction.response.send_message(f"📍 Moved to {LOCATIONS[location]['name']}!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        conn.rollback()
    finally:
        release(conn)

# -------------------- Inventory System (unchanged) --------------------
INVENTORY_PAGE_SIZE = 10

async def show_inventory(interaction, filter=None, page=0):
    user = interaction.user
    conn = db()
    try:
        cur = conn.cursor()
        query = "SELECT fish_name, weight, rarity, mutation, final_price, location, caught_at FROM caught_fish WHERE user_id = %s"
        params = [user.id]
        if filter and filter != 'all':
            if filter in RARITIES:
                query += " AND rarity = %s"
                params.append(filter)
            elif filter == 'mutated':
                query += " AND mutation IS NOT NULL"
            elif filter == 'normal':
                query += " AND mutation IS NULL"
        query += " ORDER BY caught_at DESC"
        cur.execute(query, params)
        all_fish = cur.fetchall()
        total = len(all_fish)
        total_pages = max(1, (total + INVENTORY_PAGE_SIZE - 1) // INVENTORY_PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        start = page * INVENTORY_PAGE_SIZE
        end = min(start + INVENTORY_PAGE_SIZE, total)
        page_items = all_fish[start:end]

        if not page_items:
            embed = discord.Embed(title="🎒 Inventory", description="Empty!", color=discord.Color.purple())
        else:
            embed = discord.Embed(title=f"🎒 {user.name}'s Inventory", description=f"Total: {total}  |  Page {page+1}/{total_pages}", color=discord.Color.purple())
            total_value = 0
            for item in page_items:
                total_value += item['final_price']
                mutation_text = f" ✨{item['mutation']}" if item['mutation'] else ""
                embed.add_field(
                    name=f"{item['fish_name']}{mutation_text}",
                    value=f"⭐ {item['rarity']}  |  {item['weight']:.2f}kg  |  💰 {item['final_price']}\n📍 {item['location']}  |  {item['caught_at'].strftime('%Y-%m-%d %H:%M')}",
                    inline=False
                )
            embed.add_field(name="**Total Value**", value=f"💰 {total_value}", inline=False)

        view = discord.ui.View()
        select = discord.ui.Select(
            placeholder="Filter",
            options=[
                discord.SelectOption(label="All", value="all", default=(filter=='all' or filter is None)),
                discord.SelectOption(label="Common", value="Common", default=(filter=='Common')),
                discord.SelectOption(label="Uncommon", value="Uncommon", default=(filter=='Uncommon')),
                discord.SelectOption(label="Epic", value="Epic", default=(filter=='Epic')),
                discord.SelectOption(label="Legendary", value="Legendary", default=(filter=='Legendary')),
                discord.SelectOption(label="Mythical", value="Mythical", default=(filter=='Mythical')),
                discord.SelectOption(label="Godlike", value="Godlike", default=(filter=='Godlike')),
                discord.SelectOption(label="Secret", value="Secret", default=(filter=='Secret')),
                discord.SelectOption(label="Mutated", value="mutated", default=(filter=='mutated')),
                discord.SelectOption(label="Normal", value="normal", default=(filter=='normal')),
            ],
            custom_id="inv_filter_select"
        )
        view.add_item(select)
        if page > 0:
            view.add_item(discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, custom_id=f"inv_page_{page-1}"))
        if page < total_pages - 1:
            view.add_item(discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, custom_id=f"inv_page_{page+1}"))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
    finally:
        release(conn)

# -------------------- Run Bot --------------------
if __name__ == "__main__":
    try:
        print("Starting bot...")
        bot.run(TOKEN)
    except Exception as e:
        print(f"Fatal: {e}")
        import traceback
        traceback.print_exc()
