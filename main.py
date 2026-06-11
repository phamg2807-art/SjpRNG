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

# ─── Game Data Config & Safe Initialization ─────────────────────────────────
GAME_DATA = {}
ACTIVE_DUNGEONS = {}  # Tracks running automated runs: {user_id: {message_object, current_enemy_hp, log_history}}

# Clean templates used ONLY to heal empty or missing files on startup
DEFAULT_WEAPONS = {
    "rusty_sword": {"name": "Rusty Sword", "type": "physical", "dmg": 2, "tier": "common"},
    "old_branch": {"name": "Old Branch", "type": "magical", "dmg": 1, "tier": "common"},
    "wooden_greatsword": {"name": "Wooden Greatsword", "type": "physical", "dmg": 6, "tier": "uncommon"},
    "basic_branch": {"name": "Basic Branch", "type": "magical", "dmg": 5, "tier": "uncommon"},
    "enchanted_spellblade": {"name": "Enchanted Spellblade", "type": "magical", "dmg": 15, "tier": "epic"},
    "timber_darkblade": {"name": "Timber Darkblade", "type": "physical", "dmg": 45, "tier": "legendary"}
}

DEFAULT_ARMORS = {
    "ripped_helmet": {"name": "Ripped Helmet", "hp": 5, "mn": 0, "tier": "common"},
    "ripped_shirt": {"name": "Ripped Shirt", "hp": 7, "mn": 0, "tier": "common"},
    "leather_helmet": {"name": "Leather Helmet", "hp": 10, "mn": 0, "tier": "uncommon"},
    "leather_shirt": {"name": "Leather Shirt", "hp": 12, "mn": 0, "tier": "uncommon"},
    "enchanted_fedora": {"name": "Enchanted Fedora", "hp": 18, "mn": 8, "tier": "epic"},
    "enchanted_chestplate": {"name": "Enchanted Chestplate", "hp": 22, "mn": 6, "tier": "epic"}
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

def load_game_data():
    global GAME_DATA
    data_dir = "data"
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    
    files = ['enemies.json', 'weapons.json', 'armors.json', 'maps.json']
    for file in files:
        name = file.replace('.json', '')
        path = os.path.join(data_dir, file)
        
        try:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path, 'r') as f:
                    GAME_DATA[name] = json.load(f)
                    print(f"✅ Loaded live dynamic configurations for {file}")
            else:
                raise ValueError("Empty file placeholder identified")
        except (json.JSONDecodeError, ValueError, IOError):
            print(f"⚠️ {file} missing/corrupt. Rebuilding dynamic disk file...")
            defaults = {}
            if name == 'weapons': defaults = DEFAULT_WEAPONS
            elif name == 'armors': defaults = DEFAULT_ARMORS
            elif name == 'enemies': defaults = DEFAULT_ENEMIES
            elif name == 'maps': defaults = DEFAULT_MAPS
            
            GAME_DATA[name] = defaults
            with open(path, 'w') as f:
                json.dump(defaults, f, indent=4)

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

# ─── DB Initialization ────────────────────────────────────────────────────────
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
                    item_key TEXT,
                    item_name TEXT,
                    item_type TEXT,
                    tier TEXT,
                    stat_bonus INT,
                    equipped BOOLEAN DEFAULT FALSE
                );
            """)
        conn.commit()
        get_pool().putconn(conn)
    print("✅ Database layers synced.")

# ─── Bot Setup ────────────────────────────────────────────────────────────────
TOKEN = os.getenv('DISCORD_TOKEN') or exit("ERROR: DISCORD_TOKEN missing!")
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

# ─── Dynamic Engine Drop Operations (Fully Bound to JSON) ─────────────────────
def roll_loot_drop(user_id):
    roll = random.uniform(0, 100)
    if roll <= 45.0: tier = "common"
    elif roll <= 80.0: tier = "uncommon"
    elif roll <= 95.0: tier = "epic"
    elif roll <= 99.0: tier = "legendary"
    else: tier = "secret"

    pool = []
    # Reads directly out of live JSON data objects now!
    for k, v in GAME_DATA.get('weapons', {}).items():
        if v.get('tier', '').lower() == tier: pool.append((k, v, 'weapon'))
    for k, v in GAME_DATA.get('armors', {}).items():
        if v.get('tier', '').lower() == tier: pool.append((k, v, 'armor'))

    if not pool:
        # Emergency safety handler if loaded json lacks specific tier elements
        all_weapons = list(GAME_DATA.get('weapons', {}).items())
        if all_weapons:
            chosen_key, chosen_item = random.choice(all_weapons)
            item_type = 'weapon'
        else:
            chosen_key, chosen_item, item_type = "rusty_sword", {"name": "Rusty Sword", "tier": "common", "dmg": 2}, 'weapon'
    else:
        chosen_key, chosen_item, item_type = random.choice(pool)

    bonus = chosen_item.get('dmg', 0) if item_type == 'weapon' else chosen_item.get('hp', 0)

    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_inventory (user_id, item_key, item_name, item_type, tier, stat_bonus)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, chosen_key, chosen_item['name'], item_type, chosen_item.get('tier', 'common'), bonus))
        conn.commit()
        get_pool().putconn(conn)
    return f"**{chosen_item['name']}** ({chosen_item.get('tier', 'common').upper()})"

# ─── Dynamic Live Combat Engine ───────────────────────────────────────────────
@tasks.loop(seconds=4.0)
async def automated_combat_tick():
    if not ACTIVE_DUNGEONS:
        return

    to_remove = []
    with get_pool().getconn() as conn:
        for user_id, session in list(ACTIVE_DUNGEONS.items()):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM players WHERE user_id = %s", (user_id,))
                player = cur.fetchone()
                
                if not player:
                    to_remove.append(user_id)
                    continue

                cur.execute("SELECT * FROM user_inventory WHERE user_id = %s AND equipped = TRUE", (user_id,))
                equipped_items = cur.fetchall()
                
                weapon_bonus = sum(i['stat_bonus'] for i in equipped_items if i['item_type'] == 'weapon')
                armor_bonus = sum(i['stat_bonus'] for i in equipped_items if i['item_type'] == 'armor')
                
                max_hp = player['max_hp'] + armor_bonus
                stage = player['current_stage']
                
                # Dynamic mapping straight from maps.json configuration files
                map_config = GAME_DATA.get('maps', {}).get('bright_forest', {})
                stages_config = map_config.get('stages', {})
                stage_enemies = stages_config.get(str(stage), {})
                
                if not stage_enemies:
                    stage_enemies = {"small_slime": 1}
                    
                enemy_id = list(stage_enemies.keys())[0]
                
                # Dynamic mapping straight from enemies.json configuration files
                enemy_template = GAME_DATA.get('enemies', {}).get(enemy_id)
                if not enemy_template:
                    enemy_template = {"name": enemy_id.replace('_', ' ').title(), "hp": 10, "dmg": 1, "exp": 2}
                
                # Automatic verification check for boss profiles
                is_boss = "boss" in enemy_template.get('name', '').lower() or stage == 10

                if 'enemy_hp' not in session:
                    session['enemy_hp'] = enemy_template.get('hp', 10)
                    session['turn_count'] = 0
                    session['logs'] = [f"⚔️ An aggressive **{enemy_template['name']}** appeared!"]

                # 🥊 1. Player Attacks Enemy
                dmg_dealt = max(1, (player['st'] + weapon_bonus) - 2)
                session['enemy_hp'] -= dmg_dealt
                session['logs'].append(f"💥 You dealt **{dmg_dealt}** damage to the enemy.")

                if session['enemy_hp'] <= 0:
                    session['logs'].append(f"🎉 **{enemy_template['name']}** defeated!")
                    new_xp = player['xp'] + enemy_template.get('exp', 2)
                    
                    if stage == 10:  # Beat Boss
                        cur.execute("UPDATE players SET is_exploring = FALSE, current_stage = 1, xp = %s WHERE user_id = %s", (new_xp, user_id))
                        conn.commit()
                        
                        drop1 = roll_loot_drop(user_id)
                        drop2 = roll_loot_drop(user_id)
                        drop3 = roll_loot_drop(user_id)
                        
                        embed = discord.Embed(title="🏆 DUNGEON CLEAR!", description=f"You successfully conquered the **Bright Forest**!\n\n🎁 **Boss Loot Drops Received:**\n1. {drop1}\n2. {drop2}\n3. {drop3}", color=discord.Color.gold())
                        bot.loop.create_task(session['message'].edit(embed=embed, view=None))
                        to_remove.append(user_id)
                        continue
                    else:
                        next_stage = stage + 1
                        cur.execute("UPDATE players SET current_stage = %s, xp = %s WHERE user_id = %s", (next_stage, new_xp, user_id))
                        conn.commit()
                        del session['enemy_hp']
                        continue
                
                # 🛡️ 2. Enemy Attacks Player
                session['turn_count'] += 1
                enemy_dmg = enemy_template.get('dmg', 0)
                
                if is_boss:
                    # Hardened Spellcaster counter mechanics logic block
                    if session['turn_count'] % 3 == 0:
                        enemy_dmg = enemy_template.get('dmg', 8)
                        session['logs'].append(f"🔮 {enemy_template['name']} casts an ultimate spell!")
                    else:
                        enemy_dmg = 0

                if enemy_dmg > 0:
                    player_current_hp = max(0, player['hp'] - enemy_dmg)
                    cur.execute("UPDATE players SET hp = %s WHERE user_id = %s", (player_current_hp, user_id))
                    conn.commit()
                    session['logs'].append(f"💔 The enemy retaliated and hit you for **{enemy_dmg}** DMG.")
                    
                    if player_current_hp <= 0:
                        session['logs'].append("💀 You collapsed in battle! Fleeing safely to camp.")
                        cur.execute("UPDATE players SET is_exploring = FALSE, hp = 20, current_stage = 1 WHERE user_id = %s", (user_id,))
                        conn.commit()
                        embed = discord.Embed(title="💀 Defeated!", description="Your party ran out of health and was forced out of the dungeon.", color=discord.Color.red())
                        bot.loop.create_task(session['message'].edit(embed=embed, view=None))
                        to_remove.append(user_id)
                        continue

                if len(session['logs']) > 4:
                    session['logs'] = session['logs'][-4:]

                hp_bar_pct = max(0, min(10, int((player['hp'] / max_hp) * 10)))
                hp_bar = "🟩" * hp_bar_pct + "⬛" * (10 - hp_bar_pct)

                embed = discord.Embed(title="⚔️ Dungeon: Bright Forest (AUTO)", color=discord.Color.dark_green())
                embed.add_field(name="❤️ Your HP Status", value=f"{hp_bar} ({player['hp']}/{max_hp})", inline=False)
                embed.add_field(name="👾 Current Target Status", value=f"**{enemy_template['name']}**: {session['enemy_hp']} HP", inline=True)
                embed.add_field(name="🚩 Stage Progress", value=f"Stage **{stage}/10**", inline=True)
                embed.add_field(name="📜 Live Combat Feed", value="\n".join(session['logs']), inline=False)
                
                bot.loop.create_task(session['message'].edit(embed=embed, view=None))

        get_pool().putconn(conn)

    for uid in to_remove:
        if uid in ACTIVE_DUNGEONS: del ACTIVE_DUNGEONS[uid]

# ─── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    load_game_data()
    automated_combat_tick.start()
    print(f"✅ Logged in successfully as {bot.user}")

# ─── Commands ─────────────────────────────────────────────────────────────────
@bot.command()
async def dungeon(ctx):
    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO players (user_id, hp, max_hp) VALUES (%s, 100, 100)
                ON CONFLICT (user_id) DO NOTHING
            """, (ctx.author.id,))
            conn.commit()
            cur.execute("SELECT * FROM players WHERE user_id = %s", (ctx.author.id,))
            player = cur.fetchone()
        get_pool().putconn(conn)

    if ctx.author.id in ACTIVE_DUNGEONS:
        await ctx.send("⚠️ You are already automatically playing in a dungeon session right now!")
        return

    embed = discord.Embed(title="⚔️ Entering Bright Forest...", description="Initializing automatic battle sequence lines...", color=discord.Color.green())
    msg = await ctx.send(embed=embed)
    
    ACTIVE_DUNGEONS[ctx.author.id] = {
        "message": msg,
        "logs": ["🚀 Character entered Bright Forest."]
    }

@bot.command()
async def inventory(ctx):
    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_inventory WHERE user_id = %s", (ctx.author.id,))
            items = cur.fetchall()
        get_pool().putconn(conn)

    if not items:
        await ctx.send("🎒 Your inventory is currently empty! Clear Stage 10 Boss rooms to drop items.")
        return

    inv_lines = []
    for item in items:
        status = "✨ [EQUIPPED]" if item['equipped'] else ""
        inv_lines.append(f"• ID: `{item['id']}` | **{item['item_name']}** ({item['tier'].upper()}) Bonus: +{item['stat_bonus']} {status}")

    embed = discord.Embed(title="🎒 Your Gear Inventory", description="\n".join(inv_lines), color=discord.Color.blue())
    await ctx.send(embed=embed)

@bot.command()
async def equip(ctx, item_id: int):
    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_inventory WHERE user_id = %s AND id = %s", (ctx.author.id, item_id))
            item = cur.fetchone()
            
            if not item:
                await ctx.send("❌ Item not found in your inventory profile lines.")
                get_pool().putconn(conn)
                return

            cur.execute("UPDATE user_inventory SET equipped = FALSE WHERE user_id = %s AND item_type = %s", (ctx.author.id, item['item_type']))
            cur.execute("UPDATE user_inventory SET equipped = TRUE WHERE id = %s", (item_id,))
            conn.commit()
        get_pool().putconn(conn)

    await ctx.send(f"✅ Successfully equipped **{item['item_name']}**!")

@bot.command()
async def stats(ctx):
    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM players WHERE user_id = %s", (ctx.author.id,))
            p = cur.fetchone()
        get_pool().putconn(conn)
        
    if not p:
        await ctx.send("❌ Register first by typing `-dungeon`.")
        return
        
    await ctx.send(f"📊 **Your Core Stats:**\nLevel: {p['level']} | XP: {p['xp']}\nHP: {p['hp']}/{p['max_hp']} | ST: {p['st']} | MN: {p['mn']} | DF: {p['df']}")

bot.run(TOKEN)
