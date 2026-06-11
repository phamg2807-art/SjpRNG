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
ACTIVE_DUNGEONS = {}  # Tracks running automated runs: {user_id: {message_object, dungeon_id, current_enemy_hp, log_history}}

# Comprehensive database profile templates updated with custom explicit stats
DEFAULT_WEAPONS = {
    "rusty_sword": {"name": "Rusty Sword", "type": "weapon", "st": 2, "mn": 0, "hp": 0, "tier": "common"},
    "old_branch": {"name": "Old Branch", "type": "weapon", "st": 0, "mn": 1, "hp": 0, "tier": "common"},
    "wooden_greatsword": {"name": "Wooden Greatsword", "type": "weapon", "st": 6, "mn": 0, "hp": 0, "tier": "uncommon"},
    "basic_branch": {"name": "Basic Branch", "type": "weapon", "st": 0, "mn": 5, "hp": 0, "tier": "uncommon"},
    "enchanted_spellblade": {"name": "Enchanted Spellblade", "type": "weapon", "st": 0, "mn": 15, "hp": 0, "tier": "epic"},
    "timber_darkblade": {"name": "Timber Darkblade", "type": "weapon", "st": 45, "mn": 0, "hp": 0, "tier": "legendary"}
}

DEFAULT_ARMORS = {
    "ripped_helmet": {"name": "Ripped Helmet", "type": "helmet", "hp": 5, "mn": 0, "st": 0, "tier": "common"},
    "ripped_shirt": {"name": "Ripped Shirt", "type": "chestplate", "hp": 7, "mn": 0, "st": 0, "tier": "common"},
    "leather_helmet": {"name": "Leather Helmet", "type": "helmet", "hp": 10, "mn": 0, "st": 0, "tier": "uncommon"},
    "leather_shirt": {"name": "Leather Shirt", "type": "chestplate", "hp": 12, "mn": 0, "st": 0, "tier": "uncommon"},
    "enchanted_fedora": {"name": "Enchanted Fedora", "type": "helmet", "hp": 18, "mn": 8, "st": 0, "tier": "epic"},
    "enchanted_chestplate": {"name": "Enchanted Chestplate", "type": "chestplate", "hp": 22, "mn": 6, "st": 0, "tier": "epic"}
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

def get_visual_emojis(item_type, tier):
    """Generates procedural rich text graphical headers for database drop profiles."""
    t = str(tier).lower()
    t_emoji = "⚪"
    if "uncommon" in t: t_emoji = "🟢"
    elif "epic" in t: t_emoji = "🟣"
    elif "legendary" in t: t_emoji = "🟡"
    
    i_emoji = "📦"
    if item_type == "weapon": i_emoji = "⚔️"
    elif item_type == "helmet": i_emoji = "🪖"
    elif item_type == "chestplate": i_emoji = "👕"
    return f"{t_emoji} {i_emoji}"

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

# ─── DB Initialization & Migrations ──────────────────────────────────────────
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
            
            # Hotpatch Migration: Keep players table structural data updated
            for col in [
                ("max_hp", "INT DEFAULT 100"), ("st", "INT DEFAULT 10"), ("df", "INT DEFAULT 10"),
                ("mn", "INT DEFAULT 10"), ("unallocated_points", "INT DEFAULT 5"), ("gold", "INT DEFAULT 0"),
                ("xp", "INT DEFAULT 0"), ("level", "INT DEFAULT 1"), ("current_stage", "INT DEFAULT 1"),
                ("is_exploring", "BOOLEAN DEFAULT FALSE")
            ]:
                cur.execute(f"ALTER TABLE players ADD COLUMN IF NOT EXISTS {col[0]} {col[1]};")
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_inventory (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES players(user_id) ON DELETE CASCADE,
                    item_key TEXT, item_name TEXT, item_type TEXT, tier TEXT, stat_bonus INT, equipped BOOLEAN DEFAULT FALSE
                );
            """)
            
            # Hotpatch Migration: Inject explicit modular attributes columns into inventory records
            cur.execute("ALTER TABLE user_inventory ADD COLUMN IF NOT EXISTS bonus_hp INT DEFAULT 0;")
            cur.execute("ALTER TABLE user_inventory ADD COLUMN IF NOT EXISTS bonus_st INT DEFAULT 0;")
            cur.execute("ALTER TABLE user_inventory ADD COLUMN IF NOT EXISTS bonus_mn INT DEFAULT 0;")
            
        conn.commit()
        get_pool().putconn(conn)
    print("✅ Database structural hotpatches synced successfully.")

# ─── Bot Setup ────────────────────────────────────────────────────────────────
TOKEN = os.getenv('DISCORD_TOKEN') or exit("ERROR: DISCORD_TOKEN missing!")
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

# ─── Dynamic Loot Drop Engine ────────────────────────────────────────────────
def roll_loot_drop(user_id):
    roll = random.uniform(0, 100)
    if roll <= 45.0: tier = "common"
    elif roll <= 80.0: tier = "uncommon"
    elif roll <= 95.0: tier = "epic"
    else: tier = "legendary"

    pool = []
    for k, v in GAME_DATA.get('weapons', {}).items():
        if v.get('tier', '').lower() == tier: pool.append((k, v, 'weapon'))
    for k, v in GAME_DATA.get('armors', {}).items():
        if v.get('tier', '').lower() == tier: pool.append((k, v, v.get('type', 'helmet')))

    if not pool:
        chosen_key, chosen_item, item_type = "rusty_sword", DEFAULT_WEAPONS["rusty_sword"], 'weapon'
    else:
        chosen_key, chosen_item, item_type = random.choice(pool)

    b_hp = chosen_item.get('hp', 0)
    b_st = chosen_item.get('st', 0)
    b_mn = chosen_item.get('mn', 0)
    legacy_bonus = b_st if item_type == 'weapon' else b_hp

    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_inventory (user_id, item_key, item_name, item_type, tier, stat_bonus, bonus_hp, bonus_st, bonus_mn)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (user_id, chosen_key, chosen_item['name'], item_type, chosen_item.get('tier', 'common'), legacy_bonus, b_hp, b_st, b_mn))
        conn.commit()
        get_pool().putconn(conn)
        
    v_emoji = get_visual_emojis(item_type, chosen_item.get('tier', 'common'))
    return f"{v_emoji} **{chosen_item['name']}** ({chosen_item.get('tier', 'common').upper()})"

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
                
                # Accrue modular gear parameters securely
                bonus_hp = sum(i.get('bonus_hp', 0) for i in equipped_items)
                bonus_st = sum(i.get('bonus_st', 0) for i in equipped_items)
                bonus_mn = sum(i.get('bonus_mn', 0) for i in equipped_items)
                
                max_hp = player['max_hp'] + bonus_hp
                stage = player['current_stage']
                
                dungeon_id = session.get('dungeon_id', 'bright_forest')
                map_config = GAME_DATA.get('maps', {}).get(dungeon_id, {})
                dungeon_name = map_config.get('name', dungeon_id.replace('_', ' ').title())
                
                stages_config = map_config.get('stages', {})
                stage_enemies = stages_config.get(str(stage), {})
                
                if not stage_enemies:
                    stage_enemies = {"small_slime": 1}
                    
                enemy_id = list(stage_enemies.keys())[0]
                enemy_template = GAME_DATA.get('enemies', {}).get(enemy_id, {"name": "Slime", "hp": 10, "dmg": 1, "exp": 2})
                is_boss = stage == 10

                if 'enemy_hp' not in session:
                    session['enemy_hp'] = enemy_template.get('hp', 10)
                    session['turn_count'] = 0
                    session['logs'] = [f"⚔️ An aggressive **{enemy_template['name']}** appeared!"]

                # 🥊 1. Player Attacks Enemy
                dmg_dealt = max(1, (player['st'] + bonus_st) - 2)
                session['enemy_hp'] -= dmg_dealt
                session['logs'].append(f"💥 You dealt **{dmg_dealt}** damage to the enemy.")

                if session['enemy_hp'] <= 0:
                    session['logs'].append(f"🎉 **{enemy_template['name']}** defeated!")
                    new_xp = player['xp'] + enemy_template.get('exp', 2)
                    
                    if stage == 10:  # Conquered Boss
                        cur.execute("UPDATE players SET is_exploring = FALSE, current_stage = 1, xp = %s WHERE user_id = %s", (new_xp, user_id))
                        conn.commit()
                        
                        drop1 = roll_loot_drop(user_id)
                        drop2 = roll_loot_drop(user_id)
                        drop3 = roll_loot_drop(user_id)
                        
                        embed = discord.Embed(title=f"🏆 {dungeon_name.upper()} CONQUERED!", description=f"You successfully cleared the run!\n\n🎁 **Boss Drops Gathered:**\n{drop1}\n{drop2}\n{drop3}", color=discord.Color.gold())
                        bot.loop.create_task(session['message'].edit(embed=embed, view=None))
                        to_remove.append(user_id)
                        continue
                    else:
                        next_stage = stage + 1
                        cur.execute("UPDATE players SET current_stage = %s, xp = %s WHERE user_id = %s", (next_stage, new_xp, user_id))
                        conn.commit()
                        session['logs'].append(f"🚩 Advancing directly to Stage **{next_stage}/10**...")
                        del session['enemy_hp']
                        
                        # Fix Frame Skipping Lag: Edit message now to display victory frames natively
                        hp_bar_pct = max(0, min(10, int((player['hp'] / max_hp) * 10)))
                        hp_bar = "🟩" * hp_bar_pct + "⬛" * (10 - hp_bar_pct)
                        embed = discord.Embed(title=f"⚔️ Dungeon: {dungeon_name} (AUTO)", color=discord.Color.dark_green())
                        embed.add_field(name="❤️ Your HP Status", value=f"{hp_bar} ({player['hp']}/{max_hp})", inline=False)
                        embed.add_field(name="👾 Target Status", value="Defeated!", inline=True)
                        embed.add_field(name="🚩 Stage Progress", value=f"Stage **{stage}/10**", inline=True)
                        embed.add_field(name="📜 Live Combat Feed", value="\n".join(session['logs'][-4:]), inline=False)
                        bot.loop.create_task(session['message'].edit(embed=embed, view=None))
                        continue
                
                # 🛡️ 2. Enemy Attacks Player
                session['turn_count'] += 1
                enemy_dmg = enemy_template.get('dmg', 1)
                
                if is_boss and session['turn_count'] % 3 == 0:
                    enemy_dmg = enemy_template.get('dmg', 8) * 2
                    session['logs'].append(f"🔮 {enemy_template['name']} casts an ultimate spell!")

                if enemy_dmg > 0:
                    player_current_hp = max(0, player['hp'] - enemy_dmg)
                    cur.execute("UPDATE players SET hp = %s WHERE user_id = %s", (player_current_hp, user_id))
                    conn.commit()
                    session['logs'].append(f"💔 Enemy counter-attacked you for **{enemy_dmg}** DMG.")
                    
                    if player_current_hp <= 0:
                        session['logs'].append("💀 You collapsed! Safely routed back to camp.")
                        cur.execute("UPDATE players SET is_exploring = FALSE, hp = 50, current_stage = 1 WHERE user_id = %s", (user_id,))
                        conn.commit()
                        embed = discord.Embed(title="💀 Defeated!", description=f"Your party passed out inside {dungeon_name}.", color=discord.Color.red())
                        bot.loop.create_task(session['message'].edit(embed=embed, view=None))
                        to_remove.append(user_id)
                        continue

                hp_bar_pct = max(0, min(10, int((player['hp'] / max_hp) * 10)))
                hp_bar = "🟩" * hp_bar_pct + "⬛" * (10 - hp_bar_pct)

                embed = discord.Embed(title=f"⚔️ Dungeon: {dungeon_name} (AUTO)", color=discord.Color.dark_green())
                embed.add_field(name="❤️ Your HP Status", value=f"{hp_bar} ({player['hp']}/{max_hp})", inline=False)
                embed.add_field(name="👾 Current Target Status", value=f"**{enemy_template['name']}**: {session['enemy_hp']} HP", inline=True)
                embed.add_field(name="🚩 Stage Progress", value=f"Stage **{stage}/10**", inline=True)
                embed.add_field(name="📜 Live Combat Feed", value="\n".join(session['logs'][-4:]), inline=False)
                
                bot.loop.create_task(session['message'].edit(embed=embed, view=None))

        get_pool().putconn(conn)

    for uid in to_remove:
        if uid in ACTIVE_DUNGEONS: del ACTIVE_DUNGEONS[uid]

# ─── Interactive Components Module ───────────────────────────────────────────
class DungeonDropdown(discord.ui.Select):
    def __init__(self, options_list):
        super().__init__(placeholder="Select a dungeon landscape map...", min_values=1, max_values=1, options=options_list)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.view.author_id:
            await interaction.response.send_message("❌ You are not the author initiating this dungeon sequence.", ephemeral=True)
            return

        dungeon_id = self.values[0]
        user_id = interaction.user.id

        if user_id in ACTIVE_DUNGEONS:
            await interaction.response.send_message("⚠️ You are already executing an active dynamic run!", ephemeral=True)
            return

        with get_pool().getconn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO players (user_id, hp, max_hp) VALUES (%s, 100, 100) ON CONFLICT (user_id) DO NOTHING", (user_id,))
                conn.commit()
            get_pool().putconn(conn)

        map_config = GAME_DATA.get('maps', {}).get(dungeon_id, {})
        dungeon_name = map_config.get('name', dungeon_id.replace('_', ' ').title())

        embed = discord.Embed(title=f"⚔️ Entering {dungeon_name}...", description="Initializing automatic battle sequence lines...", color=discord.Color.green())
        await interaction.response.edit_message(embed=embed, view=None)
        msg = await interaction.original_response()

        ACTIVE_DUNGEONS[user_id] = {
            "message": msg,
            "dungeon_id": dungeon_id,
            "logs": [f"🚀 Character entered {dungeon_name}."]
        }

class DungeonDropdownView(discord.ui.View):
    def __init__(self, author_id, options_list):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.add_item(DungeonDropdown(options_list))

# ─── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    load_game_data()
    if not automated_combat_tick.is_running():
        automated_combat_tick.start()
    print(f"✅ Logged in successfully as {bot.user}")

# ─── Commands ─────────────────────────────────────────────────────────────────
@bot.command()
async def help(ctx):
    embed = discord.Embed(title="📜 SjpHelper RPG Help Menu", description="Welcome adventurer! Manage commands easily below:", color=discord.Color.blue())
    embed.add_field(name="⚔️ `-dungeon [optional_name]`", value="Launch dynamic dropdown menu selector or skip selection (e.g., `-dungeon bright_forest`).", inline=False)
    embed.add_field(name="🎒 `-inventory`", value="View collected items vault and review currently equipped equipment cards.", inline=False)
    embed.add_field(name="✨ `-equip [Item ID]`", value="Equip items into independent modular allocation slots.", inline=False)
    embed.add_field(name="🛡️ `-party`", value="View group team composition structures and interactive invites status.", inline=False)
    embed.add_field(name="📊 `-stats`", value="Display core base stats profile.", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def dungeon(ctx, choice: str = None):
    if ctx.author.id in ACTIVE_DUNGEONS:
        await ctx.send("⚠️ You are already automatically playing in a dungeon session right now!")
        return

    maps_pool = GAME_DATA.get('maps', {})
    if not maps_pool:
        await ctx.send("❌ Error: No maps are loaded into the game architecture config files.")
        return

    if choice and choice in maps_pool:
        with get_pool().getconn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO players (user_id, hp, max_hp) VALUES (%s, 100, 100) ON CONFLICT (user_id) DO NOTHING", (ctx.author.id,))
                conn.commit()
            get_pool().putconn(conn)

        dungeon_name = maps_pool[choice].get('name', choice.replace('_', ' ').title())
        embed = discord.Embed(title=f"⚔️ Entering {dungeon_name}...", description="Initializing automatic battle sequence lines...", color=discord.Color.green())
        msg = await ctx.send(embed=embed)
        
        ACTIVE_DUNGEONS[ctx.author.id] = {
            "message": msg,
            "dungeon_id": choice,
            "logs": [f"🚀 Character entered {dungeon_name}."]
        }
        return

    options = []
    for key, data in maps_pool.items():
        name = data.get('name', key.replace('_', ' ').title())
        options.append(discord.SelectOption(label=name, value=key, description=f"Explore stages inside {name}"))

    embed = discord.Embed(title="🗺️ Dungeon Selection Hub", description="Choose which region map configuration you want your party to venture into:", color=discord.Color.blurple())
    view = DungeonDropdownView(ctx.author.id, options)
    await ctx.send(embed=embed, view=view)

@bot.command()
async def inventory(ctx):
    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_inventory WHERE user_id = %s ORDER BY id ASC", (ctx.author.id,))
            items = cur.fetchall()
        get_pool().putconn(conn)

    if not items:
        await ctx.send("🎒 Your inventory is empty! Defeat Stage 10 Dungeon Bosses to gain gear.")
        return

    equipped_weapon = "❌ *Empty slot*"
    equipped_helmet = "❌ *Empty slot*"
    equipped_chestplate = "❌ *Empty slot*"
    
    inv_lines = []
    for item in items:
        hp_stat = item.get('bonus_hp', 0)
        st_stat = item.get('bonus_st', 0)
        mn_stat = item.get('bonus_mn', 0)
        
        stat_pieces = []
        if hp_stat: stat_pieces.append(f"+{hp_stat} HP")
        if st_stat: stat_pieces.append(f"+{st_stat} ST")
        if mn_stat: stat_pieces.append(f"+{mn_stat} MN")
        if not stat_pieces: stat_pieces.append(f"+{item['stat_bonus']} Bonus")
        stat_label = ", ".join(stat_pieces)
        
        v_emoji = get_visual_emojis(item['item_type'], item['tier'])
        status_line = f"• ID: `{item['id']}` | {v_emoji} **{item['item_name']}** ({item['tier'].upper()}) [{stat_label}]"
        
        if item['equipped']:
            status_line += " ✨ **[EQUIPPED]**"
            if item['item_type'] == "weapon": equipped_weapon = f"{v_emoji} **{item['item_name']}** ({stat_label})"
            elif item['item_type'] == "helmet": equipped_helmet = f"{v_emoji} **{item['item_name']}** ({stat_label})"
            elif item['item_type'] == "chestplate": equipped_chestplate = f"{v_emoji} **{item['item_name']}** ({stat_label})"
            
        inv_lines.append(status_line)

    embed = discord.Embed(title="🎒 Your Equipment & Inventory Vault", color=discord.Color.blue())
    # Dedicated Equipment Tab UI Component Module
    embed.add_field(name="🛡️ EQUIPPED SLOTS", value=f"⚔️ **Weapon Slot:** {equipped_weapon}\n🪖 **Helmet Slot:** {equipped_helmet}\n👕 **Chestplate Slot:** {equipped_chestplate}", inline=False)
    embed.add_field(name="📦 VAULT STOCK ITEMS", value="\n".join(inv_lines), inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def equip(ctx, item_id: int):
    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_inventory WHERE user_id = %s AND id = %s", (ctx.author.id, item_id))
            item = cur.fetchone()
            
            if not item:
                await ctx.send("❌ Item not found in your inventory vault profile.")
                get_pool().putconn(conn)
                return

            # Safely filters and releases ONLY items corresponding to the same explicit matching slot category
            cur.execute("UPDATE user_inventory SET equipped = FALSE WHERE user_id = %s AND item_type = %s", (ctx.author.id, item['item_type']))
            cur.execute("UPDATE user_inventory SET equipped = TRUE WHERE id = %s", (item_id,))
            conn.commit()
        get_pool().putconn(conn)

    v_emoji = get_visual_emojis(item['item_type'], item['tier'])
    await ctx.send(f"✅ Successfully equipped {v_emoji} **{item['item_name']}** into your {item['item_type'].upper()} slot!")

@bot.command()
async def party(ctx):
    """Lobby coordination command card."""
    embed = discord.Embed(title="🛡️ Party Co-Op Command Board", description="Form cooperative parties to battle custom dungeons together!", color=discord.Color.purple())
    embed.add_field(name="👥 Your Lobby", value=f"👑 Leader: {ctx.author.mention}\n• Member 2: *Empty Open Slot*\n• Member 3: *Empty Open Slot*", inline=False)
    embed.set_footer(text="Multiplayer dungeon queues are fully initializing soon!")
    await ctx.send(embed=embed)

@bot.command()
async def stats(ctx):
    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM players WHERE user_id = %s", (ctx.author.id,))
            p = cur.fetchone()
            if p:
                cur.execute("SELECT * FROM user_inventory WHERE user_id = %s AND equipped = TRUE", (ctx.author.id,))
                eq = cur.fetchall()
            else:
                eq = []
        get_pool().putconn(conn)
        
    if not p:
        await ctx.send("❌ Register your profile parameters first by executing `-dungeon`.")
        return
        
    bonus_hp = sum(i.get('bonus_hp', 0) for i in eq)
    bonus_st = sum(i.get('bonus_st', 0) for i in eq)
    bonus_mn = sum(i.get('bonus_mn', 0) for i in eq)
    
    display_max_hp = p['max_hp'] + bonus_hp
    display_st = p['st'] + bonus_st
    display_mn = p['mn'] + bonus_mn
    
    await ctx.send(f"📊 **Your Hero Stats Profile:**\nLevel: {p['level']} | XP: {p['xp']}\nHP: {p['hp']}/{display_max_hp} (Bonus: +{bonus_hp})\nST: {display_st} (Bonus: +{bonus_st})\nMN: {display_mn} (Bonus: +{bonus_mn})\nDF: {p['df']}")

bot.run(TOKEN)
