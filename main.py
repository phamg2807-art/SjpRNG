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
REDIS_URL = os.getenv('REDIS_URL')  # optional, for cooldowns

if not DB_URL or not TOKEN:
    print("ERROR: Missing DATABASE_URL or DISCORD_TOKEN.")
    exit(1)

# Redis client (optional)
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

# Channel IDs (as provided)
CHANNELS = {
    'info': 1516791844678271156,
    'world': 1516791545599103127,
    'merchant': 1516791889750397018,
    'fisher_shore': 1516793333790408845
}

ROLES = {
    'player': 1515614836653031475,        # role given on join
    'fisher_shore': None,                 # you need to add role IDs for each location
    # Add other location roles here
}

# Locations with their role IDs (you must add them)
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

# -------------------- Fish Data (unchanged) --------------------
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
        # Add missing columns if any (safe)
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
    # If we have an ID, try to fetch and edit; if fails, resend
    if info_message_id:
        try:
            msg = await channel.fetch_message(info_message_id)
        except discord.NotFound:
            info_message_id = None  # reset, will send new below

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
                        "`-stats` – your progress\n"
                        "`-sell <fish>` – sell a fish\n"
                        "`-buy <item>` – buy from merchant\n"
                        "`-move <location>` – change area\n"
                        "`-join` – join the game"
                    ),
                    inline=False)

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🎣 Join Game", style=discord.ButtonStyle.success, custom_id="join_game"))
    # No fish/inventory buttons – use commands

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

    # Dropdown for locations
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

# -------------------- Interaction Handler (for dropdown & join) --------------------
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
    elif custom_id.startswith("inv_page_"):
        # pagination for inventory command (we'll keep that as button interaction)
        # This will be used when inventory is displayed via command
        page = int(custom_id.replace("inv_page_", ""))
        # We need to store the filter state somehow; we'll pass via message
        # For simplicity, we'll re-implement inventory with buttons in the command.
        # Let's handle it by calling the inventory command with page.
        # We'll use a different approach: we'll store filter in the custom_id? easier: use message components.
        # We'll handle it in the inventory command below.
        pass  # will be overridden

# -------------------- Commands (prefix -) --------------------
@bot.command(name='join')
async def cmd_join(ctx):
    """Join the game (same as button)."""
    await join_game(ctx)  # we'll adapt join_game to work with both interaction and context

@bot.command(name='fish')
async def cmd_fish(ctx):
    """Go fishing."""
    await fish_action(ctx)

@bot.command(name='inventory')
async def cmd_inventory(ctx, *args):
    """View your inventory."""
    # Optional filter argument
    filter = args[0] if args and args[0] in ['common', 'uncommon', 'epic', 'legendary', 'mythical', 'godlike', 'secret', 'mutated', 'normal'] else None
    await show_inventory(ctx, filter=filter, page=0)

@bot.command(name='stats')
async def cmd_stats(ctx):
    """View your fishing stats."""
    await show_stats(ctx)

@bot.command(name='move')
async def cmd_move(ctx, location: str = None):
    """Move to a location (use -move <location name>)."""
    if not location:
        await ctx.send("Please specify a location. Available: " + ", ".join(LOCATIONS.keys()))
        return
    # Find matching location (case-insensitive partial match)
    found = None
    for key, loc in LOCATIONS.items():
        if location.lower() in key.lower() or location.lower() in loc['name'].lower():
            found = key
            break
    if not found:
        await ctx.send(f"Location '{location}' not found.")
        return
    await move_and_assign_role(ctx, found)

# -------------------- Helper functions for commands --------------------
async def join_game(ctx):
    user = ctx.author
    guild = ctx.guild
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
        embed = discord.Embed(title="✅ Joined!", description="You are now a fisher!", color=discord.Color.green())
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Error: {e}")
        conn.rollback()
    finally:
        release(conn)

async def move_and_assign_role(ctx_or_inter, location_key):
    """Move user to location and assign role."""
    if isinstance(ctx_or_inter, discord.Interaction):
        user = ctx_or_inter.user
        guild = ctx_or_inter.guild
        response = ctx_or_inter.response
    else:  # commands.Context
        user = ctx_or_inter.author
        guild = ctx_or_inter.guild
        response = ctx_or_inter.send

    loc = LOCATIONS.get(location_key)
    if not loc:
        await response("Invalid location.")
        return

    # Update DB
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE players SET current_location = %s WHERE user_id = %s", (location_key, user.id))
        conn.commit()
    except Exception as e:
        await response(f"Database error: {e}")
        conn.rollback()
        return
    finally:
        release(conn)

    # Assign role
    role_id = loc.get('role_id')
    if role_id:
        role = guild.get_role(role_id)
        if role:
            # Remove previous location roles (optional)
            for other_loc in LOCATIONS.values():
                other_role = guild.get_role(other_loc.get('role_id', 0))
                if other_role and other_role != role:
                    try:
                        await user.remove_roles(other_role)
                    except:
                        pass
            await user.add_roles(role)

    msg = f"📍 Moved to **{loc['name']}**!"
    if isinstance(ctx_or_inter, discord.Interaction):
        await response.send_message(msg, ephemeral=True)
    else:
        await response(msg)

async def fish_action(ctx):
    user = ctx.author
    conn = db()
    msg = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE user_id = %s", (user.id,))
        player = cur.fetchone()
        if not player:
            await ctx.send("You need to join first! Use `-join`.")
            return

        # Cooldown via Redis or DB
        if player['last_fish_time']:
            cd = (time.time() - player['last_fish_time'].timestamp())
            if cd < 30:
                await ctx.send(f"⏳ Wait {int(30-cd)}s.")
                return

        # Animation
        await ctx.send("🎣 Casting line...")
        msg = await ctx.channel.fetch_message(ctx.message.id + 1)  # not reliable; better use send and edit.
        # Better: send a new message and edit it.
        msg = await ctx.send("🌊 Waiting...")
        await asyncio.sleep(1.5)
        await msg.edit(content="🐟 Something's pulling!")
        await asyncio.sleep(1)
        await msg.edit(content="🎣 Reeling in...")
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
                await ctx.send(f"Error: {e}")
        except:
            pass
        conn.rollback()
    finally:
        release(conn)

async def show_stats(ctx):
    user = ctx.author
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE user_id = %s", (user.id,))
        player = cur.fetchone()
        if not player:
            await ctx.send("Join first!")
            return
        embed = discord.Embed(title=f"{user.name}'s Stats", color=discord.Color.blue())
        embed.add_field(name="Level", value=player['level'], inline=True)
        embed.add_field(name="Experience", value=player['experience'], inline=True)
        embed.add_field(name="Fish Caught", value=player['fish_caught'], inline=True)
        embed.add_field(name="Coins", value=player['coins'], inline=True)
        embed.add_field(name="Location", value=LOCATIONS.get(player['current_location'], {}).get('name', 'Unknown'), inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        release(conn)

# Inventory command with pagination and filter (buttons)
INVENTORY_PAGE_SIZE = 10
async def show_inventory(ctx, filter=None, page=0):
    user = ctx.author
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
        # Filter dropdown
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
        if page > 0:
            view.add_item(discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, custom_id=f"inv_page_{page-1}"))
        if page < total_pages - 1:
            view.add_item(discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, custom_id=f"inv_page_{page+1}"))
        # We'll use a custom interaction handler for these buttons
        # We'll store filter and page in a temporary state (use message id?)
        # For simplicity, we'll handle the button clicks via a separate event.
        # We'll attach a callback to the view.
        await ctx.send(embed=embed, view=view)
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        release(conn)

# -------------------- Additional Interaction Handler for Inventory Pagination & Filter --------------------
# We need to handle the inventory filter and page buttons. We'll store state in the custom_id.
# We'll use a global dict to store user's current filter and page? Better: store in the button custom_id.
# But we need to keep filter across page changes. We can store filter in the message's view state.
# However, Discord components don't persist state except via custom_id.
# Simpler: when user clicks a button, we fetch the current filter from the database? Not efficient.
# We can store filter as part of the custom_id: e.g., inv_page_<filter>_<page>.
# But our select menu already triggers a custom_id "inv_filter_select", which we can handle.
# We'll use a different approach: on filter select, we'll send a new message with updated filter and page=0.
# On page buttons, we'll also send a new message with the same filter.
# We'll pass the filter as a parameter in the custom_id.
# But for simplicity, we'll use a global variable or store in the user's state (not recommended).
# Let's implement a simple approach: when filter changes, we edit the original message? Not possible with ephemeral.
# Since this is a command response (non-ephemeral), we can edit the message.
# But the view is attached to the message; we can't update it easily.
# I'll simplify: use only buttons for pagination and a dropdown for filter; when filter changes, we send a new message.
# To keep it clean, we'll just let the user use the command again with a filter argument, e.g., -inventory epic.
# That way we don't need complex state.
# For now, I'll implement the dropdown and page buttons as separate interactions that call the command with the appropriate filter.
# But we need to capture the filter. We can store the filter in the message's embed? not possible.
# I'll use a simple solution: when the dropdown is used, we respond with a new message (or edit the original if we can).
# I'll make the inventory command send a new message each time with the filter.
# For page buttons, we can store the filter in the custom_id: inv_page_<filter>_<page>.
# Then we can parse it.

# Let's implement that: we'll use custom_id format: inv_page_<filter>_<page>
# And for filter select, we'll call the command again.
# We'll need to modify the on_interaction to handle these.

# I'll rewrite the inventory command to be called with a specific filter and page.
# And the interaction handler will parse and call it.

# Let's do it:

# We'll store the user's current filter in a dict per user, but it's simpler to parse from custom_id.

# In the command, we'll build the view with buttons that include the filter in the custom_id.
# For dropdown, we'll handle it separately.

# I'll implement that now.

# -------------------- Inventory Command (revised) --------------------
async def show_inventory(ctx, filter=None, page=0):
    user = ctx.author
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
        # Filter dropdown
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
        # Page buttons with filter encoded
        filter_str = filter if filter else "all"
        if page > 0:
            view.add_item(discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, custom_id=f"inv_page_{filter_str}_{page-1}"))
        if page < total_pages - 1:
            view.add_item(discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, custom_id=f"inv_page_{filter_str}_{page+1}"))

        # Send the message
        if isinstance(ctx, discord.Interaction):
            await ctx.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await ctx.send(embed=embed, view=view)
    except Exception as e:
        if isinstance(ctx, discord.Interaction):
            await ctx.response.send_message(f"Error: {e}", ephemeral=True)
        else:
            await ctx.send(f"Error: {e}")
    finally:
        release(conn)

# -------------------- Interaction Handler (extended) --------------------
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
        # Dropdown filter selected
        filter = interaction.data['values'][0]
        # Call inventory with this filter
        await show_inventory(interaction, filter=filter, page=0)
    elif custom_id.startswith("inv_page_"):
        # Parse filter and page
        parts = custom_id.split("_")
        if len(parts) == 4:
            filter = parts[2]
            page = int(parts[3])
            await show_inventory(interaction, filter=filter, page=page)
        else:
            await interaction.response.send_message("Invalid pagination.", ephemeral=True)

# -------------------- Run Bot --------------------
if __name__ == "__main__":
    try:
        print("Starting bot...")
        bot.run(TOKEN)
    except Exception as e:
        print(f"Fatal: {e}")
        import traceback
        traceback.print_exc()
