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

sys.stdout.reconfigure(line_buffering=True)

# ─── Game Data Config (Updated with your Custom Assets) ────────────────────
GAME_DATA = {}
ACTIVE_DUNGEONS = {}

# Weapon definitions now linked to your custom CDN artwork
DEFAULT_WEAPONS = {
    "rusty_sword": {"name": "Rusty Sword", "type": "weapon", "st": 2, "mn": 0, "hp": 0, "tier": "common", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514648256313823316/content.png?ex=6a2c219d&is=6a2ad01d&hm=9c83656b0c962c6294ccf802225796287549958e8334f05229c41987205d16f9&"},
    "old_branch": {"name": "Old Branch", "type": "weapon", "st": 0, "mn": 1, "hp": 0, "tier": "common", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514648666588319938/content.png?ex=6a2c21fe&is=6a2ad07e&hm=da0e1f9aa4e533cfd61519e226d102e234209d30f26f3c00609e833d409feb22&"},
    "wooden_greatsword": {"name": "Wooden Greatsword", "type": "weapon", "st": 6, "mn": 0, "hp": 0, "tier": "uncommon", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514650422931886202/content.png?ex=6a2c23a1&is=6a2ad221&hm=26d00817a02fcd73c56a0d4df37d02d9beb81af0df8b03e7f0699578df665941&"},
    "basic_branch": {"name": "Basic Branch", "type": "weapon", "st": 0, "mn": 5, "hp": 0, "tier": "uncommon", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514650908456390736/content.png?ex=6a2c2415&is=6a2ad295&hm=747ea378d7304c68e890b5cfd8eb4cee86bb05891478a92483ce2ee1e0410796&"},
    "enchanted_spellblade": {"name": "Enchanted Spellblade", "type": "weapon", "st": 0, "mn": 15, "hp": 0, "tier": "epic", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514652861911273652/content.png?ex=6a2c25e7&is=6a2ad467&hm=efde82b21a2b027fa668ddd10ac012814ad93bf0a627ab126794140d950f9be0&"},
    "timber_darkblade": {"name": "Timber Darkblade", "type": "weapon", "st": 45, "mn": 0, "hp": 0, "tier": "legendary", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514654812703297606/content.png?ex=6a2c27b8&is=6a2ad638&hm=026a963fc9550744c09eaa7ffa8a18ccd29f65d33630b773112690209aa68b1a&"}
}

# Armor definitions linked to your custom CDN artwork
DEFAULT_ARMORS = {
    "ripped_helmet": {"name": "Ripped Helmet", "type": "helmet", "hp": 5, "mn": 0, "st": 0, "tier": "common", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514649621543256224/content.png?ex=6a2c22e2&is=6a2ad162&hm=612222145cca4f0cf6759006db63855b5afb8e9ba46a2d71e5da8dfe2feaf5ec&"},
    "ripped_shirt": {"name": "Ripped Shirt", "type": "chestplate", "hp": 7, "mn": 0, "st": 0, "tier": "common", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514649974137425920/content.png?ex=6a2c2336&is=6a2ad1b6&hm=c7ec32847f8c57ef5d3f6f8183c6dd08f1f7837fb49d19b789fdceb9d0e61c84&"},
    "leather_helmet": {"name": "Leather Helmet", "type": "helmet", "hp": 10, "mn": 0, "st": 0, "tier": "uncommon", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514651383243214988/content.png?ex=6a2c2486&is=6a2ad306&hm=cc4ee64d71c585c94f3e9df6433f78c620d78100865d156d10fea546beba8d38&"},
    "leather_shirt": {"name": "Leather Shirt", "type": "chestplate", "hp": 12, "mn": 0, "st": 0, "tier": "uncommon", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514652422994264064/content.png?ex=6a2c257e&is=6a2ad3fe&hm=62306fc2cf1370d886d5ec97a3b7bca1130e6e81a624929db1b1d032e7e2bfa7&"},
    "enchanted_fedora": {"name": "Enchanted Fedora", "type": "helmet", "hp": 18, "mn": 8, "st": 0, "tier": "epic", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514653217160560750/content.png?ex=6a2c263b&is=6a2ad4bb&hm=ee4443fe3ae92f72b66c4e0243a3eb47540238b28a779714e888127c44ea075d&"},
    "enchanted_chestplate": {"name": "Enchanted Chestplate", "type": "chestplate", "hp": 22, "mn": 6, "st": 0, "tier": "epic", "img": "https://cdn.discordapp.com/attachments/1514648232268005436/1514653986320416939/content.png?ex=6a2c26f3&is=6a2ad573&hm=d227c2f2cdad074782e8c28b9427aa1f936fd22c9f53db783db0e118dc38bdb1&"}
}

DEFAULT_ENEMIES = {
    "small_slime": {"name": "Small Slime", "hp": 4, "dmg": 0, "exp": 1, "boss": False},
    "basic_slime": {"name": "Basic Slime", "hp": 8, "dmg": 0, "exp": 2, "boss": False},
    "large_slime": {"name": "Large Slime", "hp": 12, "dmg": 0, "exp": 5, "boss": False},
    "small_goblin": {"name": "Small Goblin", "hp": 2, "dmg": 1, "exp": 2, "boss": False},
    "goblin": {"name": "Goblin", "hp": 6, "dmg": 2, "exp": 6, "boss": False},
    "ripped_goblin": {"name": "Ripped Goblin", "hp": 12, "dmg": 5, "exp": 15, "boss": False},
    "hardened_spellcaster": {"name": "Hardened Spellcaster", "hp": 70, "dmg": 8, "exp": 60, "boss": True}
}

DEFAULT_MAPS = {
    "bright_forest": {
        "name": "Bright Forest",
        "stages": {
            "1": {"small_slime": 2},
            "2": {"small_slime": 5, "basic_slime": 2},
            "3": {"basic_slime": 3, "large_slime": 1},
            "4": {"basic_slime": 3, "large_slime": 2, "small_goblin": 3},
            "5": {"large_slime": 6, "small_goblin": 3, "goblin": 2},
            "6": {"small_goblin": 3, "goblin": 3},
            "7": {"small_goblin": 5, "goblin": 3},
            "8": {"goblin": 5, "ripped_goblin": 1},
            "9": {"ripped_goblin": 2},
            "10": {"hardened_spellcaster": 1}
        }
    }
}

# ─── Logic Helpers ───

def get_item_image_url(item_key):
    """Retrieves the CDN link from GAME_DATA configuration."""
    all_items = {**DEFAULT_WEAPONS, **DEFAULT_ARMORS}
    return all_items.get(item_key, {}).get("img", "")

def get_item_slot(item_key, current_type):
    """Dynamic Slot Resolver."""
    if item_key in DEFAULT_WEAPONS: return "weapon"
    if item_key in DEFAULT_ARMORS: return DEFAULT_ARMORS[item_key]['type']
    k = str(item_key).lower()
    if "helmet" in k or "fedora" in k: return "helmet"
    if "shirt" in k or "chestplate" in k: return "chestplate"
    return current_type

def get_visual_emojis(item_type, tier):
    """Generates procedural tier indicators."""
    t = str(tier).lower()
    t_icon = "⚪"
    if "uncommon" in t: t_icon = "🟢"
    elif "epic" in t: t_icon = "🟣"
    elif "legendary" in t: t_icon = "🟡"
    
    i_emoji = "📦"
    if item_type == "weapon": i_emoji = "⚔️"
    elif item_type == "helmet": i_emoji = "🪖"
    elif item_type == "chestplate": i_emoji = "👕"
    return f"{t_icon} {i_emoji}"

def load_game_data():
    global GAME_DATA
    GAME_DATA = {
        'weapons': DEFAULT_WEAPONS,
        'armors': DEFAULT_ARMORS,
        'enemies': DEFAULT_ENEMIES,
        'maps': DEFAULT_MAPS
    }
    print("✅ Game assets and item images loaded into memory.")

# ─── Flask Keep-Alive ─────────────────────────────────────────────────────────
app = Flask('')
@app.route('/')
def home(): return "Bot is alive!"
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── DB & Redis Pools ─────────────────────────────────────────────────────────
DB_URL = os.getenv('DATABASE_URL') or exit("ERROR: DATABASE_URL missing!")
_pool = None

def get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(2, 10, dsn=DB_URL, cursor_factory=RealDictCursor, sslmode='require', connect_timeout=10)
    return _pool

REDIS_URL = os.getenv('REDIS_URL') or exit("ERROR: REDIS_URL missing!")
rd = redis.from_url(REDIS_URL, decode_responses=True)

# ─── DB Initialization ──────────────────────────────────────────
def init_db():
    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    user_id BIGINT PRIMARY KEY,
                    hp INT DEFAULT 100, max_hp INT DEFAULT 100, st INT DEFAULT 10, df INT DEFAULT 10, mn INT DEFAULT 10,
                    unallocated_points INT DEFAULT 5, gold INT DEFAULT 0, xp INT DEFAULT 0, level INT DEFAULT 1,
                    current_stage INT DEFAULT 1, is_exploring BOOLEAN DEFAULT FALSE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_inventory (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES players(user_id) ON DELETE CASCADE,
                    item_key TEXT, item_name TEXT, item_type TEXT, tier TEXT, stat_bonus INT, equipped BOOLEAN DEFAULT FALSE,
                    bonus_hp INT DEFAULT 0, bonus_st INT DEFAULT 0, bonus_mn INT DEFAULT 0
                );
            """)
        conn.commit()
        get_pool().putconn(conn)
    print("✅ Database structural hotpatches synced.")

# ─── Bot Setup ────────────────────────────────────────────────────────────────
TOKEN = os.getenv('DISCORD_TOKEN') or exit("ERROR: DISCORD_TOKEN missing!")
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

# ─── Commands ─────────────────────────────────────────────────────────────────

@bot.command()
async def inventory(ctx):
    """Displays inventory with custom asset thumbnails."""
    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_inventory WHERE user_id = %s ORDER BY id ASC", (ctx.author.id,))
            items = cur.fetchall()
        get_pool().putconn(conn)

    if not items:
        await ctx.send("🎒 Your inventory is empty!")
        return

    embed = discord.Embed(title="🎒 Your Equipment Vault", color=discord.Color.blue())
    for item in items:
        slot = get_item_slot(item['item_key'], item['item_type'])
        emoji = get_visual_emojis(slot, item['tier'])
        embed.add_field(
            name=f"{emoji} {item['item_name']} (ID: {item['id']})", 
            value=f"Type: {slot.capitalize()} | Tier: {item['tier'].capitalize()}\n[View Asset]({get_item_image_url(item['item_key'])})", 
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command()
async def equip(ctx, item_id: int):
    """Equips an item and displays its custom artwork."""
    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_inventory WHERE user_id = %s AND id = %s", (ctx.author.id, item_id))
            item = cur.fetchone()
            if not item:
                await ctx.send("❌ Item not found.")
                get_pool().putconn(conn)
                return
            
            # Logic to handle slot-swapping
            slot = get_item_slot(item['item_key'], item['item_type'])
            cur.execute("UPDATE user_inventory SET equipped = FALSE WHERE user_id = %s AND item_type = %s", (ctx.author.id, slot))
            cur.execute("UPDATE user_inventory SET equipped = TRUE WHERE id = %s", (item_id,))
            conn.commit()
        get_pool().putconn(conn)
        
    embed = discord.Embed(title=f"✨ Equipped {item['item_name']}!", color=discord.Color.green())
    embed.set_thumbnail(url=get_item_image_url(item['item_key']))
    await ctx.send(embed=embed)

@bot.event
async def on_ready():
    init_db()
    load_game_data()
    print(f"✅ Logged in as {bot.user}")

bot.run(TOKEN)
