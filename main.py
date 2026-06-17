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

# -------------------- Flask Web Server (for Render) --------------------
app = Flask('SjpFish')

@app.route('/')
def home():
    return "SjpFish is alive!"

Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# -------------------- Environment & Database --------------------
DB_URL = os.getenv('DATABASE_URL') or exit("ERROR: DATABASE_URL missing!")
TOKEN = os.getenv('DISCORD_TOKEN') or exit("ERROR: DISCORD_TOKEN missing!")

_pool = None

def get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(
            4, 20,
            dsn=DB_URL,
            cursor_factory=RealDictCursor,
            sslmode='require',
            connect_timeout=10
        )
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

bot = commands.Bot(command_prefix='!', intents=intents)  # prefix kept but no commands used

# Channel IDs
CHANNELS = {
    'info': 1516791844678271156,
    'world': 1516791545599103127,
    'merchant': 1516791889750397018,
    'fisher_shore': 1516793333790408845
}

ROLES = {
    'player': 1515614836653031475
}

# Only one location now (as requested)
LOCATIONS = {
    '1-fisher-shore': '🏖️ Starter area – Safe fishing'
}

# Store message IDs for editing
info_message_id = None
world_message_id = None

# -------------------- Database Initialization --------------------
def init_database():
    conn = db()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS players (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                current_location TEXT DEFAULT '1-fisher-shore',
                fish_caught INTEGER DEFAULT 0,
                coins INTEGER DEFAULT 0,
                experience INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                last_fish_time TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fish_inventory (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES players(user_id),
                fish_name TEXT,
                quantity INTEGER DEFAULT 1,
                rarity TEXT,
                value INTEGER,
                caught_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
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
        print("Database initialized.")
    except Exception as e:
        print(f"DB init error: {e}")
        conn.rollback()
    finally:
        release(conn)

# -------------------- Background Tasks --------------------
@tasks.loop(seconds=30)
async def update_info_channel():
    """Refresh the info embed with live statistics."""
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
        # Total fish caught
        cur.execute("SELECT COUNT(*) as total FROM fish_inventory")
        total_fish = cur.fetchone()['total'] or 0

        # Total players
        cur.execute("SELECT COUNT(*) as total FROM players")
        total_players = cur.fetchone()['total'] or 0

        # Active players (fished in last hour)
        cur.execute("""
            SELECT COUNT(DISTINCT user_id) as active
            FROM fish_inventory
            WHERE caught_at > NOW() - INTERVAL '1 hour'
        """)
        active_players = cur.fetchone()['active'] or 0

        # Top fisher (by fish count)
        cur.execute("""
            SELECT username, fish_caught
            FROM players
            ORDER BY fish_caught DESC
            LIMIT 1
        """)
        top = cur.fetchone()
        top_fisher = f"{top['username']} ({top['fish_caught']} fish)" if top else "None"

    except Exception as e:
        print(f"Stats query error: {e}")
        return
    finally:
        release(conn)

    embed = discord.Embed(
        title="🎣 SJpFISH – Fishing Adventure",
        description="Welcome to the ultimate fishing adventure!",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="📊 Live Statistics",
        value=(
            f"🐟 **Total Fish Caught:** {total_fish}\n"
            f"👥 **Total Players:** {total_players}\n"
            f"🟢 **Active (last hour):** {active_players}\n"
            f"🏆 **Top Fisher:** {top_fisher}"
        ),
        inline=False
    )
    embed.add_field(
        name="📘 How to Play",
        value=(
            "1. Click the **'Join Game'** button below\n"
            "2. Use the **'Fish'** button to catch fish\n"
            "3. Check your **'Inventory'** with the button\n"
            "4. Visit the merchant channel to buy/sell"
        ),
        inline=False
    )
    embed.add_field(
        name="📍 Your Location",
        value="🏖️ **1-fisher-shore** – your current spot",
        inline=False
    )

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🎣 Join Game", style=discord.ButtonStyle.success, custom_id="join_game"))
    view.add_item(discord.ui.Button(label="📖 Guide", style=discord.ButtonStyle.secondary, custom_id="guide"))
    view.add_item(discord.ui.Button(label="🎣 Fish!", style=discord.ButtonStyle.primary, custom_id="fish"))
    view.add_item(discord.ui.Button(label="🎒 Inventory", style=discord.ButtonStyle.secondary, custom_id="inventory"))

    await msg.edit(embed=embed, view=view)

@tasks.loop(seconds=60)
async def update_world_channel():
    """Update world embed – now only shows Fisher Shore and user location."""
    if world_message_id is None:
        return
    channel = bot.get_channel(CHANNELS['world'])
    if not channel:
        return
    try:
        msg = await channel.fetch_message(world_message_id)
    except:
        return

    # We'll show that the only location is Fisher Shore
    embed = discord.Embed(
        title="🗺️ SJPFISH WORLD MAP",
        description="Select your location (only one available for now)",
        color=discord.Color.green()
    )
    embed.add_field(
        name="🏖️ 1-fisher-shore",
        value="Starter area – Safe fishing",
        inline=False
    )
    embed.set_footer(text="Your current location is shown below")

    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="📍 Go to Fisher Shore",
        style=discord.ButtonStyle.primary,
        custom_id="move_1-fisher-shore"
    ))

    await msg.edit(embed=embed, view=view)

# -------------------- Bot Events --------------------
@bot.event
async def on_ready():
    print(f'{bot.user} has connected!')
    init_database()
    await setup_info_channel()
    await setup_world_channel()
    update_info_channel.start()
    update_world_channel.start()

async def setup_info_channel():
    """Send or retrieve the info message."""
    global info_message_id
    channel = bot.get_channel(CHANNELS['info'])
    if not channel:
        return

    # Look for existing message
    async for msg in channel.history(limit=50):
        if msg.author == bot.user and "SJPFISH" in msg.content.upper():
            info_message_id = msg.id
            await update_info_channel()  # immediately update it
            return

    # If none, send a new one (will be edited by the task)
    embed = discord.Embed(title="Loading stats...", color=discord.Color.blue())
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Join Game", style=discord.ButtonStyle.success, custom_id="join_game"))
    view.add_item(discord.ui.Button(label="Guide", style=discord.ButtonStyle.secondary, custom_id="guide"))
    view.add_item(discord.ui.Button(label="Fish", style=discord.ButtonStyle.primary, custom_id="fish"))
    view.add_item(discord.ui.Button(label="Inventory", style=discord.ButtonStyle.secondary, custom_id="inventory"))
    msg = await channel.send(embed=embed, view=view)
    info_message_id = msg.id

async def setup_world_channel():
    """Send or retrieve the world message."""
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

# -------------------- Interaction Handlers --------------------
@bot.event
async def on_interaction(interaction):
    if interaction.type != discord.InteractionType.component:
        return

    custom_id = interaction.data['custom_id']
    user = interaction.user

    if custom_id == "join_game":
        await join_game(interaction)
    elif custom_id == "guide":
        await send_guide(interaction)
    elif custom_id == "inventory":
        await show_inventory(interaction)
    elif custom_id == "fish":
        await fish_action(interaction)
    elif custom_id.startswith("move_"):
        location = custom_id.replace("move_", "")
        await move_location(interaction, location)

async def join_game(interaction):
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
        embed = discord.Embed(
            title="✅ Welcome!",
            description=f"You are now a fisher! Use the **Fish** button to start.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
    finally:
        release(conn)

async def send_guide(interaction):
    embed = discord.Embed(
        title="📖 Quick Guide",
        description=(
            "• Click **Fish** to catch a fish.\n"
            "• Click **Inventory** to see your catches.\n"
            "• Sell fish in the merchant channel (coming soon).\n"
            "• Your location is always Fisher Shore for now."
        ),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def show_inventory(interaction):
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT fish_name, rarity, value, COUNT(*) as qty
            FROM fish_inventory
            WHERE user_id = %s
            GROUP BY fish_name, rarity, value
            ORDER BY rarity DESC, value DESC
        """, (user.id,))
        items = cur.fetchall()

        if not items:
            embed = discord.Embed(
                title="🎒 Inventory",
                description="You have no fish yet. Go fishing!",
                color=discord.Color.purple()
            )
        else:
            embed = discord.Embed(title=f"🎒 {user.name}'s Inventory", color=discord.Color.purple())
            total = 0
            for item in items:
                val = item['value'] * item['qty']
                total += val
                embed.add_field(
                    name=f"{item['fish_name']} x{item['qty']}",
                    value=f"⭐ {item['rarity']} | 💰 {val} coins",
                    inline=False
                )
            embed.add_field(name="**Total Value**", value=f"💰 {total} coins", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
    finally:
        release(conn)

async def fish_action(interaction):
    # Only allow in the shore channel? We'll allow anywhere for simplicity.
    user = interaction.user
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE user_id = %s", (user.id,))
        player = cur.fetchone()
        if not player:
            await interaction.response.send_message("You need to join the game first! Use the Join Game button.", ephemeral=True)
            return

        # Cooldown (30 seconds)
        if player['last_fish_time']:
            cooldown = (time.time() - player['last_fish_time'].timestamp())
            if cooldown < 30:
                await interaction.response.send_message(
                    f"⏳ Please wait {int(30 - cooldown)} seconds.", ephemeral=True
                )
                return

        # Fishing logic
        fish_pool = [
            {"name": "Common Carp", "rarity": "Common", "value": 10},
            {"name": "Trout", "rarity": "Common", "value": 15},
            {"name": "Bass", "rarity": "Uncommon", "value": 25},
            {"name": "Salmon", "rarity": "Uncommon", "value": 30},
            {"name": "Golden Fish", "rarity": "Rare", "value": 100},
            {"name": "Legendary Koi", "rarity": "Legendary", "value": 500}
        ]
        catch = random.choices(fish_pool, weights=[30,25,20,15,8,2])[0]

        # Update player
        cur.execute("""
            UPDATE players
            SET fish_caught = fish_caught + 1,
                coins = coins + %s,
                experience = experience + %s,
                last_fish_time = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (catch['value']//2, catch['value']//10, user.id))

        # Add to inventory
        cur.execute("""
            INSERT INTO fish_inventory (user_id, fish_name, rarity, value)
            VALUES (%s, %s, %s, %s)
        """, (user.id, catch['name'], catch['rarity'], catch['value']))
        conn.commit()

        # Get updated stats
        cur.execute("SELECT * FROM players WHERE user_id = %s", (user.id,))
        updated = cur.fetchone()

        embed = discord.Embed(
            title="🎣 You caught a fish!",
            color=discord.Color.gold()
        )
        embed.add_field(name="Fish", value=f"**{catch['name']}**", inline=True)
        embed.add_field(name="Rarity", value=f"🌟 {catch['rarity']}", inline=True)
        embed.add_field(name="Value", value=f"💰 {catch['value']} coins", inline=True)
        embed.add_field(name="Total Fish", value=str(updated['fish_caught']), inline=True)
        embed.add_field(name="Coins", value=str(updated['coins']), inline=True)
        embed.add_field(name="Level", value=str(updated['level']), inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=False)  # visible to all
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        conn.rollback()
    finally:
        release(conn)

async def move_location(interaction, location):
    user = interaction.user
    if location not in LOCATIONS:
        await interaction.response.send_message("Invalid location.", ephemeral=True)
        return

    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE players
            SET current_location = %s
            WHERE user_id = %s
        """, (location, user.id))
        conn.commit()
        await interaction.response.send_message(
            f"📍 You are now at **{location}**!", ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        conn.rollback()
    finally:
        release(conn)

# -------------------- Run Bot --------------------
if __name__ == "__main__":
    bot.run(TOKEN)
