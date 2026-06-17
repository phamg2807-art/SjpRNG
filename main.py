import os
import time
import asyncio
import random
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

CHANNELS = {
    'info': 1516791844678271156,
    'world': 1516791545599103127,
    'merchant': 1516791889750397018,
    'fisher_shore': 1516793333790408845
}

ROLES = {
    'player': 1515614836653031475,
}

LOCATIONS = {
    '1-fisher-shore': {
        'name': '🏖️ Fisher Shore',
        'description': 'Starter area – Safe fishing',
        'max_depth': 20,
        'price_multiplier': 1.0,
        'weight_range': (0.2, 25),
        'role_id': 0,
        'native_fish': ['Bristlemouths', 'Peruvian Anchoveta', 'Capelin',
                        'Alaska Pollock', 'Nile Tilapia', 'Atlantic Herring'],
        'shop_name': 'Old Market',
        'shop_items': [
            {'name': 'Plastic Rod', 'max_depth': 45, 'max_weight': 30, 'price': 400},
            {'name': 'Basic Rod', 'max_depth': 65, 'max_weight': 50, 'price': 800},
            {'name': 'Advanced Rod', 'max_depth': 80, 'max_weight': 80, 'price': 1500},
            {'name': 'Rod of Strength', 'max_depth': 120, 'max_weight': 100, 'price': 10400, 'ability': 'strength'}
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
GLOBAL_NAMES = {f['name'] for f in GLOBAL_FISH}
NATIVE_NAMES = {f['name'] for f in NATIVE_FISH}

# -------------------- Helper Functions --------------------
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

def get_origin(fish_name):
    if fish_name in GLOBAL_NAMES:
        return "Global"
    elif fish_name in NATIVE_NAMES:
        return "Native"
    return "Unknown"

# -------------------- Database Migrations --------------------
def init_database():
    conn = db()
    try:
        cur = conn.cursor()
        # players
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

        # caught_fish
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

        # user_rods
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
        # migrate old rods if exists
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

        # locked_fish (user-level lock by fish name)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS locked_fish (
                user_id BIGINT REFERENCES players(user_id),
                fish_name TEXT,
                PRIMARY KEY (user_id, fish_name)
            )
        """)

        # merchant_stock
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

# -------------------- Background Tasks (unchanged) --------------------
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
    embed.add_field(name="📘 Commands (prefix `-`)",
                    value=(
                        "`-fish` – go fishing\n"
                        "`-inv` / `-inventory` – view your catches\n"
                        "`-rods` – see your rods\n"
                        "`-shop` – visit the local market\n"
                        "`-sell <fish>` – sell one fish\n"
                        "`-sell all` – sell all unlocked fish\n"
                        "`-sell <fish> all` – sell all unlocked fish of that name\n"
                        "`-lock <fish>` – lock all fish of that name (prevents selling)\n"
                        "`-unlock <fish>` – unlock them\n"
                        "`-collection` / `-col` – view your fish collection"
                    ),
                    inline=False)

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🎣 Join Game", style=discord.ButtonStyle.success, custom_id="join_game"))

    if info_message_id:
        try:
            msg = await channel.fetch_message(info_message_id)
            await msg.edit(embed=embed, view=view)
        except:
            info_message_id = None
    if not info_message_id:
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

    if world_message_id:
        try:
            msg = await channel.fetch_message(world_message_id)
            await msg.edit(embed=embed, view=view)
        except:
            world_message_id = None
    if not world_message_id:
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
    elif custom_id.startswith("buy_rod_"):
        rod_name = custom_id.replace("buy_rod_", "")
        await buy_rod(interaction, rod_name)

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

@bot.command(name='shop')
async def cmd_shop(ctx):
    await show_shop(ctx)

@bot.command(name='sell')
async def cmd_sell(ctx, *, args: str = None):
    if not args:
        await ctx.send("Usage: `-sell <fish_name>` or `-sell all` or `-sell <fish_name> all`")
        return
    parts = args.split()
    if len(parts) == 1:
        fish_name = parts[0]
        await sell_fish(ctx, fish_name, all_of_type=False)
    elif len(parts) == 2:
        if parts[1].lower() == 'all':
            fish_name = parts[0]
            if fish_name.lower() == 'all':
                await sell_all_fish(ctx)
            else:
                await sell_fish(ctx, fish_name, all_of_type=True)
        else:
            await ctx.send("Invalid syntax. Use `-sell <fish_name>` or `-sell all` or `-sell <fish_name> all`")
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
            INSERT INTO players (user_id, username, current_location)
            VALUES (%s, %s, '1-fisher-shore')
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

async def fish_action(ctx):
    user = ctx.author
    conn = db()
    msg = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE user_id = %s", (user.id,))
        player = cur.fetchone()
        if not player:
            await ctx.send("Join first using the button in the info channel.")
            return

        cur.execute("""
            SELECT * FROM user_rods 
            WHERE user_id = %s AND equipped = TRUE
        """, (user.id,))
        rod = cur.fetchone()
        if not rod:
            await ctx.send("You have no rod equipped! Use `-rods` to equip one.")
            return

        # Cooldown 5 seconds
        if player['last_fish_time']:
            cd = (time.time() - player['last_fish_time'].timestamp())
            if cd < 5:
                await ctx.send(f"⏳ Wait {int(5-cd)}s.")
                return

        # Animation
        msg = await ctx.send("🎣 Casting line...")
        await asyncio.sleep(1.5)
        await msg.edit(content="🌊 Waiting for a bite...")
        await asyncio.sleep(2)
        await msg.edit(content="🐟 Something's pulling!")
        await asyncio.sleep(1)
        await msg.edit(content="🎣 Reeling it in...")
        await asyncio.sleep(1.5)

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
        embed.set_footer(text=f"Rod: {rod_name}{bonus_text} | Location: {loc_data['name']}")
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
        # Add equip dropdown
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

@bot.event
async def on_interaction(interaction):
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = interaction.data.get('custom_id')
    if custom_id == "equip_rod":
        rod_id = int(interaction.data['values'][0])
        await equip_rod(interaction, rod_id)
    # ... other handlers already defined, we'll combine with the previous one
    # To avoid duplication, we'll combine all handlers in one event.
    # We'll define a single on_interaction that covers all.

# We'll redefine on_interaction to include all cases (combine with earlier)
# For cleanliness, we'll redefine on_interaction after all functions, overriding.

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

async def show_shop(ctx):
    user = ctx.author
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_location FROM players WHERE user_id = %s", (user.id,))
        player = cur.fetchone()
        if not player:
            await ctx.send("You need to join first!")
            return
        location_key = player['current_location']
        loc = LOCATIONS.get(location_key)
        if not loc or 'shop_items' not in loc:
            await ctx.send("No shop available in this location.")
            return

        embed = discord.Embed(title=f"🏪 {loc['shop_name']}", description=f"Welcome to the {loc['shop_name']}!", color=discord.Color.blue())
        for item in loc['shop_items']:
            ability_text = f"\nAbility: {item.get('ability', 'None')}" if item.get('ability') else ""
            embed.add_field(
                name=f"🎣 {item['name']}",
                value=f"Max Depth: {item['max_depth']}m | Max Weight: {item['max_weight']}kg\nPrice: 💰 {item['price']}{ability_text}",
                inline=False
            )

        view = discord.ui.View()
        for item in loc['shop_items']:
            view.add_item(discord.ui.Button(label=f"Buy {item['name']}", style=discord.ButtonStyle.primary, custom_id=f"buy_rod_{item['name']}"))

        await ctx.send(embed=embed, view=view)
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        release(conn)

async def buy_rod(interaction, rod_name):
    user = interaction.user
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_location, coins FROM players WHERE user_id = %s", (user.id,))
        player = cur.fetchone()
        if not player:
            await interaction.response.send_message("You need to join first!", ephemeral=True)
            return

        location_key = player['current_location']
        loc = LOCATIONS.get(location_key)
        if not loc or 'shop_items' not in loc:
            await interaction.response.send_message("No shop here.", ephemeral=True)
            return

        item = None
        for it in loc['shop_items']:
            if it['name'] == rod_name:
                item = it
                break
        if not item:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return

        if player['coins'] < item['price']:
            await interaction.response.send_message(f"You don't have enough coins! You need {item['price']} coins.", ephemeral=True)
            return

        cur.execute("UPDATE players SET coins = coins - %s WHERE user_id = %s", (item['price'], user.id))
        cur.execute("""
            INSERT INTO user_rods (user_id, rod_name, max_depth, max_weight, equipped)
            VALUES (%s, %s, %s, %s, FALSE)
        """, (user.id, item['name'], item['max_depth'], item['max_weight']))
        conn.commit()

        embed = discord.Embed(title="✅ Purchase successful!", description=f"You bought **{item['name']}** for {item['price']} coins.", color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        conn.rollback()
    finally:
        release(conn)

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
        # Check if this fish name is locked
        cur.execute("SELECT 1 FROM locked_fish WHERE user_id = %s AND fish_name = %s", (user.id, fish_name))
        locked = cur.fetchone()
        if locked:
            await ctx.send(f"❌ **{fish_name}** is locked. Unlock it first with `-unlock {fish_name}`.")
            return

        if all_of_type:
            # Sell all of this fish
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
            # Sell one
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
        # Get all fish, excluding locked ones
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

# -------------------- Inventory System (updated with origin) --------------------
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

# -------------------- Final Interaction Handler (combined) --------------------
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
    elif custom_id.startswith("buy_rod_"):
        rod_name = custom_id.replace("buy_rod_", "")
        await buy_rod(interaction, rod_name)
    elif custom_id == "equip_rod":
        rod_id = int(interaction.data['values'][0])
        await equip_rod(interaction, rod_id)
    else:
        # unknown
        pass

# -------------------- Run Bot --------------------
if __name__ == "__main__":
    try:
        print("Starting bot...")
        bot.run(TOKEN)
    except Exception as e:
        print(f"Fatal: {e}")
        import traceback
        traceback.print_exc()
