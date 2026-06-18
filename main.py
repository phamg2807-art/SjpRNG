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
import redis

# -------------------- Flask Web Server --------------------
app = Flask('SjpFish')

@app.route('/')
def home():
    return "SjpFish is alive!"

port = int(os.getenv('PORT', 8080))
Thread(target=lambda: app.run(host='0.0.0.0', port=port), daemon=True).start()

# -------------------- Environment --------------------
DB_URL = os.getenv('DATABASE_URL')
TOKEN = os.getenv('DISCORD_TOKEN')
REDIS_URL = os.getenv('REDIS_URL')

if not DB_URL or not TOKEN:
    print("ERROR: Missing DATABASE_URL or DISCORD_TOKEN.")
    exit(1)

redis_client = None
if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        print("Redis connected.")
    except Exception as e:
        print(f"Redis connection failed: {e}")

# -------------------- Database Pool --------------------
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

bot = commands.Bot(command_prefix='-', intents=intents)
bot.remove_command('help')

CHANNELS = {
    'info': 1516791844678271156,
    'world': 1516791545599103127,
    'merchant': 1516791889750397018,
    'fisher_shore': 1516793333790408845,
    'tropical_isle': 1517023363506114751
}

# Map channel ID -> location key for shop detection
CHANNEL_TO_LOCATION = {
    CHANNELS['fisher_shore']: '1-fisher-shore',
    CHANNELS['tropical_isle']: 'tropical-isle',
}

ROLES = {
    'player': 1515614836653031475,
}

# Locations with role IDs
LOCATIONS = {
    '1-fisher-shore': {
        'name': '🏖️ Fisher Shore',
        'description': 'Starter area – Safe fishing',
        'max_depth': 20,
        'price_multiplier': 1.0,
        'weight_range': (0.2, 25),
        'role_id': 1516800086405808158,
        'native_fish': ['Bristlemouths', 'Peruvian Anchoveta', 'Capelin',
                        'Alaska Pollock', 'Nile Tilapia', 'Atlantic Herring'],
        'shop_name': 'Old Market',
        'shop_items': [
            {'name': 'Plastic Rod', 'max_depth': 45, 'max_weight': 30, 'price': 400, 'type': 'rod'},
            {'name': 'Basic Rod', 'max_depth': 65, 'max_weight': 50, 'price': 800, 'type': 'rod'},
            {'name': 'Advanced Rod', 'max_depth': 80, 'max_weight': 80, 'price': 1500, 'type': 'rod'},
            {'name': 'Rod of Strength', 'max_depth': 120, 'max_weight': 100, 'price': 10400, 'type': 'rod', 'ability': 'strength'}
        ]
    },
    'tropical-isle': {
        'name': '🌴 Tropical Isle',
        'description': 'Exotic island with rare fish',
        'max_depth': 120,
        'price_multiplier': 1.2,
        'weight_range': (3, 45),
        'role_id': 1517023829988475060,
        'native_fish': ['Goldfish', 'Angelfish', 'Guppy', 'Platy', 'Rainbowfish',
                        'Ram Cichlid', 'Flowerhorn Cichlid', 'Arowana', 'Emperor Tetra',
                        'Black Neon Tetra', 'Pufferfish', 'Clownfish'],
        'shop_name': 'Masterbait',
        'shop_items': [
            {'name': 'Basic Bait', 'price': 500, 'type': 'bait', 'effects': {'luck_epic': 0.05, 'luck_legendary': 0.01}},
            {'name': 'Advanced Bait', 'price': 1000, 'type': 'bait', 'effects': {'luck_epic': 0.08, 'luck_legendary': 0.04, 'luck_mythical': 0.003}},
            {'name': 'Swift Bait', 'price': 600, 'type': 'bait', 'effects': {'catch_time_reduction': 0.20}},
            {'name': 'Quick Bait', 'price': 1200, 'type': 'bait', 'effects': {'catch_time_reduction': 0.35}},
            {'name': 'Rapid Bait', 'price': 3200, 'type': 'bait', 'effects': {'catch_time_reduction': 0.60}},
            {'name': 'Quick Bait+', 'price': 2500, 'type': 'bait', 'effects': {'catch_time_reduction': 0.45, 'cooldown_reduction': 0.30}},
            {'name': 'Fortune Bait', 'price': 500, 'type': 'bait', 'effects': {'luck_all': 0.50}},
            {'name': 'Weight Bait', 'price': 1300, 'type': 'bait', 'effects': {'weight_multiplier': 1.2}},
            {'name': 'Mythical Hunter', 'price': 6400, 'type': 'bait', 'effects': {'luck_mythical': 0.01}}
        ]
    }
}

# -------------------- Fish Data --------------------
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
    {'name': 'Glowlight Tetra',  'weight_min': 0.8, 'weight_max': 8,   'depth_min': 2000, 'depth_max': 4000, 'rarity': 'Epic'},
]

FISHER_SHORE_FISH = [
    {'name': 'Bristlemouths',      'weight_min': 1.2, 'weight_max': 6,   'depth_min': 12,  'depth_max': 20,   'rarity': 'Common'},
    {'name': 'Peruvian Anchoveta', 'weight_min': 0.8, 'weight_max': 2,   'depth_min': 12,  'depth_max': 280,  'rarity': 'Common'},
    {'name': 'Capelin',            'weight_min': 0.5, 'weight_max': 2,   'depth_min': 15,  'depth_max': 1200, 'rarity': 'Common'},
    {'name': 'Alaska Pollock',     'weight_min': 1.2, 'weight_max': 8,   'depth_min': 18,  'depth_max': 230,  'rarity': 'Uncommon'},
    {'name': 'Nile Tilapia',       'weight_min': 1.4, 'weight_max': 9,   'depth_min': 19,  'depth_max': 1220, 'rarity': 'Uncommon'},
    {'name': 'Atlantic Herring',   'weight_min': 3.5, 'weight_max': 11,  'depth_min': 6,   'depth_max': 1500, 'rarity': 'Epic'},
]

TROPICAL_FISH = [
    {'name': 'Goldfish',          'weight_min': 2,   'weight_max': 7,   'depth_min': 2,   'depth_max': 6,   'rarity': 'Common'},
    {'name': 'Angelfish',         'weight_min': 0.3, 'weight_max': 4,   'depth_min': 2,   'depth_max': 12,  'rarity': 'Common'},
    {'name': 'Guppy',             'weight_min': 0.1, 'weight_max': 2,   'depth_min': 12,  'depth_max': 65,  'rarity': 'Common'},
    {'name': 'Platy',             'weight_min': 0.2, 'weight_max': 3,   'depth_min': 12,  'depth_max': 65,  'rarity': 'Common'},
    {'name': 'Rainbowfish',       'weight_min': 1,   'weight_max': 5,   'depth_min': 1,   'depth_max': 65,  'rarity': 'Uncommon'},
    {'name': 'Ram Cichlid',       'weight_min': 1.2, 'weight_max': 7,   'depth_min': 50,  'depth_max': 120, 'rarity': 'Uncommon'},
    {'name': 'Flowerhorn Cichlid','weight_min': 2.8, 'weight_max': 12,  'depth_min': 40,  'depth_max': 180, 'rarity': 'Uncommon'},
    {'name': 'Arowana',           'weight_min': 6,   'weight_max': 23,  'depth_min': 30,  'depth_max': 900, 'rarity': 'Uncommon'},
    {'name': 'Emperor Tetra',     'weight_min': 0.3, 'weight_max': 5.2, 'depth_min': 60,  'depth_max': 1200,'rarity': 'Epic'},
    {'name': 'Black Neon Tetra',  'weight_min': 0.6, 'weight_max': 6,   'depth_min': 30,  'depth_max': 800, 'rarity': 'Epic'},
    {'name': 'Pufferfish',        'weight_min': 2,   'weight_max': 20,  'depth_min': 3,   'depth_max': 80,  'rarity': 'Epic'},
    {'name': 'Clownfish',         'weight_min': 0.5, 'weight_max': 0.5, 'depth_min': 0,   'depth_max': 80,  'rarity': 'Legendary'},
]

ALL_FISH = GLOBAL_FISH + FISHER_SHORE_FISH + TROPICAL_FISH
FISH_DEF = {f['name']: f for f in ALL_FISH}
GLOBAL_NAMES = {f['name'] for f in GLOBAL_FISH}
NATIVE_NAMES = {f['name'] for f in FISHER_SHORE_FISH + TROPICAL_FISH}

# -------------------- Helper Functions --------------------
def calculate_price(fish_def, weight, mutation=None, location_multiplier=1.0):
    rarity = fish_def['rarity']
    base_min, base_max, _ = RARITIES[rarity]
    w_min = fish_def['weight_min']
    w_max = fish_def['weight_max']
    ratio = 0.5 if w_max == w_min else (weight - w_min) / (w_max - w_min)
    ratio = max(0, min(1, ratio))
    price = base_min + (base_max - base_min) * ratio
    price *= location_multiplier
    if mutation:
        price *= MUTATIONS[mutation][0]
    return int(round(price))

def get_fish_for_location(location_key):
    pool = list(GLOBAL_FISH)
    if location_key == '1-fisher-shore':
        pool.extend(FISHER_SHORE_FISH)
    elif location_key == 'tropical-isle':
        pool.extend(TROPICAL_FISH)
    return pool

def roll_mutation():
    r = random.random()
    cum = 0.0
    for name, (mult, chance) in MUTATIONS.items():
        cum += chance
        if r < cum:
            return name
    return None

def get_origin(fish_name):
    if fish_name in GLOBAL_NAMES:
        return "Global"
    elif fish_name in {f['name'] for f in FISHER_SHORE_FISH}:
        return "Native (Fisher Shore)"
    elif fish_name in {f['name'] for f in TROPICAL_FISH}:
        return "Native (Tropical Isle)"
    return "Unknown"

def calculate_level(exp):
    level = 1
    needed = 10
    while exp >= needed:
        exp -= needed
        level += 1
        needed = level * 10
    return level, exp, needed

# -------------------- Database Migrations --------------------
def init_database():
    conn = db()
    try:
        cur = conn.cursor()
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
        for col, definition in [
            ("username", "TEXT"),
            ("current_location", "TEXT DEFAULT '1-fisher-shore'"),
            ("fish_caught", "INTEGER DEFAULT 0"),
            ("coins", "BIGINT DEFAULT 0"),
            ("experience", "INTEGER DEFAULT 0"),
            ("level", "INTEGER DEFAULT 1"),
            ("last_fish_time", "TIMESTAMP DEFAULT NULL"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        ]:
            try:
                cur.execute(f"ALTER TABLE players ADD COLUMN IF NOT EXISTS {col} {definition}")
            except Exception:
                pass

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
        for col, definition in [
            ("fish_name", "TEXT"),
            ("weight", "REAL"),
            ("rarity", "TEXT"),
            ("mutation", "TEXT"),
            ("base_price", "BIGINT"),
            ("final_price", "BIGINT"),
            ("location", "TEXT"),
            ("caught_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        ]:
            try:
                cur.execute(f"ALTER TABLE caught_fish ADD COLUMN IF NOT EXISTS {col} {definition}")
            except Exception:
                pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_rods (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES players(user_id),
                rod_name TEXT,
                max_depth INTEGER,
                max_weight INTEGER,
                bonus_weight INTEGER DEFAULT 0,
                equipped BOOLEAN DEFAULT FALSE,
                acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'rods'
            )
        """)
        old_rods_exist = cur.fetchone()['exists']
        if old_rods_exist:
            cur.execute("""
                INSERT INTO user_rods (user_id, rod_name, max_depth, max_weight, equipped)
                SELECT user_id, rod_name, max_depth, max_weight, equipped
                FROM rods
                ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("DROP TABLE rods")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS locked_fish (
                user_id BIGINT REFERENCES players(user_id),
                fish_name TEXT,
                PRIMARY KEY (user_id, fish_name)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_baits (
                user_id BIGINT REFERENCES players(user_id),
                bait_name TEXT,
                quantity INTEGER DEFAULT 1,
                equipped BOOLEAN DEFAULT FALSE,
                PRIMARY KEY (user_id, bait_name)
            )
        """)

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
        print("Database ready.")
    except Exception as e:
        print(f"DB init error: {e}")
        conn.rollback()
    finally:
        release(conn)

# -------------------- Background Tasks --------------------
info_message_id = None
world_message_id = None

@tasks.loop(seconds=30)
async def update_info_channel():
    global info_message_id
    channel = bot.get_channel(CHANNELS['info'])
    if not channel:
        return

    if info_message_id:
        try:
            msg = await channel.fetch_message(info_message_id)
        except discord.NotFound:
            info_message_id = None
            msg = None
        except Exception:
            msg = None
    else:
        msg = None

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
    fs = LOCATIONS['1-fisher-shore']
    ti = LOCATIONS['tropical-isle']
    embed.add_field(name="📍 Fisher Shore Stats",
                    value=f"Max Depth: {fs['max_depth']}m\nMultiplier: {fs['price_multiplier']}x\nWeight: {fs['weight_range'][0]}–{fs['weight_range'][1]}kg",
                    inline=True)
    embed.add_field(name="📍 Tropical Isle Stats",
                    value=f"Max Depth: {ti['max_depth']}m\nMultiplier: {ti['price_multiplier']}x\nWeight: {ti['weight_range'][0]}–{ti['weight_range'][1]}kg",
                    inline=True)
    embed.add_field(name="📘 Commands (prefix `-`)",
                    value=(
                        "`-fish` – go fishing\n"
                        "`-inv` / `-inventory` – view your catches\n"
                        "`-rods` – see your rods\n"
                        "`-baits` – see your baits\n"
                        "`-equipbait <name>` – equip a bait\n"
                        "`-unequipbait` – remove equipped bait\n"
                        "`-shop` – visit the local market\n"
                        "`-balance` / `-bal` – check your coins & level\n"
                        "`-sell <fish>` – sell one fish\n"
                        "`-sell all` – sell all unlocked fish\n"
                        "`-sell <fish> all` – sell all of that fish\n"
                        "`-lock <fish>` – lock a fish (prevents `-sell all`)\n"
                        "`-unlock <fish>` – unlock a fish\n"
                        "`-collection` / `-col` – view your fish collection\n"
                        "`-help` – show this help"
                    ),
                    inline=False)

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🎣 Join Game", style=discord.ButtonStyle.success, custom_id="join_game"))

    if msg:
        try:
            await msg.edit(embed=embed, view=view)
        except:
            msg = await channel.send(embed=embed, view=view)
            info_message_id = msg.id
    else:
        msg = await channel.send(embed=embed, view=view)
        info_message_id = msg.id

@tasks.loop(seconds=60)
async def update_world_channel():
    global world_message_id
    channel = bot.get_channel(CHANNELS['world'])
    if not channel:
        return

    if world_message_id:
        try:
            msg = await channel.fetch_message(world_message_id)
        except discord.NotFound:
            world_message_id = None
            msg = None
        except Exception:
            msg = None
    else:
        msg = None

    embed = discord.Embed(title="🗺️ SJPFISH WORLD MAP", color=discord.Color.green())
    for key, loc in LOCATIONS.items():
        embed.add_field(
            name=loc['name'],
            value=f"{loc['description']}\nMax Depth: {loc['max_depth']}m\nMultiplier: {loc['price_multiplier']}x",
            inline=False
        )
    embed.set_footer(text="Select a location below to move there and get the role.")

    select = discord.ui.Select(
        placeholder="Choose your destination...",
        options=[
            discord.SelectOption(label=loc['name'], value=key, description=loc['description'])
            for key, loc in LOCATIONS.items()
        ],
        custom_id="world_select"
    )
    view = discord.ui.View()
    view.add_item(select)

    if msg:
        try:
            await msg.edit(embed=embed, view=view)
        except:
            msg = await channel.send(embed=embed, view=view)
            world_message_id = msg.id
    else:
        msg = await channel.send(embed=embed, view=view)
        world_message_id = msg.id

# -------------------- Bot Events --------------------
@bot.event
async def on_ready():
    print(f'{bot.user} connected!')
    init_database()
    await update_info_channel()
    await update_world_channel()
    update_info_channel.start()
    update_world_channel.start()

# -------------------- Interaction Handler --------------------
@bot.event
async def on_interaction(interaction):
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = interaction.data.get('custom_id')
    if custom_id == "join_game":
        await join_game(interaction)
    elif custom_id == "world_select":
        location_key = interaction.data['values'][0]
        await move_and_assign_role(interaction, location_key)
    elif custom_id == "inv_filter_select":
        filter = interaction.data['values'][0]
        await show_inventory(interaction, filter=filter, page=0)
    elif custom_id.startswith("inv_page_"):
        parts = custom_id.split("_")
        if len(parts) == 4:
            filter = parts[2]
            page = int(parts[3])
            await show_inventory(interaction, filter=filter, page=page)
        else:
            await interaction.response.send_message("Invalid pagination.", ephemeral=True)
    elif custom_id.startswith("buy_"):
        # Format: buy_type_name_locationkey
        parts = custom_id.split("_", 3)
        if len(parts) == 4:
            item_type = parts[1]
            item_name = parts[2]
            location_key = parts[3]
            await buy_item(interaction, item_type, item_name, location_key)
        else:
            await interaction.response.send_message("Invalid purchase.", ephemeral=True)
    elif custom_id == "equip_rod":
        rod_id = int(interaction.data['values'][0])
        await equip_rod(interaction, rod_id)
    else:
        pass

# -------------------- Commands --------------------
@bot.command(name='fish')
async def cmd_fish(ctx):
    await fish_action(ctx)

@bot.command(name='inventory', aliases=['inv'])
async def cmd_inventory(ctx, *args):
    filter = args[0].lower() if args and args[0].lower() in ['common','uncommon','epic','legendary','mythical','godlike','secret','mutated','normal'] else None
    await show_inventory(ctx, filter=filter, page=0)

@bot.command(name='rods', aliases=['rod'])
async def cmd_rods(ctx):
    await show_rods(ctx)

@bot.command(name='baits', aliases=['bait'])
async def cmd_baits(ctx):
    await show_baits(ctx)

@bot.command(name='equipbait')
async def cmd_equipbait(ctx, *, bait_name: str = None):
    if not bait_name:
        await ctx.send("Usage: `-equipbait <bait_name>`")
        return
    await equip_bait(ctx, bait_name)

@bot.command(name='unequipbait')
async def cmd_unequipbait(ctx):
    await unequip_bait(ctx)

@bot.command(name='shop')
async def cmd_shop(ctx):
    # Determine location based on channel ID
    location_key = CHANNEL_TO_LOCATION.get(ctx.channel.id)
    if not location_key:
        # Fallback to player's current_location
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT current_location FROM players WHERE user_id = %s", (ctx.author.id,))
            row = cur.fetchone()
            if row:
                location_key = row['current_location']
            else:
                await ctx.send("You need to join first!")
                return
        finally:
            release(conn)
    await show_shop(ctx, location_key)

@bot.command(name='sell')
async def cmd_sell(ctx, *, args: str = None):
    if not args:
        await ctx.send("Usage: `-sell <fish_name>` or `-sell all` or `-sell <fish_name> all`")
        return
    parts = args.split()
    if parts[0].lower() == 'all':
        await sell_all_fish(ctx)
        return
    if len(parts) == 1:
        fish_name = parts[0]
        await sell_fish(ctx, fish_name, all_of_type=False)
    elif len(parts) == 2 and parts[1].lower() == 'all':
        fish_name = parts[0]
        await sell_fish(ctx, fish_name, all_of_type=True)
    else:
        await ctx.send("Invalid syntax. Use `-sell <fish_name>` or `-sell all` or `-sell <fish_name> all`")

@bot.command(name='lock')
async def cmd_lock(ctx, *, fish_name: str = None):
    if not fish_name:
        await ctx.send("Usage: `-lock <fish_name>`")
        return
    await toggle_lock(ctx, fish_name, lock=True)

@bot.command(name='unlock')
async def cmd_unlock(ctx, *, fish_name: str = None):
    if not fish_name:
        await ctx.send("Usage: `-unlock <fish_name>`")
        return
    await toggle_lock(ctx, fish_name, lock=False)

@bot.command(name='collection', aliases=['col'])
async def cmd_collection(ctx):
    await show_collection(ctx)

@bot.command(name='balance', aliases=['bal'])
async def cmd_balance(ctx):
    await show_balance(ctx)

@bot.command(name='help')
async def cmd_help(ctx):
    embed = discord.Embed(
        title="🎣 SJpFISH Help",
        description="All commands use the prefix `-`",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="🎣 Fishing & Inventory",
        value=(
            "`-fish` – go fishing (5s cooldown)\n"
            "`-inv` / `-inventory` – view your fish (with filters)\n"
            "`-collection` / `-col` – see your collection\n"
            "`-balance` / `-bal` – check your coins & level"
        ),
        inline=False
    )
    embed.add_field(
        name="💰 Shop & Equipment",
        value=(
            "`-shop` – visit the local market\n"
            "`-rods` – view and equip your rods\n"
            "`-baits` – view your baits\n"
            "`-equipbait <name>` – equip a bait\n"
            "`-unequipbait` – remove equipped bait"
        ),
        inline=False
    )
    embed.add_field(
        name="💵 Selling & Locking",
        value=(
            "`-sell <fish>` – sell one fish\n"
            "`-sell all` – sell all unlocked fish\n"
            "`-sell <fish> all` – sell all of a fish\n"
            "`-lock <fish>` – prevent selling with `-sell all`\n"
            "`-unlock <fish>` – unlock a fish"
        ),
        inline=False
    )
    embed.add_field(
        name="📍 Location",
        value="Use the dropdown in `#sjpfish-world` to move.",
        inline=False
    )
    embed.set_footer(text="Enjoy your fishing adventure!")
    await ctx.send(embed=embed)

# -------------------- Core Functions --------------------
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
            INSERT INTO players (user_id, username, current_location, experience, level)
            VALUES (%s, %s, '1-fisher-shore', 0, 1)
            ON CONFLICT (user_id) DO UPDATE
            SET username = EXCLUDED.username
        """, (user.id, user.name))
        cur.execute("""
            INSERT INTO user_rods (user_id, rod_name, max_depth, max_weight, equipped)
            VALUES (%s, 'Old Rod', 30, 20, TRUE)
            ON CONFLICT DO NOTHING
        """, (user.id,))
        conn.commit()
        embed = discord.Embed(title="✅ Joined!", description="You now have an **Old Rod** (max depth 30m, max weight 20kg).", color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        conn.rollback()
    finally:
        release(conn)

async def move_and_assign_role(interaction, location_key):
    user = interaction.user
    guild = interaction.guild
    loc = LOCATIONS.get(location_key)
    if not loc:
        await interaction.response.send_message("Invalid location.", ephemeral=True)
        return

    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE players SET current_location = %s WHERE user_id = %s", (location_key, user.id))
        conn.commit()
    except Exception as e:
        await interaction.response.send_message(f"Database error: {e}", ephemeral=True)
        conn.rollback()
        return
    finally:
        release(conn)

    role_id = loc.get('role_id')
    if role_id:
        role = guild.get_role(role_id)
        if role:
            for other_loc in LOCATIONS.values():
                other_role = guild.get_role(other_loc.get('role_id', 0))
                if other_role and other_role != role:
                    try:
                        await user.remove_roles(other_role)
                    except:
                        pass
            await user.add_roles(role)

    await interaction.response.send_message(f"📍 Moved to **{loc['name']}**!", ephemeral=True)

# -------------------- Fishing Action --------------------
async def fish_action(ctx):
    user = ctx.author
    conn = db()
    msg = None
    try:
        base_cooldown = 5
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE user_id = %s", (user.id,))
        player = cur.fetchone()
        if not player:
            await ctx.send("Join first using the button in the info channel.")
            return

        # Get equipped bait
        cur.execute("""
            SELECT bait_name FROM user_baits
            WHERE user_id = %s AND equipped = TRUE
        """, (user.id,))
        bait_row = cur.fetchone()
        equipped_bait_name = bait_row['bait_name'] if bait_row else None
        bait_effects = {}
        if equipped_bait_name:
            for loc in LOCATIONS.values():
                for item in loc.get('shop_items', []):
                    if item['type'] == 'bait' and item['name'] == equipped_bait_name:
                        bait_effects = item.get('effects', {})
                        break
                if bait_effects:
                    break

        cooldown = base_cooldown
        if 'cooldown_reduction' in bait_effects:
            cooldown = cooldown * (1 - bait_effects['cooldown_reduction'])
        cooldown = max(1, int(cooldown))

        if redis_client:
            cooldown_key = f"fish_cooldown:{user.id}"
            if redis_client.exists(cooldown_key):
                ttl = redis_client.ttl(cooldown_key)
                await ctx.send(f"⏳ Wait {ttl}s.")
                return
            redis_client.setex(cooldown_key, cooldown, "1")
        else:
            if player['last_fish_time']:
                cd = (time.time() - player['last_fish_time'].timestamp())
                if cd < cooldown:
                    await ctx.send(f"⏳ Wait {int(cooldown-cd)}s.")
                    return

        # Get rod
        cur.execute("""
            SELECT * FROM user_rods 
            WHERE user_id = %s AND equipped = TRUE
        """, (user.id,))
        rod = cur.fetchone()
        if not rod:
            await ctx.send("You have no rod equipped! Use `-rods` to equip one.")
            return

        catch_time = 5.0
        if 'catch_time_reduction' in bait_effects:
            catch_time = catch_time * (1 - bait_effects['catch_time_reduction'])
        catch_time = max(1.5, catch_time)

        steps = [
            ("🎣 Casting line...", 1.5),
            ("🌊 Waiting for a bite...", catch_time * 0.35),
            ("🐟 Something's pulling!", catch_time * 0.25),
            ("🎣 Reeling it in...", catch_time * 0.25)
        ]
        msg = await ctx.send("🎣 Casting line...")
        for i, (text, duration) in enumerate(steps):
            await asyncio.sleep(duration)
            if i < len(steps)-1:
                await msg.edit(content=text)
        await msg.edit(content="🎣 Reeling it in...")
        await asyncio.sleep(catch_time * 0.15)

        location_key = player['current_location']
        loc_data = LOCATIONS.get(location_key) or LOCATIONS['1-fisher-shore']
        full_pool = get_fish_for_location(location_key)

        max_depth = rod['max_depth']
        max_weight = rod['max_weight'] + rod['bonus_weight']

        pool = [f for f in full_pool if f['depth_max'] <= max_depth and f['weight_max'] <= max_weight]
        if not pool:
            await msg.edit(content="❌ Your rod can't reach any fish here! Upgrade your rod.")
            return

        weights = [RARITIES[f['rarity']][2] for f in pool]
        if bait_effects:
            luck_epic = bait_effects.get('luck_epic', 0)
            luck_legendary = bait_effects.get('luck_legendary', 0)
            luck_mythical = bait_effects.get('luck_mythical', 0)
            luck_all = bait_effects.get('luck_all', 0)
            for i, f in enumerate(pool):
                rarity = f['rarity']
                boost = 0
                if rarity == 'Epic':
                    boost += luck_epic
                elif rarity == 'Legendary':
                    boost += luck_legendary
                elif rarity == 'Mythical':
                    boost += luck_mythical
                boost += luck_all
                weights[i] += boost
        weights = [max(0.001, w) for w in weights]

        chosen = random.choices(pool, weights=weights, k=1)[0]
        weight = random.uniform(chosen['weight_min'], chosen['weight_max'])
        if 'weight_multiplier' in bait_effects:
            weight *= bait_effects['weight_multiplier']
        depth = random.uniform(chosen['depth_min'], chosen['depth_max'])
        mutation = roll_mutation()
        price = calculate_price(chosen, weight, mutation, loc_data['price_multiplier'])

        cur.execute("""
            INSERT INTO caught_fish (user_id, fish_name, weight, rarity, mutation, base_price, final_price, location)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (user.id, chosen['name'], weight, chosen['rarity'], mutation, price, price, location_key))

        exp_gain = 5
        cur.execute("""
            UPDATE players
            SET fish_caught = fish_caught + 1,
                coins = coins + %s,
                experience = experience + %s,
                last_fish_time = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (price, exp_gain, user.id))

        cur.execute("SELECT experience FROM players WHERE user_id = %s", (user.id,))
        new_exp = cur.fetchone()['experience']
        new_level, remaining, needed = calculate_level(new_exp)
        cur.execute("UPDATE players SET level = %s WHERE user_id = %s", (new_level, user.id))

        if rod['rod_name'] == 'Rod of Strength':
            cur.execute("""
                UPDATE user_rods
                SET bonus_weight = bonus_weight + 1
                WHERE id = %s
            """, (rod['id'],))

        conn.commit()

        origin = get_origin(chosen['name'])
        embed = discord.Embed(title="🎣 Caught!", color=discord.Color.gold())
        embed.add_field(name="Fish", value=f"**{chosen['name']}**", inline=True)
        embed.add_field(name="Weight", value=f"{weight:.2f}kg", inline=True)
        embed.add_field(name="Rarity", value=f"⭐ {chosen['rarity']}", inline=True)
        if mutation:
            embed.add_field(name="Mutation", value=f"✨ {mutation} (x{MUTATIONS[mutation][0]})", inline=True)
        embed.add_field(name="Value", value=f"💰 {price}", inline=True)
        embed.add_field(name="Depth", value=f"{depth:.1f}m", inline=True)
        embed.add_field(name="Origin", value=origin, inline=True)
        rod_name = rod['rod_name']
        bonus_text = f" (+{rod['bonus_weight']} bonus weight)" if rod['bonus_weight'] > 0 else ""
        bait_text = f" | Bait: {equipped_bait_name}" if equipped_bait_name else ""
        embed.set_footer(text=f"Rod: {rod_name}{bonus_text}{bait_text} | Location: {loc_data['name']}")
        await msg.edit(content=None, embed=embed)

    except Exception as e:
        try:
            if msg:
                await msg.edit(content=f"❌ Error: {e}")
            else:
                await ctx.send(f"Error: {e}")
        except:
            pass
        conn.rollback()
    finally:
        release(conn)

# -------------------- Rod Functions --------------------
async def show_rods(ctx):
    user = ctx.author
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, rod_name, max_depth, max_weight, bonus_weight, equipped
            FROM user_rods
            WHERE user_id = %s
            ORDER BY acquired_at
        """, (user.id,))
        rods = cur.fetchall()
        if not rods:
            await ctx.send("You have no rods. Join the game using the button in the info channel.")
            return

        embed = discord.Embed(title=f"{user.name}'s Rods", color=discord.Color.green())
        for r in rods:
            status = "✅ Equipped" if r['equipped'] else "Not equipped"
            bonus_text = f" (+{r['bonus_weight']} bonus)" if r['bonus_weight'] > 0 else ""
            embed.add_field(
                name=f"{r['rod_name']}{bonus_text}",
                value=f"Max Depth: {r['max_depth']}m | Max Weight: {r['max_weight']}kg{bonus_text}\n{status}",
                inline=False
            )
        if len(rods) > 1:
            select = discord.ui.Select(
                placeholder="Equip a rod...",
                options=[
                    discord.SelectOption(label=f"{r['rod_name']} (ID:{r['id']})", value=str(r['id']))
                    for r in rods if not r['equipped']
                ],
                custom_id="equip_rod"
            )
            view = discord.ui.View()
            view.add_item(select)
            await ctx.send(embed=embed, view=view)
        else:
            await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        release(conn)

async def equip_rod(interaction, rod_id):
    user = interaction.user
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM user_rods WHERE id = %s AND user_id = %s", (rod_id, user.id))
        rod = cur.fetchone()
        if not rod:
            await interaction.response.send_message("You don't own that rod.", ephemeral=True)
            return
        cur.execute("UPDATE user_rods SET equipped = FALSE WHERE user_id = %s", (user.id,))
        cur.execute("UPDATE user_rods SET equipped = TRUE WHERE id = %s", (rod_id,))
        conn.commit()
        await interaction.response.send_message("✅ Rod equipped!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        conn.rollback()
    finally:
        release(conn)

# -------------------- Bait Functions --------------------
async def show_baits(ctx):
    user = ctx.author
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT bait_name, quantity, equipped
            FROM user_baits
            WHERE user_id = %s
            ORDER BY bait_name
        """, (user.id,))
        baits = cur.fetchall()
        if not baits:
            await ctx.send("You have no baits. Buy some from the shop!")
            return

        embed = discord.Embed(title=f"{user.name}'s Baits", color=discord.Color.teal())
        for b in baits:
            status = "✅ Equipped" if b['equipped'] else "Not equipped"
            embed.add_field(
                name=f"{b['bait_name']} (x{b['quantity']})",
                value=status,
                inline=False
            )
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        release(conn)

async def equip_bait(ctx, bait_name):
    user = ctx.author
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT quantity FROM user_baits WHERE user_id = %s AND bait_name = %s", (user.id, bait_name))
        row = cur.fetchone()
        if not row:
            await ctx.send(f"You don't own any **{bait_name}**.")
            return
        cur.execute("UPDATE user_baits SET equipped = FALSE WHERE user_id = %s", (user.id,))
        cur.execute("UPDATE user_baits SET equipped = TRUE WHERE user_id = %s AND bait_name = %s", (user.id, bait_name))
        conn.commit()
        await ctx.send(f"✅ Equipped **{bait_name}**!")
    except Exception as e:
        await ctx.send(f"Error: {e}")
        conn.rollback()
    finally:
        release(conn)

async def unequip_bait(ctx):
    user = ctx.author
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE user_baits SET equipped = FALSE WHERE user_id = %s", (user.id,))
        conn.commit()
        await ctx.send("✅ Bait unequipped!")
    except Exception as e:
        await ctx.send(f"Error: {e}")
        conn.rollback()
    finally:
        release(conn)

# -------------------- Shop Functions (channel-based) --------------------
async def show_shop(ctx, location_key):
    loc = LOCATIONS.get(location_key)
    if not loc or 'shop_items' not in loc:
        await ctx.send("No shop available in this location.")
        return

    embed = discord.Embed(title=f"🏪 {loc['shop_name']}", description=f"Welcome to the {loc['shop_name']}!", color=discord.Color.blue())
    for item in loc['shop_items']:
        if item['type'] == 'rod':
            ability_text = f"\nAbility: {item.get('ability', 'None')}" if item.get('ability') else ""
            embed.add_field(
                name=f"🎣 {item['name']}",
                value=f"Max Depth: {item['max_depth']}m | Max Weight: {item['max_weight']}kg\nPrice: 💰 {item['price']}{ability_text}",
                inline=False
            )
        elif item['type'] == 'bait':
            effects = item.get('effects', {})
            effect_lines = []
            for key, val in effects.items():
                if 'luck' in key:
                    effect_lines.append(f"➕ {val*100:.1f}% luck for {key.replace('luck_','').capitalize()}")
                elif 'catch_time_reduction' in key:
                    effect_lines.append(f"⏱️ {val*100:.0f}% faster catch")
                elif 'cooldown_reduction' in key:
                    effect_lines.append(f"⏳ {val*100:.0f}% cooldown reduction")
                elif 'weight_multiplier' in key:
                    effect_lines.append(f"⚖️ {val}x weight multiplier")
            embed.add_field(
                name=f"🪱 {item['name']}",
                value=f"Price: 💰 {item['price']}\n" + "\n".join(effect_lines),
                inline=False
            )

    view = discord.ui.View()
    for item in loc['shop_items']:
        view.add_item(discord.ui.Button(
            label=f"Buy {item['name']}",
            style=discord.ButtonStyle.primary,
            custom_id=f"buy_{item['type']}_{item['name']}_{location_key}"
        ))

    await ctx.send(embed=embed, view=view)

async def buy_item(interaction, item_type, item_name, location_key):
    user = interaction.user
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT coins FROM players WHERE user_id = %s", (user.id,))
        player = cur.fetchone()
        if not player:
            await interaction.response.send_message("You need to join first!", ephemeral=True)
            return

        loc = LOCATIONS.get(location_key)
        if not loc or 'shop_items' not in loc:
            await interaction.response.send_message("This item is no longer available.", ephemeral=True)
            return

        item = None
        for it in loc['shop_items']:
            if it['type'] == item_type and it['name'] == item_name:
                item = it
                break
        if not item:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return

        if player['coins'] < item['price']:
            await interaction.response.send_message(f"You need {item['price']} coins.", ephemeral=True)
            return

        cur.execute("UPDATE players SET coins = coins - %s WHERE user_id = %s", (item['price'], user.id))

        if item_type == 'rod':
            cur.execute("""
                INSERT INTO user_rods (user_id, rod_name, max_depth, max_weight, equipped)
                VALUES (%s, %s, %s, %s, FALSE)
            """, (user.id, item['name'], item['max_depth'], item['max_weight']))
        elif item_type == 'bait':
            cur.execute("""
                INSERT INTO user_baits (user_id, bait_name, quantity, equipped)
                VALUES (%s, %s, 1, FALSE)
                ON CONFLICT (user_id, bait_name)
                DO UPDATE SET quantity = user_baits.quantity + 1
            """, (user.id, item['name']))

        conn.commit()
        embed = discord.Embed(title="✅ Purchase successful!", description=f"You bought **{item['name']}** for {item['price']} coins.", color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        conn.rollback()
    finally:
        release(conn)

# -------------------- Sell / Lock / Collection / Balance --------------------
async def toggle_lock(ctx, fish_name, lock=True):
    user = ctx.author
    conn = db()
    try:
        cur = conn.cursor()
        if lock:
            cur.execute("INSERT INTO locked_fish (user_id, fish_name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (user.id, fish_name))
            await ctx.send(f"🔒 Locked **{fish_name}** – you won't sell it with `-sell all`.")
        else:
            cur.execute("DELETE FROM locked_fish WHERE user_id = %s AND fish_name = %s", (user.id, fish_name))
            await ctx.send(f"🔓 Unlocked **{fish_name}**.")
        conn.commit()
    except Exception as e:
        await ctx.send(f"Error: {e}")
        conn.rollback()
    finally:
        release(conn)

async def sell_fish(ctx, fish_name, all_of_type=False):
    user = ctx.author
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM locked_fish WHERE user_id = %s AND fish_name = %s", (user.id, fish_name))
        locked = cur.fetchone()
        if locked:
            await ctx.send(f"❌ **{fish_name}** is locked. Unlock it first with `-unlock {fish_name}`.")
            return

        if all_of_type:
            cur.execute("""
                SELECT id, final_price FROM caught_fish
                WHERE user_id = %s AND fish_name ILIKE %s
            """, (user.id, f"%{fish_name}%"))
            fish_list = cur.fetchall()
            if not fish_list:
                await ctx.send(f"You don't have any fish named '{fish_name}'.")
                return
            total_coins = sum(f['final_price'] for f in fish_list)
            ids = [f['id'] for f in fish_list]
            cur.execute("DELETE FROM caught_fish WHERE id = ANY(%s)", (ids,))
            cur.execute("UPDATE players SET coins = coins + %s WHERE user_id = %s", (total_coins, user.id))
            conn.commit()
            await ctx.send(f"✅ Sold **{len(fish_list)}** **{fish_name}** for **{total_coins}** coins!")
        else:
            cur.execute("""
                SELECT id, final_price FROM caught_fish
                WHERE user_id = %s AND fish_name ILIKE %s
                LIMIT 1
            """, (user.id, f"%{fish_name}%"))
            fish = cur.fetchone()
            if not fish:
                await ctx.send(f"You don't have any fish named '{fish_name}'.")
                return
            cur.execute("DELETE FROM caught_fish WHERE id = %s", (fish['id'],))
            cur.execute("UPDATE players SET coins = coins + %s WHERE user_id = %s", (fish['final_price'], user.id))
            conn.commit()
            await ctx.send(f"✅ Sold **{fish_name}** for **{fish['final_price']}** coins!")
    except Exception as e:
        await ctx.send(f"Error: {e}")
        conn.rollback()
    finally:
        release(conn)

async def sell_all_fish(ctx):
    user = ctx.author
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT cf.id, cf.final_price
            FROM caught_fish cf
            LEFT JOIN locked_fish lf ON cf.user_id = lf.user_id AND cf.fish_name = lf.fish_name
            WHERE cf.user_id = %s AND lf.fish_name IS NULL
        """, (user.id,))
        fish_list = cur.fetchall()
        if not fish_list:
            await ctx.send("You have no unlocked fish to sell.")
            return
        total_coins = sum(f['final_price'] for f in fish_list)
        ids = [f['id'] for f in fish_list]
        cur.execute("DELETE FROM caught_fish WHERE id = ANY(%s)", (ids,))
        cur.execute("UPDATE players SET coins = coins + %s WHERE user_id = %s", (total_coins, user.id))
        conn.commit()
        await ctx.send(f"✅ Sold **{len(fish_list)}** fish for **{total_coins}** coins! (Locked fish were skipped)")
    except Exception as e:
        await ctx.send(f"Error: {e}")
        conn.rollback()
    finally:
        release(conn)

async def show_collection(ctx):
    user = ctx.author
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT fish_name, rarity, COUNT(*) as count
            FROM caught_fish
            WHERE user_id = %s
            GROUP BY fish_name, rarity
            ORDER BY rarity DESC, fish_name
        """, (user.id,))
        collection = cur.fetchall()
        if not collection:
            await ctx.send("You haven't caught any fish yet!")
            return

        embed = discord.Embed(title=f"{user.name}'s Fish Collection", color=discord.Color.gold())
        total_species = len(collection)
        total_fish = sum(c['count'] for c in collection)
        embed.set_footer(text=f"Total species: {total_species} | Total fish: {total_fish}")

        for item in collection:
            origin = get_origin(item['fish_name'])
            embed.add_field(
                name=f"{item['fish_name']} (x{item['count']})",
                value=f"⭐ {item['rarity']} | Origin: {origin}",
                inline=False
            )
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        release(conn)

async def show_balance(ctx):
    user = ctx.author
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT coins, level, experience FROM players WHERE user_id = %s", (user.id,))
        row = cur.fetchone()
        if not row:
            await ctx.send("You need to join first!")
            return
        level, remaining, needed = calculate_level(row['experience'])
        embed = discord.Embed(title=f"{user.name}'s Stats", color=discord.Color.gold())
        embed.add_field(name="💰 Coins", value=row['coins'], inline=True)
        embed.add_field(name="🎣 Level", value=level, inline=True)
        embed.add_field(name="⭐ XP", value=f"{remaining}/{needed} (total {row['experience']})", inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        release(conn)

# -------------------- Inventory System --------------------
INVENTORY_PAGE_SIZE = 10

async def show_inventory(ctx_or_inter, filter=None, page=0):
    if isinstance(ctx_or_inter, discord.Interaction):
        user = ctx_or_inter.user
        response = ctx_or_inter.response.send_message
        ephemeral = True
    else:
        user = ctx_or_inter.author
        response = ctx_or_inter.send
        ephemeral = False

    conn = db()
    try:
        cur = conn.cursor()
        query = "SELECT fish_name, weight, rarity, mutation, final_price, location, caught_at FROM caught_fish WHERE user_id = %s"
        params = [user.id]
        if filter and filter != 'all':
            if filter in RARITIES:
                query += " AND rarity = %s"
                params.append(filter.capitalize())
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
                origin = get_origin(item['fish_name'])
                embed.add_field(
                    name=f"{item['fish_name']}{mutation_text}",
                    value=f"⭐ {item['rarity']}  |  {item['weight']:.2f}kg  |  💰 {item['final_price']}\n📍 {origin}  |  {item['caught_at'].strftime('%Y-%m-%d %H:%M')}",
                    inline=False
                )
            embed.add_field(name="**Total Value**", value=f"💰 {total_value}", inline=False)

        view = discord.ui.View()
        select = discord.ui.Select(
            placeholder="Filter",
            options=[
                discord.SelectOption(label="All", value="all", default=(filter=='all' or filter is None)),
                discord.SelectOption(label="Common", value="common", default=(filter=='common')),
                discord.SelectOption(label="Uncommon", value="uncommon", default=(filter=='uncommon')),
                discord.SelectOption(label="Epic", value="epic", default=(filter=='epic')),
                discord.SelectOption(label="Legendary", value="legendary", default=(filter=='legendary')),
                discord.SelectOption(label="Mythical", value="mythical", default=(filter=='mythical')),
                discord.SelectOption(label="Godlike", value="godlike", default=(filter=='godlike')),
                discord.SelectOption(label="Secret", value="secret", default=(filter=='secret')),
                discord.SelectOption(label="Mutated", value="mutated", default=(filter=='mutated')),
                discord.SelectOption(label="Normal", value="normal", default=(filter=='normal')),
            ],
            custom_id="inv_filter_select"
        )
        view.add_item(select)
        filter_str = filter if filter else "all"
        if page > 0:
            view.add_item(discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, custom_id=f"inv_page_{filter_str}_{page-1}"))
        if page < total_pages - 1:
            view.add_item(discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, custom_id=f"inv_page_{filter_str}_{page+1}"))

        await response(embed=embed, view=view, ephemeral=ephemeral)
    except Exception as e:
        await response(f"Error: {e}", ephemeral=True)
    finally:
        release(conn)

# -------------------- Run Bot --------------------
if __name__ == "__main__":
    try:
        print("Starting bot...")
        bot.run(TOKEN)
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
