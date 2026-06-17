import os
import time
import asyncio
import json
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

# -------------------- Flask Web Server (for Render) --------------------
app = Flask('SjpFish')

@app.route('/')
def home():
    return "SjpFish is alive!"

# Run Flask in a background thread
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
    """Get a connection from the pool."""
    return get_pool().getconn()

def release(conn):
    """Return a connection to the pool."""
    try:
        get_pool().putconn(conn)
    except Exception:
        pass

# -------------------- Discord Bot Setup --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Channel IDs
CHANNELS = {
    'info': 1516791844678271156,
    'world': 1516791545599103127,
    'merchant': 1516791889750397018,
    'fisher_shore': 1516793333790408845
}

# Role IDs
ROLES = {
    'player': 1515614836653031475,
    'shore': 1516793333790408845,  # Example role ID for shore access
}

# -------------------- Database Initialization --------------------
def init_database():
    conn = db()
    try:
        cursor = conn.cursor()
        
        # Players table
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
        
        # Fish inventory
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
        
        # Merchant stock
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
        print("Database initialized successfully!")
    except Exception as e:
        print(f"Database init error: {e}")
        conn.rollback()
    finally:
        release(conn)

# -------------------- Bot Events --------------------
@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    init_database()
    await setup_info_channel()
    await setup_world_channel()

async def setup_info_channel():
    """Setup the info channel with persistent message"""
    channel = bot.get_channel(CHANNELS['info'])
    if not channel:
        return
    
    # Check for existing info message
    async for message in channel.history(limit=50):
        if message.author == bot.user and "**🎣 SJpFISH - Fishing Adventure**" in message.content:
            return
    
    # Create initial info message
    embed = discord.Embed(
        title="🎣 SJpFISH - Fishing Adventure",
        description="Welcome to the ultimate fishing adventure!",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="📚 **How to Play**",
        value=(
            "1. Click the **'Join Game'** button below to start\n"
            "2. Type `!fish` in the shore channel to fish\n"
            "3. Type `!inventory` to see your catches\n"
            "4. Type `!stats` to check your progress\n"
            "5. Visit the merchant channel to buy/sell items"
        ),
        inline=False
    )
    embed.add_field(
        name="📍 **Locations**",
        value=(
            "• 🏖️ **1-fisher-shore** - Starter location\n"
            "• 🌍 Check `#sjpfish-world` for more areas"
        ),
        inline=False
    )
    embed.add_field(
        name="💰 **Commands**",
        value=(
            "`!fish` - Go fishing\n"
            "`!inventory` - View your inventory\n"
            "`!stats` - View your statistics\n"
            "`!sell [item]` - Sell items\n"
            "`!buy [item]` - Buy from merchant\n"
            "`!move [location]` - Change location"
        ),
        inline=False
    )
    
    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="🎣 Join Game",
        style=discord.ButtonStyle.success,
        custom_id="join_game"
    ))
    view.add_item(discord.ui.Button(
        label="📖 Guide",
        style=discord.ButtonStyle.secondary,
        custom_id="guide"
    ))
    
    await channel.send(embed=embed, view=view)

async def setup_world_channel():
    """Setup world map in the world channel"""
    channel = bot.get_channel(CHANNELS['world'])
    if not channel:
        return
    
    # Check for existing world message
    async for message in channel.history(limit=50):
        if message.author == bot.user and "🗺️ **SJPFISH WORLD MAP**" in message.content:
            return
    
    embed = discord.Embed(
        title="🗺️ **SJPFISH WORLD MAP**",
        description="Select a location to explore!",
        color=discord.Color.green()
    )
    
    locations = {
        "🏖️ 1-fisher-shore": "Starter area - Safe fishing",
        "🌊 Deep Ocean": "Medium difficulty - Better catches",
        "🏝️ Coral Reef": "Advanced - Rare fish spawn",
        "🌋 Volcanic Bay": "Expert - Legendary fish"
    }
    
    for location, desc in locations.items():
        embed.add_field(name=location, value=desc, inline=False)
    
    await channel.send(embed=embed)

# -------------------- Button Interactions --------------------
@bot.event
async def on_interaction(interaction):
    if interaction.type == discord.InteractionType.component:
        if interaction.data['custom_id'] == "join_game":
            await join_game(interaction)
        elif interaction.data['custom_id'] == "guide":
            await send_guide(interaction)

async def join_game(interaction):
    """Handle join game button click"""
    user = interaction.user
    guild = interaction.guild
    
    # Add player role
    role = guild.get_role(ROLES['player'])
    if role:
        await user.add_roles(role)
    
    # Add to database
    conn = db()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO players (user_id, username, current_location)
            VALUES (%s, %s, '1-fisher-shore')
            ON CONFLICT (user_id) DO UPDATE 
            SET username = EXCLUDED.username
        """, (user.id, user.name))
        conn.commit()
        
        embed = discord.Embed(
            title="✅ Welcome to SJpFISH!",
            description=f"**{user.name}** has joined the adventure!\n\nYou can now use `!fish` in the shore channel.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
    finally:
        release(conn)

async def send_guide(interaction):
    """Send guide to user"""
    embed = discord.Embed(
        title="📖 Fishing Guide",
        description="Everything you need to know about SJpFISH!",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="🎣 Fishing",
        value=(
            "Go to `#1-fisher-shore` and type `!fish`\n"
            "Better locations give better fish!"
        ),
        inline=False
    )
    embed.add_field(
        name="💰 Economy",
        value=(
            "• Sell fish to merchant for coins\n"
            "• Buy better equipment\n"
            "• Trade with black merchant for rare items"
        ),
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -------------------- Fishing Commands --------------------
@bot.command(name='fish')
async def fish_command(ctx):
    """Fish in the current location"""
    if ctx.channel.id != CHANNELS['fisher_shore']:
        await ctx.send("Please use this command in the shore channel! 🏖️")
        return
    
    user = ctx.author
    conn = db()
    try:
        cursor = conn.cursor()
        
        # Check if user exists
        cursor.execute("SELECT * FROM players WHERE user_id = %s", (user.id,))
        player = cursor.fetchone()
        
        if not player:
            await ctx.send("You haven't joined yet! Click the 'Join Game' button in #sjpfish-info")
            return
        
        # Check cooldown (30 seconds)
        if player['last_fish_time']:
            cooldown = (time.time() - player['last_fish_time'].timestamp())
            if cooldown < 30:
                await ctx.send(f"⏳ Please wait {int(30 - cooldown)} seconds before fishing again!")
                return
        
        # Fishing logic - simple version
        import random
        fish_types = [
            {"name": "Common Carp", "rarity": "Common", "value": 10},
            {"name": "Trout", "rarity": "Common", "value": 15},
            {"name": "Bass", "rarity": "Uncommon", "value": 25},
            {"name": "Salmon", "rarity": "Uncommon", "value": 30},
            {"name": "Golden Fish", "rarity": "Rare", "value": 100},
            {"name": "Legendary Koi", "rarity": "Legendary", "value": 500}
        ]
        
        # Weighted random based on location
        catch = random.choices(fish_types, weights=[30, 25, 20, 15, 8, 2])[0]
        
        # Update database
        cursor.execute("""
            UPDATE players 
            SET fish_caught = fish_caught + 1,
                coins = coins + %s,
                experience = experience + %s,
                last_fish_time = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (catch['value'] // 2, catch['value'] // 10, user.id))
        
        # Add to inventory
        cursor.execute("""
            INSERT INTO fish_inventory (user_id, fish_name, rarity, value)
            VALUES (%s, %s, %s, %s)
        """, (user.id, catch['name'], catch['rarity'], catch['value']))
        
        conn.commit()
        
        # Get updated stats
        cursor.execute("SELECT * FROM players WHERE user_id = %s", (user.id,))
        updated = cursor.fetchone()
        
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
        embed.set_footer(text=f"{user.name}'s Fishing Stats")
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"Error while fishing: {e}")
        conn.rollback()
    finally:
        release(conn)

@bot.command(name='stats')
async def stats_command(ctx):
    """View your fishing statistics"""
    user = ctx.author
    conn = db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM players WHERE user_id = %s", (user.id,))
        player = cursor.fetchone()
        
        if not player:
            await ctx.send("You haven't joined the game yet!")
            return
        
        embed = discord.Embed(
            title=f"📊 {user.name}'s Fishing Stats",
            color=discord.Color.blue()
        )
        embed.add_field(name="Level", value=str(player['level']), inline=True)
        embed.add_field(name="Experience", value=f"{player['experience']} XP", inline=True)
        embed.add_field(name="Fish Caught", value=str(player['fish_caught']), inline=True)
        embed.add_field(name="Coins", value=f"💰 {player['coins']}", inline=True)
        embed.add_field(name="Location", value=f"📍 {player['current_location']}", inline=True)
        
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        release(conn)

@bot.command(name='inventory')
async def inventory_command(ctx):
    """View your fish inventory"""
    user = ctx.author
    conn = db()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT fish_name, rarity, value, COUNT(*) as quantity
            FROM fish_inventory 
            WHERE user_id = %s
            GROUP BY fish_name, rarity, value
            ORDER BY rarity DESC, value DESC
        """, (user.id,))
        
        fish = cursor.fetchall()
        
        if not fish:
            await ctx.send("Your inventory is empty! Go fishing! 🎣")
            return
        
        embed = discord.Embed(
            title=f"🎒 {user.name}'s Fish Inventory",
            color=discord.Color.purple()
        )
        
        total_value = 0
        for item in fish:
            value_total = item['value'] * item['quantity']
            total_value += value_total
            embed.add_field(
                name=f"{item['fish_name']} x{item['quantity']}",
                value=f"⭐ {item['rarity']} | 💰 {value_total} coins",
                inline=False
            )
        
        embed.add_field(name="**Total Value**", value=f"💰 {total_value} coins", inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        release(conn)

@bot.command(name='sell')
async def sell_command(ctx, *, fish_name: str = None):
    """Sell fish to merchant"""
    if ctx.channel.id != CHANNELS['merchant']:
        await ctx.send("Please use this command in the merchant channel! 🏪")
        return
    
    if not fish_name:
        await ctx.send("Please specify which fish to sell: `!sell [fish name]`")
        return
    
    user = ctx.author
    conn = db()
    try:
        cursor = conn.cursor()
        
        # Get fish from inventory
        cursor.execute("""
            SELECT * FROM fish_inventory 
            WHERE user_id = %s AND fish_name ILIKE %s
            LIMIT 1
        """, (user.id, f"%{fish_name}%"))
        
        fish = cursor.fetchone()
        
        if not fish:
            await ctx.send(f"You don't have any fish named '{fish_name}'!")
            return
        
        # Sell the fish
        cursor.execute("""
            DELETE FROM fish_inventory 
            WHERE id = %s
        """, (fish['id'],))
        
        cursor.execute("""
            UPDATE players 
            SET coins = coins + %s 
            WHERE user_id = %s
        """, (fish['value'], user.id))
        
        conn.commit()
        
        embed = discord.Embed(
            title="💰 Sale Complete!",
            description=f"Sold **{fish['fish_name']}** for **{fish['value']}** coins!",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"Error: {e}")
        conn.rollback()
    finally:
        release(conn)

# -------------------- Run Bot --------------------
if __name__ == "__main__":
    bot.run(TOKEN)
