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
REDIS_URL = os.getenv('REDIS_URL')  # optional

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

# Channel IDs
CHANNELS = {
    'info': 1516791844678271156,
    'world': 1516791545599103127,
    'merchant': 1516791889750397018,
    'fisher_shore': 1516793333790408845
}

ROLES = {
    'player': 1515614836653031475,  # role given on join
}

# Locations with role IDs (fill these)
LOCATIONS = {
    '1-fisher-shore': {
        'name': '🏖️ Fisher Shore',
        'description': 'Starter area – Safe fishing',
        'max_depth': 20,
        'price_multiplier': 1.0,
        'weight_range': (0.2, 25),
        'role_id': 0,   # <-- replace with actual role ID for this area
        'native_fish': ['Bristlemouths', 'Peruvian Anchoveta', 'Capelin',
                        'Alaska Pollock', 'Nile Tilapia', 'Atlantic Herring']
    }
    # Add more locations with their role IDs
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

# -------------------- Database Init with Migrations --------------------
def init_database():
    conn = db()
    try:
        cur = conn.cursor()
        # Players table
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

        cur.execute("""
            CREATE TABLE IF NOT EXISTS rods (
                user_id BIGINT PRIMARY KEY REFERENCES players(user_id),
                rod_name TEXT DEFAULT 'Old Rod',
                max_depth INTEGER DEFAULT 30,
                max_weight INTEGER DEFAULT 20,
                equipped BOOLEAN DEFAULT TRUE
            )
        """)
        for col, definition in [
            ("rod_name", "TEXT DEFAULT 'Old Rod'"),
            ("max_depth", "INTEGER DEFAULT 30"),
            ("max_weight", "INTEGER DEFAULT 20"),
            ("equipped", "BOOLEAN DEFAULT TRUE")
        ]:
            try:
                cur.execute(f"ALTER TABLE rods ADD COLUMN IF NOT EXISTS {col} {definition}")
            except Exception:
                pass

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
                        "`-inventory` – view your catches\n"
                        "`-rods` – see your rods\n"
                        "`-sell <fish>` – sell a fish (coming soon)\n"
                        "`-buy <item>` – buy from merchant (coming soon)"
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

# -------------------- Commands (remaining) --------------------
@bot.command(name='fish')
async def cmd_fish(ctx):
    """Go fishing."""
    await fish_action(ctx)

@bot.command(name='inventory')
async def cmd_inventory(ctx, *args):
    """View your inventory. Optional filter: common, uncommon, epic, etc."""
    filter = args[0].lower() if args and args[0].lower() in ['common','uncommon','epic','legendary','mythical','godlike','secret','mutated','normal'] else None
    await show_inventory(ctx, filter=filter, page=0)

@bot.command(name='rods')
async def cmd_rods(ctx):
    """Show your rods."""
    await show_rods(ctx)

@bot.command(name='sell')
async def cmd_sell(ctx, *, fish_name: str = None):
    """Sell a fish (coming soon)."""
    await ctx.send("Sell command is not yet implemented.")

@bot.command(name='buy')
async def cmd_buy(ctx, *, item: str = None):
    """Buy from merchant (coming soon)."""
    await ctx.send("Buy command is not yet implemented.")

# -------------------- Core Functions --------------------
async def join_game(interaction):
    """Add player and give Old Rod via button."""
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
            INSERT INTO rods (user_id, rod_name, max_depth, max_weight, equipped)
            VALUES (%s, 'Old Rod', 30, 20, TRUE)
            ON CONFLICT (user_id) DO UPDATE
            SET rod_name = 'Old Rod', max_depth = 30, max_weight = 20, equipped = TRUE
        """, (user.id,))
        conn.commit()
        embed = discord.Embed(title="✅ Joined!", description="You are now a fisher! You have an **Old Rod** (max depth 30m, max weight 20kg).", color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        conn.rollback()
    finally:
        release(conn)

async def move_and_assign_role(interaction, location_key):
    """Move user via dropdown."""
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

    # Assign role
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
            await ctx.send("You need to join first! Use the **Join Game** button in the info channel.")
            return

        cur.execute("SELECT * FROM rods WHERE user_id = %s", (user.id,))
        rod = cur.fetchone()
        if not rod:
            await ctx.send("You don't have a rod! Please rejoin with the Join Game button.")
            return

        # Cooldown (30s)
        if player['last_fish_time']:
            cd = (time.time() - player['last_fish_time'].timestamp())
            if cd < 30:
                await ctx.send(f"⏳ Wait {int(30-cd)}s.")
                return

        # --- Animation (single message edited) ---
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

        # Filter by rod limits
        pool = [f for f in full_pool if f['depth_max'] <= rod['max_depth'] and f['weight_max'] <= rod['max_weight']]
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
        conn.commit()

        embed = discord.Embed(title="🎣 Caught!", color=discord.Color.gold())
        embed.add_field(name="Fish", value=f"**{chosen['name']}**", inline=True)
        embed.add_field(name="Weight", value=f"{weight:.2f}kg", inline=True)
        embed.add_field(name="Rarity", value=f"⭐ {chosen['rarity']}", inline=True)
        if mutation:
            embed.add_field(name="Mutation", value=f"✨ {mutation} (x{MUTATIONS[mutation][0]})", inline=True)
        embed.add_field(name="Value", value=f"💰 {price}", inline=True)
        embed.add_field(name="Depth", value=f"{depth:.1f}m", inline=True)
        embed.set_footer(text=f"Rod: {rod['rod_name']} | Location: {loc_data['name']}")
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
        cur.execute("SELECT rod_name, max_depth, max_weight, equipped FROM rods WHERE user_id = %s", (user.id,))
        rods = cur.fetchall()
        if not rods:
            await ctx.send("You have no rods. Please use the Join Game button.")
            return
        embed = discord.Embed(title=f"{user.name}'s Rods", color=discord.Color.green())
        for r in rods:
            status = "✅ Equipped" if r['equipped'] else "❌ Not equipped"
            embed.add_field(
                name=r['rod_name'],
                value=f"Max Depth: {r['max_depth']}m\nMax Weight: {r['max_weight']}kg\n{status}",
                inline=False
            )
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
        print(f"Fatal: {e}")
        import traceback
        traceback.print_exc()
