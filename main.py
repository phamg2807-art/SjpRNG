import os, sys, redis, json, random, string, asyncio
sys.stdout.reconfigure(line_buffering=True)

from flask import Flask
from threading import Thread
from datetime import datetime, timezone

import discord
from discord.ext import commands

from db import (
    init_db, get_conn,
    get_total_stats, exp_for_level,
    TRADE_TAX, RARITY_COLOR, RARITY_EMOJI
)

# ─── Flask Keep-Alive ─────────────────────────────────────────────────────────
app = Flask('')
@app.route('/')
def home(): return "Bot is alive!"
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── Redis ────────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv('REDIS_URL') or exit("ERROR: REDIS_URL missing!")
rd = redis.from_url(REDIS_URL, decode_responses=True)

# ─── Bot ──────────────────────────────────────────────────────────────────────
TOKEN = os.getenv('DISCORD_TOKEN') or exit("ERROR: DISCORD_TOKEN missing!")
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

ADMIN_ROLE_ID = 920309927375933490

def is_admin(m):
    return m.guild_permissions.administrator or any(r.id == ADMIN_ROLE_ID for r in m.roles)

# ─── Helpers ──────────────────────────────────────────────────────────────────
def gen_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def fmt_number(n):
    return f"{int(n):,}"

# ═══════════════════════════════════════════════════════════════════════════════
# CHARACTER COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="start")
async def start_cmd(ctx, *, name: str = None):
    """Create your character. -start <name>"""
    if not name:
        await ctx.send("❌ Please provide a name. Example: `-start Arthur`"); return
    if len(name) > 24:
        await ctx.send("❌ Name must be 24 characters or fewer."); return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM characters WHERE user_id=%s", (ctx.author.id,))
            if cur.fetchone():
                await ctx.send("❌ You already have a character! Use `-profile` to view it."); return
            cur.execute("""
                INSERT INTO characters (user_id, name, level, exp, exp_needed, hp, max_hp, atk, def, gold)
                VALUES (%s, %s, 1, 0, 100, 100, 100, 10, 5, 0)
            """, (ctx.author.id, name))
            # Create empty equipment slots
            for slot in ("weapon", "armor", "accessory"):
                cur.execute("""
                    INSERT INTO equipment (user_id, slot, item_id)
                    VALUES (%s, %s, NULL) ON CONFLICT DO NOTHING
                """, (ctx.author.id, slot))
        conn.commit()

    e = discord.Embed(title="⚔️  Character Created!", color=0x2ECC71)
    e.add_field(name="Name",  value=f"**{name}**", inline=True)
    e.add_field(name="Level", value="**1**",        inline=True)
    e.add_field(name="HP",    value="**100**",      inline=True)
    e.add_field(name="ATK",   value="**10**",       inline=True)
    e.add_field(name="DEF",   value="**5**",        inline=True)
    e.add_field(name="Gold",  value="**0**",        inline=True)
    e.set_footer(text="Use -dungeons to see available dungeons  ·  -profile to view your stats")
    e.set_thumbnail(url=ctx.author.display_avatar.url)
    await ctx.send(embed=e)

@bot.command(name="profile", aliases=["p", "stats"])
async def profile_cmd(ctx, member: discord.Member = None):
    """View your profile. -profile [@user]"""
    target = member or ctx.author
    stats = get_total_stats(target.id)
    if not stats:
        noun = "You don't" if target == ctx.author else f"**{target.display_name}** doesn't"
        await ctx.send(f"❌ {noun} have a character yet. Use `-start <name>` to create one."); return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT e.slot, i.name, i.rarity, i.atk_bonus, i.def_bonus, i.hp_bonus
                FROM equipment e
                LEFT JOIN items i ON e.item_id = i.id
                WHERE e.user_id = %s
            """, (target.id,))
            equip = {r["slot"]: r for r in cur.fetchall()}

    exp_pct = round(stats["exp"] / stats["exp_needed"] * 100, 1) if stats["exp_needed"] else 0
    exp_bar = "▰" * int(exp_pct / 10) + "▱" * (10 - int(exp_pct / 10))

    e = discord.Embed(title=f"⚔️  {stats['name']}", color=0x5865F2)
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name="Level", value=f"**{stats['level']}**",            inline=True)
    e.add_field(name="EXP",   value=f"**{fmt_number(stats['exp'])}** / {fmt_number(stats['exp_needed'])}", inline=True)
    e.add_field(name="Gold",  value=f"**{fmt_number(stats['gold'])}** 🪙", inline=True)
    e.add_field(name="HP",    value=f"**{stats['hp']}** / {stats['max_hp']}", inline=True)
    e.add_field(name="ATK",   value=f"**{stats['atk']}**",              inline=True)
    e.add_field(name="DEF",   value=f"**{stats['def']}**",              inline=True)
    e.add_field(name="EXP Progress", value=f"`{exp_bar}` {exp_pct}%",  inline=False)

    equip_lines = []
    for slot in ("weapon", "armor", "accessory"):
        row = equip.get(slot)
        if row and row["name"]:
            emoji = RARITY_EMOJI.get(row["rarity"], "⚪")
            equip_lines.append(f"**{slot.capitalize()}:** {emoji} {row['name']}")
        else:
            equip_lines.append(f"**{slot.capitalize()}:** *empty*")
    e.add_field(name="Equipment", value="\n".join(equip_lines), inline=False)
    e.set_footer(text=f"Use -inv to see your inventory  ·  -dungeons to explore")
    await ctx.send(embed=e)

# ═══════════════════════════════════════════════════════════════════════════════
# INVENTORY & EQUIPMENT COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="inventory", aliases=["inv", "i", "bag"])
async def inventory_cmd(ctx, member: discord.Member = None):
    """View your inventory. -inv [@user]"""
    target = member or ctx.author
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.id, i.name, i.type, i.rarity, i.atk_bonus, i.def_bonus,
                       i.hp_bonus, i.min_level, i.description, inv.quantity
                FROM inventory inv
                JOIN items i ON inv.item_id = i.id
                WHERE inv.user_id = %s
                ORDER BY
                    CASE i.rarity
                        WHEN 'legendary' THEN 1
                        WHEN 'epic'      THEN 2
                        WHEN 'rare'      THEN 3
                        WHEN 'uncommon'  THEN 4
                        ELSE 5
                    END, i.name
            """, (target.id,))
            rows = cur.fetchall()

    if not rows:
        noun = "Your" if target == ctx.author else f"**{target.display_name}'s**"
        await ctx.send(f"{noun} inventory is empty. Complete dungeons to get loot!"); return

    e = discord.Embed(title=f"🎒  {target.display_name}'s Inventory", color=0x9B59B6)
    e.description = f"{len(rows)} item(s)\n\u200b"
    for row in rows[:15]:
        emoji = RARITY_EMOJI.get(row["rarity"], "⚪")
        bonuses = []
        if row["atk_bonus"]: bonuses.append(f"+{row['atk_bonus']} ATK")
        if row["def_bonus"]: bonuses.append(f"+{row['def_bonus']} DEF")
        if row["hp_bonus"]:  bonuses.append(f"+{row['hp_bonus']} HP")
        bonus_str = "  ·  ".join(bonuses) if bonuses else "No stat bonuses"
        e.add_field(
            name=f"{emoji}  {row['name']}  ×{row['quantity']}",
            value=(f"`{row['rarity'].upper()}  ·  {row['type'].upper()}`\n"
                   f"{bonus_str}\n"
                   f"Min level: {row['min_level']}"),
            inline=True
        )
    if len(rows) > 15:
        e.set_footer(text=f"Showing 15 of {len(rows)} items")
    await ctx.send(embed=e)

@bot.command(name="equip")
async def equip_cmd(ctx, *, item_name: str):
    """Equip an item from your inventory. -equip <item name>"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Find item in inventory
            cur.execute("""
                SELECT i.id, i.name, i.type, i.rarity, i.min_level,
                       i.atk_bonus, i.def_bonus, i.hp_bonus
                FROM inventory inv
                JOIN items i ON inv.item_id = i.id
                WHERE inv.user_id = %s AND i.name ILIKE %s AND inv.quantity > 0
                LIMIT 1
            """, (ctx.author.id, f"%{item_name}%"))
            item = cur.fetchone()
            if not item:
                await ctx.send(f"❌ `{item_name}` not found in your inventory."); return

            if item["type"] not in ("weapon", "armor", "accessory"):
                await ctx.send(f"❌ **{item['name']}** cannot be equipped (type: {item['type']})."); return

            # Check level requirement
            cur.execute("SELECT level FROM characters WHERE user_id=%s", (ctx.author.id,))
            char = cur.fetchone()
            if not char:
                await ctx.send("❌ You don't have a character yet. Use `-start <name>`."); return
            if char["level"] < item["min_level"]:
                await ctx.send(f"❌ You need to be level **{item['min_level']}** to equip **{item['name']}**. You are level {char['level']}."); return

            # Unequip current item in that slot → back to inventory
            cur.execute("SELECT item_id FROM equipment WHERE user_id=%s AND slot=%s", (ctx.author.id, item["type"]))
            current = cur.fetchone()
            if current and current["item_id"]:
                cur.execute("""
                    INSERT INTO inventory (user_id, item_id, quantity)
                    VALUES (%s, %s, 1)
                    ON CONFLICT (user_id, item_id) DO UPDATE SET quantity = inventory.quantity + 1
                """, (ctx.author.id, current["item_id"]))

            # Equip new item
            cur.execute("UPDATE equipment SET item_id=%s WHERE user_id=%s AND slot=%s",
                        (item["id"], ctx.author.id, item["type"]))

            # Remove from inventory
            cur.execute("UPDATE inventory SET quantity = quantity - 1 WHERE user_id=%s AND item_id=%s",
                        (ctx.author.id, item["id"]))
            cur.execute("DELETE FROM inventory WHERE user_id=%s AND item_id=%s AND quantity <= 0",
                        (ctx.author.id, item["id"]))
        conn.commit()

    emoji = RARITY_EMOJI.get(item["rarity"], "⚪")
    color = RARITY_COLOR.get(item["rarity"], 0x8B9099)
    e = discord.Embed(title=f"✅  Equipped {item['name']}", color=color)
    e.add_field(name="Slot",   value=item["type"].capitalize(), inline=True)
    e.add_field(name="Rarity", value=f"{emoji} {item['rarity'].capitalize()}", inline=True)
    bonuses = []
    if item["atk_bonus"]: bonuses.append(f"+{item['atk_bonus']} ATK")
    if item["def_bonus"]: bonuses.append(f"+{item['def_bonus']} DEF")
    if item["hp_bonus"]:  bonuses.append(f"+{item['hp_bonus']} HP")
    if bonuses: e.add_field(name="Bonuses", value="  ·  ".join(bonuses), inline=False)
    await ctx.send(embed=e)

@bot.command(name="unequip")
async def unequip_cmd(ctx, slot: str):
    """Unequip a slot. -unequip <weapon|armor|accessory>"""
    slot = slot.lower()
    if slot not in ("weapon", "armor", "accessory"):
        await ctx.send("❌ Slot must be `weapon`, `armor`, or `accessory`."); return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT item_id FROM equipment WHERE user_id=%s AND slot=%s",
                        (ctx.author.id, slot))
            row = cur.fetchone()
            if not row or not row["item_id"]:
                await ctx.send(f"❌ Your **{slot}** slot is already empty."); return
            cur.execute("""
                INSERT INTO inventory (user_id, item_id, quantity) VALUES (%s, %s, 1)
                ON CONFLICT (user_id, item_id) DO UPDATE SET quantity = inventory.quantity + 1
            """, (ctx.author.id, row["item_id"]))
            cur.execute("UPDATE equipment SET item_id=NULL WHERE user_id=%s AND slot=%s",
                        (ctx.author.id, slot))
        conn.commit()
    await ctx.send(f"✅ **{slot.capitalize()}** unequipped and moved to your inventory.")

# ═══════════════════════════════════════════════════════════════════════════════
# DUNGEON COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="dungeons", aliases=["maps", "dungeon"])
async def dungeons_cmd(ctx):
    """Browse all available dungeons. -dungeons"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM dungeons ORDER BY min_level")
            rows = cur.fetchall()

    if not rows:
        await ctx.send("No dungeons have been added yet. Admins can add them with `-adddungeon`."); return

    stats = get_total_stats(ctx.author.id)
    player_level = stats["level"] if stats else 0

    e = discord.Embed(title="🗺️  Dungeons", color=0x5865F2)
    e.description = "Complete dungeons with your party to earn EXP, gold and loot.\n\u200b"
    for row in rows:
        can_enter = player_level >= row["min_level"] if player_level else False
        lock = "🔓" if can_enter else "🔒"
        e.add_field(
            name=f"{lock}  {row['name']}",
            value=(f"Min Level: **{row['min_level']}**\n"
                   f"Waves: **{row['wave_count']} + Boss**\n"
                   f"Rewards: **{row['exp_reward']} EXP**  ·  **{row['gold_reward']} 🪙**\n"
                   f"*{row['description'] or 'No description.'}*"),
            inline=True
        )
    e.set_footer(text="Use -party create <dungeon name> to start a run")
    await ctx.send(embed=e)

# ═══════════════════════════════════════════════════════════════════════════════
# PARTY COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="party")
async def party_cmd(ctx, action: str = None, *, arg: str = None):
    """
    Party system.
    -party create <dungeon name> [open]
    -party invite @user
    -party join <code>
    -party leave
    -party info
    -party start
    -party kick @user
    """
    if not action:
        await ctx.send("Usage: `-party create/invite/join/leave/info/start/kick`"); return
    action = action.lower()

    if action == "create":
        await party_create(ctx, arg)
    elif action == "invite":
        await party_invite(ctx, arg)
    elif action == "join":
        await party_join(ctx, arg)
    elif action == "leave":
        await party_leave(ctx)
    elif action == "info":
        await party_info(ctx)
    elif action == "start":
        await party_start(ctx)
    elif action == "kick":
        await party_kick(ctx, arg)
    else:
        await ctx.send("❌ Unknown action. Use `create`, `invite`, `join`, `leave`, `info`, `start`, or `kick`.")

async def party_create(ctx, arg: str):
    if not arg:
        await ctx.send("❌ Usage: `-party create <dungeon name> [open]`"); return

    is_open = arg.lower().endswith(" open")
    dungeon_name = arg[:-5].strip() if is_open else arg.strip()

    # Check character exists
    stats = get_total_stats(ctx.author.id)
    if not stats:
        await ctx.send("❌ You need a character first. Use `-start <name>`."); return

    # Check not already in a party
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id FROM parties p
                JOIN party_members pm ON p.id = pm.party_id
                WHERE pm.user_id = %s AND p.status = 'waiting'
            """, (ctx.author.id,))
            if cur.fetchone():
                await ctx.send("❌ You're already in a party. Use `-party leave` first."); return

            # Find dungeon
            cur.execute("SELECT * FROM dungeons WHERE name ILIKE %s", (f"%{dungeon_name}%",))
            dungeon = cur.fetchone()
            if not dungeon:
                await ctx.send(f"❌ No dungeon found matching `{dungeon_name}`. Use `-dungeons` to see all."); return

            if stats["level"] < dungeon["min_level"]:
                await ctx.send(f"❌ You need to be level **{dungeon['min_level']}** to enter **{dungeon['name']}**."); return

            code = gen_code()
            cur.execute("""
                INSERT INTO parties (leader_id, dungeon_id, status, is_open, code)
                VALUES (%s, %s, 'waiting', %s, %s) RETURNING id
            """, (ctx.author.id, dungeon["id"], is_open, code))
            party_id = cur.fetchone()["id"]
            cur.execute("INSERT INTO party_members (party_id, user_id) VALUES (%s, %s)",
                        (party_id, ctx.author.id))
        conn.commit()

    e = discord.Embed(title="⚔️  Party Created!", color=0x2ECC71)
    e.add_field(name="Dungeon",  value=f"**{dungeon['name']}**",          inline=True)
    e.add_field(name="Type",     value="🔓 Open" if is_open else "🔒 Private", inline=True)
    e.add_field(name="Code",     value=f"`{code}`",                        inline=True)
    e.add_field(name="Members",  value=f"1 / 4",                           inline=True)
    e.set_footer(text=f"Share the code for others to join with -party join {code}  ·  Use -party start when ready")
    await ctx.send(embed=e)

async def party_invite(ctx, arg: str):
    if not arg:
        await ctx.send("❌ Usage: `-party invite @user`"); return
    # Parse mention
    try:
        target = await commands.MemberConverter().convert(ctx, arg)
    except Exception:
        await ctx.send("❌ Could not find that user."); return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.* FROM parties p
                JOIN party_members pm ON p.id = pm.party_id
                WHERE pm.user_id = %s AND p.status = 'waiting'
            """, (ctx.author.id,))
            party = cur.fetchone()
            if not party:
                await ctx.send("❌ You're not in a party. Use `-party create <dungeon>` first."); return
            if party["leader_id"] != ctx.author.id:
                await ctx.send("❌ Only the party leader can invite players."); return

            cur.execute("SELECT COUNT(*) AS cnt FROM party_members WHERE party_id=%s", (party["id"],))
            count = cur.fetchone()["cnt"]
            if count >= 4:
                await ctx.send("❌ Your party is full (4/4)."); return

            cur.execute("SELECT user_id FROM party_members WHERE party_id=%s AND user_id=%s",
                        (party["id"], target.id))
            if cur.fetchone():
                await ctx.send(f"❌ **{target.display_name}** is already in your party."); return

            # Check target not in another party
            cur.execute("""
                SELECT p.id FROM parties p
                JOIN party_members pm ON p.id = pm.party_id
                WHERE pm.user_id = %s AND p.status = 'waiting'
            """, (target.id,))
            if cur.fetchone():
                await ctx.send(f"❌ **{target.display_name}** is already in another party."); return

            cur.execute("INSERT INTO party_members (party_id, user_id) VALUES (%s, %s)",
                        (party["id"], target.id))
        conn.commit()

    await ctx.send(f"✅ **{target.display_name}** has been added to the party! Use `-party info` to see the party.")

async def party_join(ctx, code: str):
    if not code:
        await ctx.send("❌ Usage: `-party join <code>`"); return
    code = code.upper().strip()

    stats = get_total_stats(ctx.author.id)
    if not stats:
        await ctx.send("❌ You need a character first. Use `-start <name>`."); return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.*, d.min_level, d.name AS dungeon_name
                FROM parties p
                JOIN dungeons d ON p.dungeon_id = d.id
                WHERE p.code = %s AND p.status = 'waiting'
            """, (code,))
            party = cur.fetchone()
            if not party:
                await ctx.send(f"❌ No open party found with code `{code}`."); return
            if not party["is_open"]:
                await ctx.send("❌ This party is private. Ask the leader to invite you."); return

            if stats["level"] < party["min_level"]:
                await ctx.send(f"❌ You need level **{party['min_level']}** to enter **{party['dungeon_name']}**."); return

            cur.execute("SELECT COUNT(*) AS cnt FROM party_members WHERE party_id=%s", (party["id"],))
            if cur.fetchone()["cnt"] >= 4:
                await ctx.send("❌ That party is full (4/4)."); return

            cur.execute("SELECT user_id FROM party_members WHERE party_id=%s AND user_id=%s",
                        (party["id"], ctx.author.id))
            if cur.fetchone():
                await ctx.send("❌ You're already in this party."); return

            cur.execute("""
                SELECT p.id FROM parties p
                JOIN party_members pm ON p.id = pm.party_id
                WHERE pm.user_id = %s AND p.status = 'waiting'
            """, (ctx.author.id,))
            if cur.fetchone():
                await ctx.send("❌ You're already in a party. Use `-party leave` first."); return

            cur.execute("INSERT INTO party_members (party_id, user_id) VALUES (%s, %s)",
                        (party["id"], ctx.author.id))
        conn.commit()

    await ctx.send(f"✅ Joined party for **{party['dungeon_name']}**! Use `-party info` to see your party.")

async def party_leave(ctx):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.* FROM parties p
                JOIN party_members pm ON p.id = pm.party_id
                WHERE pm.user_id = %s AND p.status = 'waiting'
            """, (ctx.author.id,))
            party = cur.fetchone()
            if not party:
                await ctx.send("❌ You're not in any party."); return

            cur.execute("DELETE FROM party_members WHERE party_id=%s AND user_id=%s",
                        (party["id"], ctx.author.id))

            if party["leader_id"] == ctx.author.id:
                # Transfer leadership or disband
                cur.execute("SELECT user_id FROM party_members WHERE party_id=%s LIMIT 1", (party["id"],))
                next_member = cur.fetchone()
                if next_member:
                    cur.execute("UPDATE parties SET leader_id=%s WHERE id=%s",
                                (next_member["user_id"], party["id"]))
                else:
                    cur.execute("UPDATE parties SET status='disbanded' WHERE id=%s", (party["id"],))
        conn.commit()
    await ctx.send("✅ You have left the party.")

async def party_info(ctx):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.*, d.name AS dungeon_name, d.min_level, d.wave_count
                FROM parties p
                JOIN party_members pm ON p.id = pm.party_id
                JOIN dungeons d ON p.dungeon_id = d.id
                WHERE pm.user_id = %s AND p.status = 'waiting'
            """, (ctx.author.id,))
            party = cur.fetchone()
            if not party:
                await ctx.send("❌ You're not in any party."); return
            cur.execute("SELECT user_id FROM party_members WHERE party_id=%s", (party["id"],))
            members = cur.fetchall()

    e = discord.Embed(title="⚔️  Party Info", color=0x5865F2)
    e.add_field(name="Dungeon", value=f"**{party['dungeon_name']}**", inline=True)
    e.add_field(name="Status",  value=party["status"].capitalize(),   inline=True)
    e.add_field(name="Code",    value=f"`{party['code']}`",           inline=True)
    e.add_field(name="Type",    value="🔓 Open" if party["is_open"] else "🔒 Private", inline=True)

    member_lines = []
    for m in members:
        user = ctx.guild.get_member(m["user_id"])
        name = user.display_name if user else f"User {m['user_id']}"
        crown = " 👑" if m["user_id"] == party["leader_id"] else ""
        stats = get_total_stats(m["user_id"])
        lvl = stats["level"] if stats else "?"
        member_lines.append(f"**{name}**{crown}  ·  Lv.{lvl}")
    e.add_field(name=f"Members ({len(members)}/4)", value="\n".join(member_lines), inline=False)
    e.set_footer(text="Leader uses -party start to begin the dungeon run")
    await ctx.send(embed=e)

async def party_start(ctx):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.*, d.name AS dungeon_name, d.wave_count,
                       d.exp_reward, d.gold_reward
                FROM parties p
                JOIN party_members pm ON p.id = pm.party_id
                JOIN dungeons d ON p.dungeon_id = d.id
                WHERE pm.user_id = %s AND p.status = 'waiting'
            """, (ctx.author.id,))
            party = cur.fetchone()
            if not party:
                await ctx.send("❌ You're not in any party."); return
            if party["leader_id"] != ctx.author.id:
                await ctx.send("❌ Only the party leader can start the run."); return

            cur.execute("SELECT user_id FROM party_members WHERE party_id=%s", (party["id"],))
            members = [r["user_id"] for r in cur.fetchall()]

            # Create run
            cur.execute("""
                INSERT INTO dungeon_runs (party_id, dungeon_id, status, current_wave)
                VALUES (%s, %s, 'in_progress', 1) RETURNING id
            """, (party["id"], party["dungeon_id"]])
            run_id = cur.fetchone()["id"]
            cur.execute("UPDATE parties SET status='in_progress' WHERE id=%s", (party["id"],))
        conn.commit()

    # Run the dungeon
    await run_dungeon(ctx, party, members, run_id)

async def party_kick(ctx, arg: str):
    if not arg:
        await ctx.send("❌ Usage: `-party kick @user`"); return
    try:
        target = await commands.MemberConverter().convert(ctx, arg)
    except Exception:
        await ctx.send("❌ Could not find that user."); return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.* FROM parties p
                JOIN party_members pm ON p.id = pm.party_id
                WHERE pm.user_id = %s AND p.status = 'waiting'
            """, (ctx.author.id,))
            party = cur.fetchone()
            if not party:
                await ctx.send("❌ You're not in a party."); return
            if party["leader_id"] != ctx.author.id:
                await ctx.send("❌ Only the leader can kick members."); return
            if target.id == ctx.author.id:
                await ctx.send("❌ You can't kick yourself. Use `-party leave`."); return
            cur.execute("DELETE FROM party_members WHERE party_id=%s AND user_id=%s",
                        (party["id"], target.id))
        conn.commit()
    await ctx.send(f"✅ **{target.display_name}** has been removed from the party.")

# ═══════════════════════════════════════════════════════════════════════════════
# COMBAT ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

async def run_dungeon(ctx, party, member_ids: list, run_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM dungeons WHERE id=%s", (party["dungeon_id"],))
            dungeon = cur.fetchone()
            cur.execute("""
                SELECT * FROM enemies WHERE dungeon_id=%s ORDER BY wave
            """, (party["dungeon_id"],))
            all_enemies = cur.fetchall()

    # Group enemies by wave
    waves = {}
    for e in all_enemies:
        waves.setdefault(e["wave"], []).append(e)

    total_waves = dungeon["wave_count"]
    member_stats = {uid: get_total_stats(uid) for uid in member_ids}
    damage_totals = {uid: 0 for uid in member_ids}

    # ── Wave loop ──────────────────────────────────────────────────────────────
    for wave_num in range(1, total_waves + 1):
        wave_enemies = waves.get(wave_num, [])
        is_boss = (wave_num == total_waves)

        if not wave_enemies:
            continue

        title = f"👹  **BOSS WAVE**" if is_boss else f"⚔️  Wave {wave_num} / {total_waves}"
        enemy_lines = [f"• {e['name']} (HP: {e['hp']}  ATK: {e['atk']}  DEF: {e['def']})  ×{e['count']}"
                       for e in wave_enemies]

        e = discord.Embed(title=title, color=0xFF6B6B if is_boss else 0xF4A23C)
        e.add_field(name="Enemies", value="\n".join(enemy_lines), inline=False)
        await ctx.send(embed=e)
        await asyncio.sleep(2)

        # Each player attacks all enemies simultaneously
        total_enemy_hp = sum(en["hp"] * en["count"] for en in wave_enemies)
        total_enemy_atk = sum(en["atk"] * en["count"] for en in wave_enemies)
        total_enemy_def = max(en["def"] for en in wave_enemies)

        result_lines = []
        for uid in member_ids:
            s = member_stats.get(uid)
            if not s:
                continue
            # Damage dealt: ATK vs enemy DEF, with some variance
            raw_dmg = max(1, s["atk"] - total_enemy_def + random.randint(-2, 5))
            raw_dmg = int(raw_dmg * random.uniform(0.9, 1.2))
            damage_totals[uid] += raw_dmg
            user = ctx.guild.get_member(uid)
            name = user.display_name if user else f"User {uid}"
            result_lines.append(f"**{name}** dealt **{fmt_number(raw_dmg)}** damage")

        # Enemy damage to party (split across all members)
        dmg_per_member = max(1, (total_enemy_atk - sum(member_stats[uid]["def"] for uid in member_ids if member_ids)) // max(len(member_ids), 1))

        e2 = discord.Embed(title=f"✅  Wave {wave_num} Cleared!" if not is_boss else "🎉  BOSS DEFEATED!",
                           color=0x2ECC71)
        e2.add_field(name="Damage Dealt", value="\n".join(result_lines), inline=False)
        e2.add_field(name="Party took", value=f"~{dmg_per_member} damage each", inline=False)
        await ctx.send(embed=e2)
        await asyncio.sleep(1.5)

    # ── Loot & Rewards ─────────────────────────────────────────────────────────
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT lt.drop_chance, i.*
                FROM loot_table lt
                JOIN items i ON lt.item_id = i.id
                WHERE lt.dungeon_id = %s
            """, (party["dungeon_id"],))
            loot_pool = cur.fetchall()

    reward_embed = discord.Embed(title="🎁  Run Complete — Rewards", color=0xF1C40F)
    reward_embed.description = f"**{dungeon['name']}** cleared!\n\u200b"

    for uid in member_ids:
        user = ctx.guild.get_member(uid)
        name = user.display_name if user else f"User {uid}"

        # EXP and gold (scaled slightly by damage contribution)
        total_damage = sum(damage_totals.values()) or 1
        contribution = damage_totals[uid] / total_damage
        exp_earned  = int(dungeon["exp_reward"] * (0.7 + 0.3 * contribution * len(member_ids)))
        gold_earned = int(dungeon["gold_reward"] * (0.7 + 0.3 * contribution * len(member_ids)))

        # Random loot drops
        items_earned = []
        for loot in loot_pool:
            if random.random() < loot["drop_chance"]:
                items_earned.append(loot)

        # Save to DB
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO run_results (run_id, user_id, damage_dealt, exp_earned, gold_earned, items_earned)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (run_id, uid, damage_totals[uid], exp_earned, gold_earned,
                      json.dumps([i["id"] for i in items_earned])))

                # Award EXP + gold
                cur.execute("""
                    UPDATE characters SET gold = gold + %s WHERE user_id = %s
                """, (gold_earned, uid))

                # Level up check
                cur.execute("SELECT level, exp, exp_needed FROM characters WHERE user_id=%s", (uid,))
                char = cur.fetchone()
                new_exp = char["exp"] + exp_earned
                new_level = char["level"]
                leveled_up = False
                while new_exp >= exp_for_level(new_level):
                    new_exp -= exp_for_level(new_level)
                    new_level += 1
                    leveled_up = True

                # Stat increases on level up
                new_hp  = 100 + (new_level - 1) * 15
                new_atk = 10  + (new_level - 1) * 3
                new_def = 5   + (new_level - 1) * 2

                cur.execute("""
                    UPDATE characters
                    SET exp=%s, level=%s, exp_needed=%s,
                        max_hp=%s, hp=%s, atk=%s, def=%s
                    WHERE user_id=%s
                """, (new_exp, new_level, exp_for_level(new_level),
                      new_hp, new_hp, new_atk, new_def, uid))

                # Add loot to inventory
                for item in items_earned:
                    cur.execute("""
                        INSERT INTO inventory (user_id, item_id, quantity)
                        VALUES (%s, %s, 1)
                        ON CONFLICT (user_id, item_id) DO UPDATE SET quantity = inventory.quantity + 1
                    """, (uid, item["id"]))
            conn.commit()

        loot_str = "  ·  ".join(
            f"{RARITY_EMOJI.get(i['rarity'], '⚪')} {i['name']}" for i in items_earned
        ) or "*No drops*"

        level_str = f"  🎉 **LEVEL UP → {new_level}!**" if leveled_up else ""
        reward_embed.add_field(
            name=f"🧙 {name}{level_str}",
            value=(f"**+{exp_earned} EXP**  ·  **+{gold_earned} 🪙**\n"
                   f"Loot: {loot_str}"),
            inline=False
        )

    # Mark run complete
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE dungeon_runs SET status='completed', finished_at=NOW() WHERE id=%s", (run_id,))
            cur.execute("UPDATE parties SET status='completed' WHERE id=%s", (party["id"],))
        conn.commit()

    await ctx.send(embed=reward_embed)

# ═══════════════════════════════════════════════════════════════════════════════
# SHOP COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="shop")
async def shop_cmd(ctx):
    """Browse the shop. -shop"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.price, s.stock, i.*
                FROM shop s
                JOIN items i ON s.item_id = i.id
                ORDER BY
                    CASE i.rarity
                        WHEN 'legendary' THEN 1
                        WHEN 'epic'      THEN 2
                        WHEN 'rare'      THEN 3
                        WHEN 'uncommon'  THEN 4
                        ELSE 5
                    END
            """)
            rows = cur.fetchall()

    if not rows:
        await ctx.send("The shop is empty. Admins can add items with `-addshopitem`."); return

    e = discord.Embed(title="🛒  Shop", color=0xF4D03F)
    e.description = "Use `-buy <item name>` to purchase.\n\u200b"
    for row in rows:
        emoji = RARITY_EMOJI.get(row["rarity"], "⚪")
        stock_str = "∞" if row["stock"] == -1 else str(row["stock"])
        bonuses = []
        if row["atk_bonus"]: bonuses.append(f"+{row['atk_bonus']} ATK")
        if row["def_bonus"]: bonuses.append(f"+{row['def_bonus']} DEF")
        if row["hp_bonus"]:  bonuses.append(f"+{row['hp_bonus']} HP")
        e.add_field(
            name=f"{emoji}  {row['name']}",
            value=(f"`{row['rarity'].upper()}  ·  {row['type'].upper()}`\n"
                   f"{'  ·  '.join(bonuses) or 'No bonuses'}\n"
                   f"**{fmt_number(row['price'])} 🪙**  ·  Stock: {stock_str}"),
            inline=True
        )
    await ctx.send(embed=e)

@bot.command(name="buy")
async def buy_cmd(ctx, *, item_name: str):
    """Buy an item from the shop. -buy <item name>"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id AS shop_id, s.price, s.stock, i.*
                FROM shop s
                JOIN items i ON s.item_id = i.id
                WHERE i.name ILIKE %s
                LIMIT 1
            """, (f"%{item_name}%",))
            row = cur.fetchone()
            if not row:
                await ctx.send(f"❌ `{item_name}` not found in the shop."); return
            if row["stock"] == 0:
                await ctx.send(f"❌ **{row['name']}** is out of stock."); return

            cur.execute("SELECT gold FROM characters WHERE user_id=%s", (ctx.author.id,))
            char = cur.fetchone()
            if not char:
                await ctx.send("❌ You need a character first."); return
            if char["gold"] < row["price"]:
                await ctx.send(f"❌ Not enough gold. Need **{fmt_number(row['price'])} 🪙**, have **{fmt_number(char['gold'])} 🪙**."); return

            cur.execute("UPDATE characters SET gold = gold - %s WHERE user_id=%s", (row["price"], ctx.author.id))
            if row["stock"] > 0:
                cur.execute("UPDATE shop SET stock = stock - 1 WHERE id=%s", (row["shop_id"],))
            cur.execute("""
                INSERT INTO inventory (user_id, item_id, quantity) VALUES (%s, %s, 1)
                ON CONFLICT (user_id, item_id) DO UPDATE SET quantity = inventory.quantity + 1
            """, (ctx.author.id, row["id"]))
        conn.commit()

    emoji = RARITY_EMOJI.get(row["rarity"], "⚪")
    e = discord.Embed(title=f"✅  Purchased: {row['name']}", color=RARITY_COLOR.get(row["rarity"], 0x8B9099))
    e.add_field(name="Paid",   value=f"**{fmt_number(row['price'])} 🪙**",        inline=True)
    e.add_field(name="Rarity", value=f"{emoji} {row['rarity'].capitalize()}",      inline=True)
    e.set_footer(text="Use -equip <item name> to equip it")
    await ctx.send(embed=e)

@bot.command(name="sell")
async def sell_cmd(ctx, *, item_name: str):
    """Sell an item from your inventory. -sell <item name>"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.id, i.name, i.rarity, i.gold_value, inv.quantity
                FROM inventory inv
                JOIN items i ON inv.item_id = i.id
                WHERE inv.user_id = %s AND i.name ILIKE %s AND inv.quantity > 0
                LIMIT 1
            """, (ctx.author.id, f"%{item_name}%"))
            row = cur.fetchone()
            if not row:
                await ctx.send(f"❌ `{item_name}` not found in your inventory."); return

            sell_price = max(1, row["gold_value"] // 2)
            cur.execute("UPDATE characters SET gold = gold + %s WHERE user_id=%s", (sell_price, ctx.author.id))
            cur.execute("UPDATE inventory SET quantity = quantity - 1 WHERE user_id=%s AND item_id=%s",
                        (ctx.author.id, row["id"]))
            cur.execute("DELETE FROM inventory WHERE user_id=%s AND item_id=%s AND quantity <= 0",
                        (ctx.author.id, row["id"]))
        conn.commit()

    await ctx.send(f"✅ Sold **{row['name']}** for **{fmt_number(sell_price)} 🪙**.")

# ═══════════════════════════════════════════════════════════════════════════════
# TRADING COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="trade")
async def trade_cmd(ctx, action: str = None, *, arg: str = None):
    """
    Trading system.
    -trade list <item name> <price> [@user]
    -trade buy <trade id>
    -trade cancel <trade id>
    -trade listings
    -trade mylistings
    """
    if not action:
        await ctx.send("Usage: `-trade list/buy/cancel/listings/mylistings`"); return
    action = action.lower()
    if action == "list":       await trade_list(ctx, arg)
    elif action == "buy":      await trade_buy(ctx, arg)
    elif action == "cancel":   await trade_cancel(ctx, arg)
    elif action == "listings": await trade_listings(ctx)
    elif action == "mylistings": await trade_mylistings(ctx)
    else:
        await ctx.send("❌ Unknown action. Use `list`, `buy`, `cancel`, `listings`, or `mylistings`.")

async def trade_list(ctx, arg: str):
    if not arg:
        await ctx.send("❌ Usage: `-trade list <item name> <price> [@user]`"); return
    parts = arg.split()
    # Parse: last part might be @mention, second-to-last is price
    buyer_id = None
    try:
        maybe_mention = parts[-1]
        target = await commands.MemberConverter().convert(ctx, maybe_mention)
        buyer_id = target.id
        parts = parts[:-1]
    except Exception:
        pass
    if not parts:
        await ctx.send("❌ Usage: `-trade list <item name> <price> [@user]`"); return
    try:
        price = int(parts[-1])
        item_name = " ".join(parts[:-1])
    except ValueError:
        await ctx.send("❌ Price must be a number."); return
    if price < 1:
        await ctx.send("❌ Price must be at least 1."); return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.id, i.name, i.rarity, inv.quantity
                FROM inventory inv
                JOIN items i ON inv.item_id = i.id
                WHERE inv.user_id=%s AND i.name ILIKE %s AND inv.quantity > 0
                LIMIT 1
            """, (ctx.author.id, f"%{item_name}%"))
            item = cur.fetchone()
            if not item:
                await ctx.send(f"❌ `{item_name}` not found in your inventory."); return

            tax_rate = TRADE_TAX.get(item["rarity"], 0.05)
            tax_amt  = int(price * tax_rate)

            cur.execute("""
                INSERT INTO trades (seller_id, buyer_id, item_id, quantity, price, tax_rate)
                VALUES (%s, %s, %s, 1, %s, %s) RETURNING id
            """, (ctx.author.id, buyer_id, item["id"], price, tax_rate))
            trade_id = cur.fetchone()["id"]
            # Remove from inventory
            cur.execute("UPDATE inventory SET quantity = quantity - 1 WHERE user_id=%s AND item_id=%s",
                        (ctx.author.id, item["id"]))
            cur.execute("DELETE FROM inventory WHERE user_id=%s AND item_id=%s AND quantity <= 0",
                        (ctx.author.id, item["id"]))
        conn.commit()

    emoji = RARITY_EMOJI.get(item["rarity"], "⚪")
    e = discord.Embed(title="📋  Trade Listed", color=0x2ECC71)
    e.add_field(name="Item",    value=f"{emoji} **{item['name']}**",        inline=True)
    e.add_field(name="Price",   value=f"**{fmt_number(price)} 🪙**",        inline=True)
    e.add_field(name="Tax",     value=f"{int(tax_rate*100)}%  ({fmt_number(tax_amt)} 🪙)", inline=True)
    e.add_field(name="You receive", value=f"**{fmt_number(price - tax_amt)} 🪙**", inline=True)
    e.add_field(name="Trade ID", value=f"`#{trade_id}`",                    inline=True)
    if buyer_id:
        buyer = ctx.guild.get_member(buyer_id)
        e.add_field(name="Restricted to", value=buyer.display_name if buyer else f"User {buyer_id}", inline=True)
    e.set_footer(text="Use -trade cancel <id> to cancel the listing")
    await ctx.send(embed=e)

async def trade_buy(ctx, arg: str):
    if not arg:
        await ctx.send("❌ Usage: `-trade buy <trade id>`"); return
    try:
        trade_id = int(arg.strip("#"))
    except ValueError:
        await ctx.send("❌ Trade ID must be a number."); return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.*, i.name AS item_name, i.rarity, i.min_level
                FROM trades t
                JOIN items i ON t.item_id = i.id
                WHERE t.id=%s AND t.status='open'
            """, (trade_id,))
            trade = cur.fetchone()
            if not trade:
                await ctx.send(f"❌ Trade `#{trade_id}` not found or already completed."); return
            if trade["seller_id"] == ctx.author.id:
                await ctx.send("❌ You can't buy your own listing."); return
            if trade["buyer_id"] and trade["buyer_id"] != ctx.author.id:
                await ctx.send("❌ This trade is reserved for another player."); return

            # Level check
            cur.execute("SELECT level, gold FROM characters WHERE user_id=%s", (ctx.author.id,))
            char = cur.fetchone()
            if not char:
                await ctx.send("❌ You need a character first."); return
            if char["level"] < trade["min_level"]:
                await ctx.send(f"❌ You need level **{trade['min_level']}** to use **{trade['item_name']}**."); return
            if char["gold"] < trade["price"]:
                await ctx.send(f"❌ Not enough gold. Need **{fmt_number(trade['price'])} 🪙**."); return

            tax_amt    = int(trade["price"] * trade["tax_rate"])
            seller_gets = trade["price"] - tax_amt

            cur.execute("UPDATE characters SET gold = gold - %s WHERE user_id=%s", (trade["price"], ctx.author.id))
            cur.execute("UPDATE characters SET gold = gold + %s WHERE user_id=%s", (seller_gets, trade["seller_id"]))
            cur.execute("""
                INSERT INTO inventory (user_id, item_id, quantity) VALUES (%s, %s, 1)
                ON CONFLICT (user_id, item_id) DO UPDATE SET quantity = inventory.quantity + 1
            """, (ctx.author.id, trade["item_id"]))
            cur.execute("UPDATE trades SET status='completed', buyer_id=%s, completed_at=NOW() WHERE id=%s",
                        (ctx.author.id, trade_id))
        conn.commit()

    emoji = RARITY_EMOJI.get(trade["rarity"], "⚪")
    e = discord.Embed(title="✅  Trade Complete!", color=0x2ECC71)
    e.add_field(name="Item",    value=f"{emoji} **{trade['item_name']}**",   inline=True)
    e.add_field(name="Paid",    value=f"**{fmt_number(trade['price'])} 🪙**", inline=True)
    e.add_field(name="Tax",     value=f"**{fmt_number(tax_amt)} 🪙** ({int(trade['tax_rate']*100)}%)", inline=True)
    await ctx.send(embed=e)

async def trade_cancel(ctx, arg: str):
    if not arg:
        await ctx.send("❌ Usage: `-trade cancel <trade id>`"); return
    try:
        trade_id = int(arg.strip("#"))
    except ValueError:
        await ctx.send("❌ Trade ID must be a number."); return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM trades WHERE id=%s AND status='open'", (trade_id,))
            trade = cur.fetchone()
            if not trade:
                await ctx.send(f"❌ Trade `#{trade_id}` not found or already completed."); return
            if trade["seller_id"] != ctx.author.id and not is_admin(ctx.author):
                await ctx.send("❌ You can only cancel your own listings."); return
            # Return item to seller
            cur.execute("""
                INSERT INTO inventory (user_id, item_id, quantity) VALUES (%s, %s, 1)
                ON CONFLICT (user_id, item_id) DO UPDATE SET quantity = inventory.quantity + 1
            """, (trade["seller_id"], trade["item_id"]))
            cur.execute("UPDATE trades SET status='cancelled' WHERE id=%s", (trade_id,))
        conn.commit()
    await ctx.send(f"✅ Trade `#{trade_id}` cancelled. Item returned to your inventory.")

async def trade_listings(ctx):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.price, t.tax_rate, t.seller_id, t.buyer_id,
                       i.name AS item_name, i.rarity, i.min_level
                FROM trades t
                JOIN items i ON t.item_id = i.id
                WHERE t.status='open' AND (t.buyer_id IS NULL OR t.buyer_id=%s)
                ORDER BY t.listed_at DESC LIMIT 20
            """, (ctx.author.id,))
            rows = cur.fetchall()

    if not rows:
        await ctx.send("No open trade listings right now."); return

    e = discord.Embed(title="📋  Trade Listings", color=0x5865F2)
    e.description = f"{len(rows)} listing(s)\n\u200b"
    for row in rows:
        seller = ctx.guild.get_member(row["seller_id"])
        seller_name = seller.display_name if seller else f"User {row['seller_id']}"
        emoji = RARITY_EMOJI.get(row["rarity"], "⚪")
        tax = int(row["price"] * row["tax_rate"])
        e.add_field(
            name=f"`#{row['id']}`  {emoji}  {row['item_name']}",
            value=(f"**{fmt_number(row['price'])} 🪙**  ·  Tax: {fmt_number(tax)} 🪙\n"
                   f"Seller: {seller_name}  ·  Min Lv: {row['min_level']}"),
            inline=False
        )
    e.set_footer(text="Use -trade buy <id> to purchase")
    await ctx.send(embed=e)

async def trade_mylistings(ctx):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.price, t.status, i.name AS item_name, i.rarity
                FROM trades t
                JOIN items i ON t.item_id = i.id
                WHERE t.seller_id=%s
                ORDER BY t.listed_at DESC LIMIT 10
            """, (ctx.author.id,))
            rows = cur.fetchall()

    if not rows:
        await ctx.send("You have no trade listings."); return

    e = discord.Embed(title="📋  My Trade Listings", color=0x5865F2)
    for row in rows:
        emoji = RARITY_EMOJI.get(row["rarity"], "⚪")
        e.add_field(
            name=f"`#{row['id']}`  {emoji}  {row['item_name']}",
            value=f"**{fmt_number(row['price'])} 🪙**  ·  Status: `{row['status']}`",
            inline=False
        )
    e.set_footer(text="Use -trade cancel <id> to cancel an open listing")
    await ctx.send(embed=e)

# ═══════════════════════════════════════════════════════════════════════════════
# CRAFTING COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="recipes", aliases=["craft"])
async def recipes_cmd(ctx):
    """View all crafting recipes. -recipes"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.id, r.gold_cost, r.min_level, r.description,
                       i.name AS result_name, i.rarity AS result_rarity
                FROM recipes r
                JOIN items i ON r.result_item_id = i.id
                ORDER BY r.min_level
            """)
            rows = cur.fetchall()

    if not rows:
        await ctx.send("No crafting recipes available yet."); return

    e = discord.Embed(title="⚒️  Crafting Recipes", color=0xE67E22)
    e.description = "Use `-craftitem <recipe id>` to craft.\n\u200b"
    for row in rows:
        emoji = RARITY_EMOJI.get(row["result_rarity"], "⚪")
        e.add_field(
            name=f"`#{row['id']}`  {emoji}  {row['result_name']}",
            value=(f"`{row['result_rarity'].upper()}`  ·  Min Lv: {row['min_level']}\n"
                   f"Cost: **{fmt_number(row['gold_cost'])} 🪙**\n"
                   f"*{row['description'] or 'No description'}*"),
            inline=False
        )
    await ctx.send(embed=e)

@bot.command(name="craftitem")
async def craftitem_cmd(ctx, recipe_id: int = None):
    """Craft an item. -craftitem <recipe id>"""
    if not recipe_id:
        await ctx.send("❌ Usage: `-craftitem <recipe id>`  ·  Use `-recipes` to see all recipes."); return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.*, i.name AS result_name, i.rarity AS result_rarity, i.id AS result_item_id
                FROM recipes r
                JOIN items i ON r.result_item_id = i.id
                WHERE r.id=%s
            """, (recipe_id,))
            recipe = cur.fetchone()
            if not recipe:
                await ctx.send(f"❌ Recipe `#{recipe_id}` not found."); return

            cur.execute("SELECT level, gold FROM characters WHERE user_id=%s", (ctx.author.id,))
            char = cur.fetchone()
            if not char:
                await ctx.send("❌ You need a character first."); return
            if char["level"] < recipe["min_level"]:
                await ctx.send(f"❌ You need level **{recipe['min_level']}** to craft this."); return
            if char["gold"] < recipe["gold_cost"]:
                await ctx.send(f"❌ Not enough gold. Need **{fmt_number(recipe['gold_cost'])} 🪙**."); return

            ingredients = recipe["ingredients"]
            # Check all ingredients are in inventory
            missing = []
            for ing in ingredients:
                cur.execute("SELECT quantity FROM inventory WHERE user_id=%s AND item_id=%s",
                            (ctx.author.id, ing["item_id"]))
                row = cur.fetchone()
                if not row or row["quantity"] < ing["qty"]:
                    cur.execute("SELECT name FROM items WHERE id=%s", (ing["item_id"],))
                    item_row = cur.fetchone()
                    have = row["quantity"] if row else 0
                    missing.append(f"{item_row['name'] if item_row else ing['item_id']}: need {ing['qty']}, have {have}")

            if missing:
                await ctx.send("❌ Missing ingredients:\n" + "\n".join(f"• {m}" for m in missing)); return

            # Deduct ingredients
            for ing in ingredients:
                cur.execute("UPDATE inventory SET quantity = quantity - %s WHERE user_id=%s AND item_id=%s",
                            (ing["qty"], ctx.author.id, ing["item_id"]))
                cur.execute("DELETE FROM inventory WHERE user_id=%s AND item_id=%s AND quantity <= 0",
                            (ctx.author.id, ing["item_id"]))

            # Deduct gold
            cur.execute("UPDATE characters SET gold = gold - %s WHERE user_id=%s",
                        (recipe["gold_cost"], ctx.author.id))

            # Add crafted item
            cur.execute("""
                INSERT INTO inventory (user_id, item_id, quantity) VALUES (%s, %s, 1)
                ON CONFLICT (user_id, item_id) DO UPDATE SET quantity = inventory.quantity + 1
            """, (ctx.author.id, recipe["result_item_id"]))
        conn.commit()

    emoji = RARITY_EMOJI.get(recipe["result_rarity"], "⚪")
    e = discord.Embed(title=f"⚒️  Crafted: {recipe['result_name']}", color=RARITY_COLOR.get(recipe["result_rarity"], 0x8B9099))
    e.add_field(name="Result", value=f"{emoji} **{recipe['result_name']}**", inline=True)
    e.add_field(name="Gold spent", value=f"**{fmt_number(recipe['gold_cost'])} 🪙**", inline=True)
    await ctx.send(embed=e)

# ═══════════════════════════════════════════════════════════════════════════════
# LEADERBOARD
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="leaderboard", aliases=["lb", "top"])
async def leaderboard_cmd(ctx, lb_type: str = "level"):
    """View leaderboards. -lb [level|gold]"""
    lb_type = lb_type.lower()
    medals = ["🥇", "🥈", "🥉"] + [f"`{n}`" for n in range(4, 11)]

    with get_conn() as conn:
        with conn.cursor() as cur:
            if lb_type == "gold":
                cur.execute("SELECT user_id, name, gold, level FROM characters ORDER BY gold DESC LIMIT 10")
                title = "💰  Richest Players"
            else:
                cur.execute("SELECT user_id, name, level, exp FROM characters ORDER BY level DESC, exp DESC LIMIT 10")
                title = "⚔️  Top Players by Level"
            rows = cur.fetchall()

    e = discord.Embed(title=title, color=0xF1C40F)
    e.description = "\u200b"
    for i, row in enumerate(rows):
        member = ctx.guild.get_member(row["user_id"])
        display = member.display_name if member else row["name"]
        if lb_type == "gold":
            e.add_field(name=f"{medals[i]}  {display}",
                        value=f"**{fmt_number(row['gold'])} 🪙**  ·  Lv.{row['level']}", inline=False)
        else:
            e.add_field(name=f"{medals[i]}  {display}",
                        value=f"Level **{row['level']}**  ·  {fmt_number(row['exp'])} EXP", inline=False)
    e.set_footer(text="-lb level  ·  -lb gold")
    await ctx.send(embed=e)

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="adddungeon")
async def adddungeon_cmd(ctx, min_level: int, exp_reward: int, gold_reward: int,
                          wave_count: int = 5, *, name: str):
    """[Admin] Add a dungeon. -adddungeon <min_level> <exp> <gold> [waves] <name>"""
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO dungeons (name, min_level, wave_count, exp_reward, gold_reward)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    min_level=%s, wave_count=%s, exp_reward=%s, gold_reward=%s
            """, (name, min_level, wave_count, exp_reward, gold_reward,
                  min_level, wave_count, exp_reward, gold_reward))
        conn.commit()
    await ctx.send(f"✅ Dungeon **{name}** added/updated.")

@bot.command(name="addenemy")
async def addenemy_cmd(ctx, dungeon_name: str, wave: int, hp: int, atk: int,
                        def_: int, exp: int, gold: int, count: int = 1, *, name: str):
    """[Admin] Add an enemy to a dungeon. wave=0 means boss."""
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM dungeons WHERE name ILIKE %s", (f"%{dungeon_name}%",))
            dungeon = cur.fetchone()
            if not dungeon:
                await ctx.send(f"❌ Dungeon `{dungeon_name}` not found."); return
            cur.execute("""
                INSERT INTO enemies (dungeon_id, name, wave, count, hp, atk, def, exp_reward, gold_reward)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (dungeon["id"], name, wave, count, hp, atk, def_, exp, gold))
        conn.commit()
    wave_str = "**BOSS**" if wave == 0 else f"wave {wave}"
    await ctx.send(f"✅ Enemy **{name}** added to **{dungeon_name}** ({wave_str}).")

@bot.command(name="additem")
async def additem_cmd(ctx, type_: str, rarity: str, min_level: int,
                       atk: int, def_: int, hp: int, gold_value: int, *, name: str):
    """[Admin] Add an item. -additem <type> <rarity> <min_lv> <atk> <def> <hp> <gold_value> <name>"""
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    if type_ not in ("weapon", "armor", "accessory", "material", "consumable"):
        await ctx.send("❌ Type: weapon / armor / accessory / material / consumable"); return
    if rarity not in ("common", "uncommon", "rare", "epic", "legendary"):
        await ctx.send("❌ Rarity: common / uncommon / rare / epic / legendary"); return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO items (name, type, rarity, min_level, atk_bonus, def_bonus, hp_bonus, gold_value)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    type=%s, rarity=%s, min_level=%s,
                    atk_bonus=%s, def_bonus=%s, hp_bonus=%s, gold_value=%s
            """, (name, type_, rarity, min_level, atk, def_, hp, gold_value,
                  type_, rarity, min_level, atk, def_, hp, gold_value))
        conn.commit()
    emoji = RARITY_EMOJI.get(rarity, "⚪")
    await ctx.send(f"✅ Item {emoji} **{name}** added.")

@bot.command(name="addloot")
async def addloot_cmd(ctx, dungeon_name: str, drop_chance: float, *, item_name: str):
    """[Admin] Add loot to a dungeon. -addloot <dungeon> <0.0-1.0> <item name>"""
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM dungeons WHERE name ILIKE %s", (f"%{dungeon_name}%",))
            dungeon = cur.fetchone()
            if not dungeon: await ctx.send("❌ Dungeon not found."); return
            cur.execute("SELECT id FROM items WHERE name ILIKE %s", (f"%{item_name}%",))
            item = cur.fetchone()
            if not item: await ctx.send("❌ Item not found."); return
            cur.execute("""
                INSERT INTO loot_table (dungeon_id, item_id, drop_chance)
                VALUES (%s, %s, %s)
                ON CONFLICT (dungeon_id, item_id) DO UPDATE SET drop_chance=%s
            """, (dungeon["id"], item["id"], drop_chance, drop_chance))
        conn.commit()
    await ctx.send(f"✅ **{item_name}** added to **{dungeon_name}** loot table ({drop_chance*100:.1f}% chance).")

@bot.command(name="addshopitem")
async def addshopitem_cmd(ctx, price: int, stock: int = -1, *, item_name: str):
    """[Admin] Add an item to the shop. -addshopitem <price> [stock] <item name>"""
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM items WHERE name ILIKE %s", (f"%{item_name}%",))
            item = cur.fetchone()
            if not item: await ctx.send("❌ Item not found."); return
            cur.execute("""
                INSERT INTO shop (item_id, price, stock) VALUES (%s, %s, %s)
                ON CONFLICT (item_id) DO UPDATE SET price=%s, stock=%s
            """, (item["id"], price, stock, price, stock))
        conn.commit()
    await ctx.send(f"✅ **{item_name}** added to shop for **{fmt_number(price)} 🪙**.")

@bot.command(name="addrecipe")
async def addrecipe_cmd(ctx, result_item: str, gold_cost: int, min_level: int, *, ingredients_str: str):
    """
    [Admin] Add a crafting recipe.
    -addrecipe <result item name> <gold cost> <min level> <item1:qty,item2:qty,...>
    Example: -addrecipe "Dragon Sword" 500 10 Iron:3,Dragon Scale:1
    """
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM items WHERE name ILIKE %s", (f"%{result_item}%",))
            result = cur.fetchone()
            if not result: await ctx.send(f"❌ Item `{result_item}` not found."); return

            ingredients = []
            for part in ingredients_str.split(","):
                part = part.strip()
                if ":" not in part:
                    await ctx.send(f"❌ Bad format for ingredient `{part}`. Use `item name:qty`."); return
                iname, qty_str = part.rsplit(":", 1)
                cur.execute("SELECT id FROM items WHERE name ILIKE %s", (f"%{iname.strip()}%",))
                ing_item = cur.fetchone()
                if not ing_item:
                    await ctx.send(f"❌ Ingredient item `{iname}` not found."); return
                ingredients.append({"item_id": ing_item["id"], "qty": int(qty_str.strip())})

            cur.execute("""
                INSERT INTO recipes (result_item_id, ingredients, gold_cost, min_level)
                VALUES (%s, %s, %s, %s)
            """, (result["id"], json.dumps(ingredients), gold_cost, min_level))
        conn.commit()
    await ctx.send(f"✅ Recipe for **{result_item}** added with {len(ingredients)} ingredient(s).")

@bot.command(name="giveitem")
async def giveitem_cmd(ctx, member: discord.Member, *, item_name: str):
    """[Admin] Give an item to a player. -giveitem @user <item name>"""
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM items WHERE name ILIKE %s", (f"%{item_name}%",))
            item = cur.fetchone()
            if not item: await ctx.send("❌ Item not found."); return
            cur.execute("""
                INSERT INTO inventory (user_id, item_id, quantity) VALUES (%s, %s, 1)
                ON CONFLICT (user_id, item_id) DO UPDATE SET quantity = inventory.quantity + 1
            """, (member.id, item["id"]))
        conn.commit()
    await ctx.send(f"✅ Gave **{item['name']}** to **{member.display_name}**.")

@bot.command(name="givegold")
async def givegold_cmd(ctx, member: discord.Member, amount: int):
    """[Admin] Give gold to a player. -givegold @user <amount>"""
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE characters SET gold = gold + %s WHERE user_id=%s", (amount, member.id))
        conn.commit()
    await ctx.send(f"✅ Gave **{fmt_number(amount)} 🪙** to **{member.display_name}**.")

# ═══════════════════════════════════════════════════════════════════════════════
# HELP & MISC
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="help", aliases=["h"])
async def help_cmd(ctx):
    e = discord.Embed(title="⚔️  RPG Bot Commands", color=0x5865F2)
    e.description = "Prefix: `-`\n\u200b"
    e.add_field(name="👤 Character", value=(
        "`-start <name>` — Create your character\n"
        "`-profile [@user]` — View stats & equipment\n"
    ), inline=False)
    e.add_field(name="🎒 Inventory & Equipment", value=(
        "`-inv [@user]` — View inventory\n"
        "`-equip <item>` — Equip an item\n"
        "`-unequip <slot>` — Unequip weapon/armor/accessory\n"
    ), inline=False)
    e.add_field(name="🗺️ Dungeons", value=(
        "`-dungeons` — Browse all dungeons\n"
        "`-party create <dungeon> [open]` — Create a party\n"
        "`-party invite @user` — Invite someone\n"
        "`-party join <code>` — Join open party\n"
        "`-party leave` — Leave your party\n"
        "`-party info` — View party details\n"
        "`-party start` — Start the dungeon run (leader only)\n"
        "`-party kick @user` — Kick a member (leader only)\n"
    ), inline=False)
    e.add_field(name="🛒 Economy", value=(
        "`-shop` — Browse shop\n"
        "`-buy <item>` — Buy from shop\n"
        "`-sell <item>` — Sell an item\n"
        "`-trade list <item> <price> [@user]` — List item for trade\n"
        "`-trade buy <id>` — Buy a trade listing\n"
        "`-trade cancel <id>` — Cancel your listing\n"
        "`-trade listings` — Browse all open trades\n"
        "`-trade mylistings` — Your trade listings\n"
    ), inline=False)
    e.add_field(name="⚒️ Crafting", value=(
        "`-recipes` — View all crafting recipes\n"
        "`-craftitem <id>` — Craft an item\n"
    ), inline=False)
    e.add_field(name="🏆 Leaderboard", value=(
        "`-lb [level|gold]` — Rankings\n"
    ), inline=False)
    if is_admin(ctx.author):
        e.add_field(name="⚙️ Admin", value=(
            "`-adddungeon <min_lv> <exp> <gold> [waves] <name>`\n"
            "`-addenemy <dungeon> <wave> <hp> <atk> <def> <exp> <gold> [count] <name>`\n"
            "`-additem <type> <rarity> <min_lv> <atk> <def> <hp> <gold> <name>`\n"
            "`-addloot <dungeon> <chance 0-1> <item name>`\n"
            "`-addshopitem <price> [stock] <item name>`\n"
            "`-addrecipe <result> <gold> <min_lv> <item:qty,...>`\n"
            "`-giveitem @user <item name>`\n"
            "`-givegold @user <amount>`\n"
        ), inline=False)
    e.set_footer(text="More features coming soon!")
    await ctx.send(embed=e)

@bot.command(name="ping")
async def ping_cmd(ctx):
    await ctx.send(f"Pong! `{round(bot.latency * 1000)} ms`")

# ═══════════════════════════════════════════════════════════════════════════════
# EVENTS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    init_db()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument. Use `-help` for usage.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Could not find that user.")
    elif isinstance(error, commands.CommandNotFound):
        pass  # Ignore unknown commands silently

# ─── Run ──────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
