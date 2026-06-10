import os
import discord
import random
import asyncio
from datetime import datetime, timezone
from discord.ext import commands
from flask import Flask
from threading import Thread
import psycopg2
from psycopg2.extras import RealDictCursor

# ─── Flask Keep-Alive ─────────────────────────────────────────────────────────

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── Supabase / PostgreSQL Setup ──────────────────────────────────────────────

DB_URL = os.getenv('DATABASE_URL')
if not DB_URL:
    print("ERROR: DATABASE_URL environment variable is missing!")
    exit(1)

def get_conn():
    import time
    last_err = None
    for attempt in range(3):
        try:
            return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor, sslmode='require', connect_timeout=10)
        except psycopg2.OperationalError as e:
            last_err = e
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise last_err

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS prizes (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    image TEXT,
                    chance BIGINT NOT NULL,
                    roll_message TEXT NOT NULL,
                    description TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS server_collection (
                    id SERIAL PRIMARY KEY,
                    prize_name TEXT UNIQUE NOT NULL,
                    discovered BOOLEAN DEFAULT FALSE,
                    first_user_id BIGINT,
                    first_user TEXT,
                    first_at TIMESTAMPTZ,
                    total_found INT DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS inventory (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    prize_name TEXT NOT NULL,
                    quantity INT DEFAULT 1,
                    first_found_at TIMESTAMPTZ DEFAULT NOW(),
                    last_found_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(user_id, prize_name)
                );

                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id BIGINT PRIMARY KEY,
                    bio TEXT,
                    favorite_prize TEXT,
                    showcase_prize TEXT,
                    total_messages BIGINT DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            # Add last_found_at column if it doesn't exist (migration)
            cur.execute("""
                ALTER TABLE inventory ADD COLUMN IF NOT EXISTS last_found_at TIMESTAMPTZ DEFAULT NOW();
            """)
        conn.commit()
    print("✅ Database tables ready")

# ─── DB Helpers ───────────────────────────────────────────────────────────────

def db_load_prizes() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM prizes ORDER BY chance DESC")
            return cur.fetchall()

def db_get_prize(name: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM prizes WHERE name = %s", (name,))
            return cur.fetchone()

def db_upsert_prize(data: dict) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM prizes WHERE name = %s", (data['name'],))
            existing = cur.fetchone()
            if existing:
                cur.execute("""
                    UPDATE prizes SET image=%s, chance=%s, roll_message=%s, description=%s
                    WHERE name=%s
                """, (data['image'], data['chance'], data['roll_message'], data['description'], data['name']))
                action = "Updated"
            else:
                cur.execute("""
                    INSERT INTO prizes (name, image, chance, roll_message, description)
                    VALUES (%s, %s, %s, %s, %s)
                """, (data['name'], data['image'], data['chance'], data['roll_message'], data['description']))
                cur.execute("""
                    INSERT INTO server_collection (prize_name, discovered, total_found)
                    VALUES (%s, FALSE, 0)
                    ON CONFLICT (prize_name) DO NOTHING
                """, (data['name'],))
                action = "Created"
        conn.commit()
    return action

def db_delete_prize(name: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prizes WHERE name = %s", (name,))
            cur.execute("DELETE FROM server_collection WHERE prize_name = %s", (name,))
        conn.commit()

def db_get_collection() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM server_collection ORDER BY prize_name")
            return cur.fetchall()

def db_get_disc(prize_name: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM server_collection WHERE prize_name = %s", (prize_name,))
            return cur.fetchone()

def db_record_roll(prize_name: str, user_id: int, user_name: str, now: datetime):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM server_collection WHERE prize_name = %s", (prize_name,))
            disc = cur.fetchone()

            if disc and not disc['discovered']:
                cur.execute("""
                    UPDATE server_collection
                    SET discovered=TRUE, first_user_id=%s, first_user=%s, first_at=%s, total_found=total_found+1
                    WHERE prize_name=%s
                """, (user_id, user_name, now, prize_name))
            else:
                cur.execute("""
                    UPDATE server_collection SET total_found=total_found+1 WHERE prize_name=%s
                """, (prize_name,))

            cur.execute("""
                INSERT INTO inventory (user_id, prize_name, quantity, first_found_at, last_found_at)
                VALUES (%s, %s, 1, %s, %s)
                ON CONFLICT (user_id, prize_name)
                DO UPDATE SET quantity = inventory.quantity + 1, last_found_at = %s
            """, (user_id, prize_name, now, now, now))

            # Increment message counter in profile
            cur.execute("""
                INSERT INTO user_profiles (user_id, total_messages)
                VALUES (%s, 1)
                ON CONFLICT (user_id)
                DO UPDATE SET total_messages = user_profiles.total_messages + 1
            """, (user_id,))
        conn.commit()

def db_get_inventory(user_id: int) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.*, p.chance FROM inventory i
                LEFT JOIN prizes p ON i.prize_name = p.name
                WHERE i.user_id=%s ORDER BY p.chance DESC NULLS LAST, i.quantity DESC
            """, (user_id,))
            return cur.fetchall()

def db_search_inventory(user_id: int, query: str) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.*, p.chance FROM inventory i
                LEFT JOIN prizes p ON i.prize_name = p.name
                WHERE i.user_id=%s AND i.prize_name ILIKE %s
                ORDER BY p.chance DESC NULLS LAST
            """, (user_id, f'%{query}%'))
            return cur.fetchall()

def db_leaderboard_total() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, SUM(quantity) as total,
                       COUNT(DISTINCT prize_name) as unique_prizes
                FROM inventory
                GROUP BY user_id
                ORDER BY total DESC
                LIMIT 10
            """)
            return cur.fetchall()

def db_leaderboard_unique() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, COUNT(DISTINCT prize_name) as unique_prizes,
                       SUM(quantity) as total
                FROM inventory
                GROUP BY user_id
                ORDER BY unique_prizes DESC
                LIMIT 10
            """)
            return cur.fetchall()

def db_leaderboard_rarity() -> list:
    """Leaderboard by total rarity score (sum of 1/chance for each prize found)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.user_id,
                       SUM(CAST(i.quantity AS FLOAT) / p.chance) as rarity_score,
                       SUM(i.quantity) as total
                FROM inventory i
                JOIN prizes p ON i.prize_name = p.name
                GROUP BY i.user_id
                ORDER BY rarity_score DESC
                LIMIT 10
            """)
            return cur.fetchall()

def db_rarest() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.*, s.first_user, s.first_at, s.total_found
                FROM prizes p
                JOIN server_collection s ON p.name = s.prize_name
                WHERE s.discovered = TRUE
                ORDER BY p.chance DESC
                LIMIT 8
            """)
            return cur.fetchall()

def db_stats() -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as c FROM prizes")
            total_prizes = cur.fetchone()['c']
            cur.execute("SELECT COUNT(*) as c FROM server_collection WHERE discovered=TRUE")
            discovered = cur.fetchone()['c']
            cur.execute("SELECT COUNT(*) as c FROM server_collection WHERE discovered=FALSE")
            undiscovered = cur.fetchone()['c']
            cur.execute("SELECT COALESCE(SUM(total_found),0) as c FROM server_collection")
            total_found = cur.fetchone()['c']
            cur.execute("SELECT COUNT(*) as c FROM inventory")
            inv_entries = cur.fetchone()['c']
            cur.execute("SELECT COUNT(DISTINCT user_id) as c FROM inventory")
            unique_users = cur.fetchone()['c']
    return {
        "total_prizes": total_prizes,
        "discovered": discovered,
        "undiscovered": undiscovered,
        "total_found": total_found,
        "inv_entries": inv_entries,
        "unique_users": unique_users,
    }

def db_get_profile(user_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_profiles WHERE user_id=%s", (user_id,))
            return cur.fetchone()

def db_upsert_profile(user_id: int, **kwargs):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_profiles (user_id) VALUES (%s)
                ON CONFLICT (user_id) DO NOTHING
            """, (user_id,))
            for key, val in kwargs.items():
                cur.execute(f"""
                    UPDATE user_profiles SET {key}=%s WHERE user_id=%s
                """, (val, user_id))
        conn.commit()

def db_user_inventory_summary(user_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT i.prize_name) as unique_prizes,
                       COALESCE(SUM(i.quantity), 0) as total_found,
                       MIN(i.first_found_at) as first_find,
                       MAX(i.last_found_at) as last_find
                FROM inventory i WHERE i.user_id=%s
            """, (user_id,))
            row = cur.fetchone()
            # Best prize by chance
            cur.execute("""
                SELECT i.prize_name, p.chance FROM inventory i
                JOIN prizes p ON i.prize_name = p.name
                WHERE i.user_id=%s ORDER BY p.chance DESC LIMIT 1
            """, (user_id,))
            best = cur.fetchone()
            return {**(row or {}), "best_prize": best}

def db_compare_inventories(user1_id: int, user2_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT prize_name FROM inventory WHERE user_id=%s
            """, (user1_id,))
            u1_prizes = {r["prize_name"] for r in cur.fetchall()}
            cur.execute("""
                SELECT prize_name FROM inventory WHERE user_id=%s
            """, (user2_id,))
            u2_prizes = {r["prize_name"] for r in cur.fetchall()}
    return {
        "shared": u1_prizes & u2_prizes,
        "only_u1": u1_prizes - u2_prizes,
        "only_u2": u2_prizes - u1_prizes,
    }

def db_recent_finds(limit: int = 10) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.user_id, i.prize_name, i.last_found_at, p.chance
                FROM inventory i
                JOIN prizes p ON i.prize_name = p.name
                ORDER BY i.last_found_at DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()

# ─── Bot Setup ────────────────────────────────────────────────────────────────

TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable is missing!")
    exit(1)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

# ─── Constants ────────────────────────────────────────────────────────────────

ADMIN_ROLE_ID            = 920309927375933490
ANNOUNCEMENT_CHANNEL_ID  = 900015775069401128

# ─── Rarity System ────────────────────────────────────────────────────────────

def get_rarity_info(chance: int) -> tuple:
    if chance >= 1_000_000_000:
        return ("★ MYTHIC",     0x111111, True,  True,  "mythic")
    if chance >= 100_000_000:
        return ("✦ LEGENDARY",  0xFF1493, True,  False, "legendary")
    if chance >= 10_000_000:
        return ("◆ EPIC+",      0x00008B, False, False, "dark_blue")
    if chance >= 1_000_000:
        return ("◇ EPIC",       0xFF69B4, False, False, "pink")
    if chance >= 100_000:
        return ("● RARE+",      0x00CED1, False, False, "cyan")
    if chance >= 10_000:
        return ("○ RARE",       0xFFA500, False, False, "orange")
    return     ("· COMMON",     0xAAAAAA, False, False, "normal")

EFFECT_TITLES = {
    "mythic":    "🌑 ══════ MYTHIC ROLL ══════ 🌑",
    "legendary": "🌸 ════ LEGENDARY ROLL ════ 🌸",
    "dark_blue": "💙 ══════ EPIC+ ROLL ══════ 💙",
    "pink":      "💗 ══════ EPIC ROLL ═══════ 💗",
    "cyan":      "🩵 ══════ RARE+ ROLL ══════ 🩵",
    "orange":    "🟠 ══════ RARE ROLL ═══════ 🟠",
    "normal":    "🎲 You got a roll!",
}

RARITY_EMOJIS = {
    "mythic": "🌑", "legendary": "🌸", "dark_blue": "💙",
    "pink": "💗", "cyan": "🩵", "orange": "🟠", "normal": "⚪",
}

ROLL_FRAMES = [
    "🎰 **|** ❓ **|** ❓ **|** ❓",
    "🎰 **|** 🌀 **|** ❓ **|** ❓",
    "🎰 **|** 🌀 **|** 🎯 **|** ❓",
    "🎰 **|** 🌀 **|** 🎯 **|** ✨",
]

# ─── Inventory Sort Options ───────────────────────────────────────────────────

INV_SORT_OPTIONS = {
    "rarity":    ("Rarity (highest first)", "p.chance DESC NULLS LAST"),
    "quantity":  ("Quantity (most first)",  "i.quantity DESC"),
    "name":      ("Name (A-Z)",             "i.prize_name ASC"),
    "recent":    ("Recently found",         "i.last_found_at DESC NULLS LAST"),
    "oldest":    ("First found",            "i.first_found_at ASC"),
}

def db_get_inventory_sorted(user_id: int, sort: str = "rarity") -> list:
    order = INV_SORT_OPTIONS.get(sort, INV_SORT_OPTIONS["rarity"])[1]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT i.*, p.chance FROM inventory i
                LEFT JOIN prizes p ON i.prize_name = p.name
                WHERE i.user_id=%s ORDER BY {order}
            """, (user_id,))
            return cur.fetchall()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.id == ADMIN_ROLE_ID for role in member.roles)

def format_dt(dt) -> str:
    if not dt:
        return "Unknown"
    if hasattr(dt, 'strftime'):
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    return str(dt)

def luck_description(chance: int) -> str:
    if chance >= 1_000_000_000: return "Beyond imagination"
    if chance >= 100_000_000:   return "Legendary fortune"
    if chance >= 10_000_000:    return "Incredibly lucky"
    if chance >= 1_000_000:     return "Extremely lucky"
    if chance >= 100_000:       return "Very lucky"
    if chance >= 10_000:        return "Lucky"
    return "Common luck"

def rarity_bar(chance: int) -> str:
    """Visual rarity bar out of 7 stars."""
    tiers = [1_000, 10_000, 100_000, 1_000_000, 10_000_000, 100_000_000, 1_000_000_000]
    filled = sum(1 for t in tiers if chance >= t)
    return "⭐" * filled + "☆" * (7 - filled)

# ─── Build Embeds ─────────────────────────────────────────────────────────────

def build_roll_embed(prize: dict, user: discord.Member) -> discord.Embed:
    chance = prize["chance"]
    label, color, _, _, effect = get_rarity_info(chance)
    description = (
        prize["roll_message"]
        .replace("{user}", user.mention)
        .replace("{prize}", f"**{prize['name']}**")
    )
    embed = discord.Embed(
        title=EFFECT_TITLES.get(effect, "🎲 You got a roll!"),
        description=description,
        color=color,
    )
    embed.add_field(name="🎁 Prize",  value=prize["name"],                        inline=True)
    embed.add_field(name="✨ Rarity", value=f"{label}\n1/{chance:,}",             inline=True)
    embed.add_field(name="🍀 Luck",   value=luck_description(chance),             inline=True)
    embed.add_field(name="⭐ Tier",   value=rarity_bar(chance),                   inline=False)
    embed.set_footer(
        text=f"Rolled by {user.display_name} • {format_dt(datetime.now(timezone.utc))}",
        icon_url=user.display_avatar.url,
    )
    if prize.get("image"):
        embed.set_image(url=prize["image"])
    return embed

def build_spinning_embed(frame: str, effect: str, color: int) -> discord.Embed:
    emoji = RARITY_EMOJIS.get(effect, "⚪")
    return discord.Embed(
        title=f"{emoji} Rolling...",
        description=f"```\n{frame}\n```",
        color=color,
    )

# ─── Animated Roll ────────────────────────────────────────────────────────────

async def do_animated_roll(channel, prize: dict, user: discord.Member):
    chance = prize["chance"]
    label, color, ping_everyone, send_announcement, effect = get_rarity_info(chance)

    msg = await channel.send(embed=build_spinning_embed(ROLL_FRAMES[0], effect, color))
    delays = [0.5, 0.5, 0.6, 0.8]
    for i, frame in enumerate(ROLL_FRAMES):
        await asyncio.sleep(delays[i])
        await msg.edit(embed=build_spinning_embed(frame, effect, color))

    await asyncio.sleep(1.0)

    final_embed = build_roll_embed(prize, user)
    content = "@everyone" if ping_everyone else ""
    await msg.edit(content=content, embed=final_embed)

    if send_announcement:
        ann_ch = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if ann_ch:
            await ann_ch.send(
                content="@everyone 🌑 **A MYTHIC prize has just been rolled!**",
                embed=final_embed,
            )

# ─── Prize Maker Modal ────────────────────────────────────────────────────────

class PrizeMakerModal(discord.ui.Modal, title="🎁 Create / Edit Prize"):
    prize_name = discord.ui.TextInput(label="Prize Name (unique)", placeholder="e.g. Golden Crown", max_length=100)
    image_url  = discord.ui.TextInput(label="Image URL (optional)", placeholder="https://i.imgur.com/example.png", required=False, max_length=500)
    chance     = discord.ui.TextInput(label="Chance (1 in X) — e.g. 1000 = 1/1000", placeholder="1000", max_length=12)
    roll_msg   = discord.ui.TextInput(label="Roll Message ({user} and {prize} tokens)", placeholder="{user} has rolled {prize}! 🎉", max_length=300, style=discord.TextStyle.paragraph)
    desc       = discord.ui.TextInput(label="Prize Description (shown in collection)", placeholder="A rare golden crown...", max_length=200, required=False, style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            chance_val = int(self.chance.value.strip())
            if chance_val < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Chance must be a positive whole number.", ephemeral=True)
            return

        name = self.prize_name.value.strip()
        data = {
            "name":         name,
            "image":        self.image_url.value.strip() or None,
            "chance":       chance_val,
            "roll_message": self.roll_msg.value.strip(),
            "description":  self.desc.value.strip() or None,
        }

        action = db_upsert_prize(data)
        label, color, ping_e, announce, _ = get_rarity_info(chance_val)

        embed = discord.Embed(title=f"✅ Prize {action}!", color=color)
        embed.add_field(name="Name",    value=name,                           inline=True)
        embed.add_field(name="Rarity",  value=f"{label}  (1/{chance_val:,})", inline=True)
        embed.add_field(name="Message", value=data["roll_message"],           inline=False)
        preview = data["roll_message"].replace("{user}", interaction.user.mention).replace("{prize}", f"**{name}**")
        embed.add_field(name="📋 Preview", value=preview, inline=False)
        flags = []
        if ping_e:   flags.append("Pings @everyone")
        if announce: flags.append("→ Announcement channel")
        if flags:
            embed.add_field(name="⚠️ Effects", value=" • ".join(flags), inline=False)
        if data["image"]:
            embed.set_thumbnail(url=data["image"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ─── Profile Bio Modal ────────────────────────────────────────────────────────

class BioModal(discord.ui.Modal, title="✏️ Edit Your Bio"):
    bio = discord.ui.TextInput(
        label="Bio (shown on your profile)",
        placeholder="Tell the server about yourself...",
        max_length=200,
        required=False,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction):
        db_upsert_profile(interaction.user.id, bio=self.bio.value.strip() or None)
        await interaction.response.send_message("✅ Bio updated!", ephemeral=True)

# ─── Selects ──────────────────────────────────────────────────────────────────

def prize_options(prizes) -> list:
    return [
        discord.SelectOption(
            label=p["name"][:100],
            description=f"{get_rarity_info(p['chance'])[0]}  •  1/{p['chance']:,}",
            value=p["name"],
        )
        for p in list(prizes)[:25]
    ]

class DeleteSelect(discord.ui.Select):
    def __init__(self, prizes):
        super().__init__(placeholder="Choose a prize to delete…", options=prize_options(prizes))
    async def callback(self, interaction: discord.Interaction):
        db_delete_prize(self.values[0])
        await interaction.response.send_message(f"🗑️ Deleted **{self.values[0]}** and its collection entry.", ephemeral=True)

class DeleteSelectView(discord.ui.View):
    def __init__(self, prizes):
        super().__init__(timeout=60)
        self.add_item(DeleteSelect(prizes))

class PreviewSelect(discord.ui.Select):
    def __init__(self, prizes):
        super().__init__(placeholder="Choose a prize to preview…", options=prize_options(prizes))
    async def callback(self, interaction: discord.Interaction):
        prize = db_get_prize(self.values[0])
        if not prize:
            await interaction.response.send_message("❌ Prize not found.", ephemeral=True); return
        embed = build_roll_embed(prize, interaction.user)
        await interaction.response.send_message(content="**🔍 Preview:**", embed=embed, ephemeral=True)

class PreviewSelectView(discord.ui.View):
    def __init__(self, prizes):
        super().__init__(timeout=60)
        self.add_item(PreviewSelect(prizes))

class EditSelect(discord.ui.Select):
    def __init__(self, prizes):
        super().__init__(placeholder="Choose a prize to edit…", options=prize_options(prizes))
    async def callback(self, interaction: discord.Interaction):
        prize = db_get_prize(self.values[0])
        modal = PrizeMakerModal()
        if prize:
            modal.prize_name.default = prize["name"]
            modal.image_url.default  = prize.get("image") or ""
            modal.chance.default     = str(prize["chance"])
            modal.roll_msg.default   = prize["roll_message"]
            modal.desc.default       = prize.get("description") or ""
        await interaction.response.send_modal(modal)

class EditSelectView(discord.ui.View):
    def __init__(self, prizes):
        super().__init__(timeout=60)
        self.add_item(EditSelect(prizes))

# Showcase prize select (from user's inventory)
class ShowcaseSelect(discord.ui.Select):
    def __init__(self, entries):
        options = [
            discord.SelectOption(
                label=e["prize_name"][:100],
                description=f"Found {e['quantity']}x",
                value=e["prize_name"],
            )
            for e in list(entries)[:25]
        ]
        super().__init__(placeholder="Choose a prize to showcase…", options=options)
    async def callback(self, interaction: discord.Interaction):
        db_upsert_profile(interaction.user.id, showcase_prize=self.values[0])
        await interaction.response.send_message(f"✅ Showcase prize set to **{self.values[0]}**!", ephemeral=True)

class ShowcaseSelectView(discord.ui.View):
    def __init__(self, entries):
        super().__init__(timeout=60)
        self.add_item(ShowcaseSelect(entries))

# ─── Inventory Sort Select ────────────────────────────────────────────────────

class InventorySortSelect(discord.ui.Select):
    def __init__(self, user: discord.Member, current_sort: str):
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                default=(key == current_sort),
            )
            for key, (label, _) in INV_SORT_OPTIONS.items()
        ]
        super().__init__(placeholder="Sort by…", options=options)
        self.inv_user = user

    async def callback(self, interaction: discord.Interaction):
        entries = db_get_inventory_sorted(self.inv_user.id, self.values[0])
        view = InventoryView(list(entries), self.inv_user, sort=self.values[0])
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

# ─── Leaderboard Type Select ──────────────────────────────────────────────────

class LeaderboardTypeSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild, current: str):
        options = [
            discord.SelectOption(label="🏆 Most Prizes Found",    value="total",  default=(current == "total")),
            discord.SelectOption(label="🎯 Most Unique Prizes",   value="unique", default=(current == "unique")),
            discord.SelectOption(label="💎 Rarity Score",         value="rarity", default=(current == "rarity")),
        ]
        super().__init__(placeholder="Change leaderboard type…", options=options)
        self.guild = guild

    async def callback(self, interaction: discord.Interaction):
        view = LeaderboardView(self.guild, lb_type=self.values[0])
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

# ─── Prize Maker Panel ────────────────────────────────────────────────────────

class PrizeMakerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Add Prize",    style=discord.ButtonStyle.success,   row=0)
    async def add_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        await interaction.response.send_modal(PrizeMakerModal())

    @discord.ui.button(label="✏️ Edit Prize",   style=discord.ButtonStyle.primary,   row=0)
    async def edit_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        prizes = db_load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes yet.", ephemeral=True); return
        await interaction.response.send_message("Select a prize to edit:", view=EditSelectView(prizes), ephemeral=True)

    @discord.ui.button(label="🗑️ Delete Prize", style=discord.ButtonStyle.danger,    row=0)
    async def delete_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        prizes = db_load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes to delete.", ephemeral=True); return
        await interaction.response.send_message("Select a prize to delete:", view=DeleteSelectView(prizes), ephemeral=True)

    @discord.ui.button(label="📋 List Prizes",  style=discord.ButtonStyle.secondary, row=1)
    async def list_prizes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        prizes = db_load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes yet!", ephemeral=True); return
        embed = discord.Embed(title="🎁 All Prizes", color=0x5865F2)
        for p in prizes:
            label = get_rarity_info(p["chance"])[0]
            embed.add_field(
                name=p["name"],
                value=f"{label}  •  1/{p['chance']:,}\n_{p.get('description') or 'No description'}_",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="👁️ Preview Roll", style=discord.ButtonStyle.secondary, row=1)
    async def preview_roll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        prizes = db_load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes to preview.", ephemeral=True); return
        await interaction.response.send_message("Select a prize to preview:", view=PreviewSelectView(prizes), ephemeral=True)

    @discord.ui.button(label="📊 Stats",        style=discord.ButtonStyle.secondary, row=1)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        s = db_stats()
        embed = discord.Embed(title="📊 Bot Statistics", color=0x5865F2)
        embed.add_field(name="Total Prizes",      value=str(s["total_prizes"]),  inline=True)
        embed.add_field(name="Discovered",        value=str(s["discovered"]),    inline=True)
        embed.add_field(name="Undiscovered",      value=str(s["undiscovered"]),  inline=True)
        embed.add_field(name="Total Rolls Won",   value=str(s["total_found"]),   inline=True)
        embed.add_field(name="Inventory Entries", value=str(s["inv_entries"]),   inline=True)
        embed.add_field(name="Active Players",    value=str(s["unique_users"]),  inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ─── Inventory Pagination (reworked) ─────────────────────────────────────────

class InventoryView(discord.ui.View):
    def __init__(self, entries: list, user: discord.Member, page: int = 0, sort: str = "rarity"):
        super().__init__(timeout=120)
        self.entries = entries
        self.user    = user
        self.page    = page
        self.sort    = sort
        self.per     = 6
        self.pages   = max(1, (len(entries) + self.per - 1) // self.per)
        self._update_buttons()
        self.add_item(InventorySortSelect(user, sort))

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def build_embed(self) -> discord.Embed:
        chunk   = self.entries[self.page * self.per:(self.page + 1) * self.per]
        summary = db_user_inventory_summary(self.user.id)
        total   = summary.get("total_found", 0)
        unique  = summary.get("unique_prizes", 0)

        sort_label = INV_SORT_OPTIONS.get(self.sort, ("",))[0]
        embed = discord.Embed(
            title=f"🎒 {self.user.display_name}'s Inventory",
            color=0x5865F2,
            description=(
                f"**{unique}** unique  •  **{total}** total found\n"
                f"Sorted by: *{sort_label}*  •  Page {self.page+1}/{self.pages}"
            ),
        )

        all_prizes = db_load_prizes()
        total_prizes = len(all_prizes)
        completion_pct = round(unique / total_prizes * 100, 1) if total_prizes else 0
        bar_filled = int(completion_pct / 10)
        progress_bar = "█" * bar_filled + "░" * (10 - bar_filled)
        embed.add_field(
            name="📈 Completion",
            value=f"`{progress_bar}` {completion_pct}%  ({unique}/{total_prizes})",
            inline=False,
        )

        for entry in chunk:
            chance = entry.get("chance")
            label  = get_rarity_info(chance)[0] if chance else "Unknown"
            embed.add_field(
                name=entry["prize_name"],
                value=(
                    f"{label}  •  **{entry['quantity']}x**\n"
                    f"First: {format_dt(entry.get('first_found_at'))}\n"
                    f"Last: {format_dt(entry.get('last_found_at'))}"
                ),
                inline=True,
            )
        embed.set_thumbnail(url=self.user.display_avatar.url)
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1; self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1; self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

# ─── Collection Pagination ────────────────────────────────────────────────────

class CollectionView(discord.ui.View):
    def __init__(self, all_prizes: list, discoveries: dict, page: int = 0):
        super().__init__(timeout=120)
        self.all_prizes  = all_prizes
        self.discoveries = discoveries
        self.page        = page
        self.per         = 6
        self.pages       = max(1, (len(all_prizes) + self.per - 1) // self.per)
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def build_embed(self) -> discord.Embed:
        chunk      = self.all_prizes[self.page * self.per:(self.page + 1) * self.per]
        total      = len(self.all_prizes)
        discovered = sum(1 for d in self.discoveries.values() if d.get("discovered"))
        bar_filled = int(discovered / total * 10) if total else 0
        progress   = "█" * bar_filled + "░" * (10 - bar_filled)
        embed = discord.Embed(
            title="🗂️ Server Collection",
            description=(
                f"**{discovered}/{total}** prizes discovered\n"
                f"`{progress}` {round(discovered/total*100,1) if total else 0}%\n"
                f"Page {self.page+1}/{self.pages}"
            ),
            color=0x5865F2,
        )
        for prize in chunk:
            disc = self.discoveries.get(prize["name"], {})
            if disc.get("discovered"):
                label, _, _, _, effect = get_rarity_info(prize["chance"])
                emoji = RARITY_EMOJIS.get(effect, "⚪")
                embed.add_field(
                    name=f"{emoji} {prize['name']}",
                    value=(
                        f"{label}  •  1/{prize['chance']:,}\n"
                        f"_{prize.get('description') or 'No description'}_\n"
                        f"First: **{disc.get('first_user', 'Unknown')}**\n"
                        f"📅 {format_dt(disc.get('first_at'))}\n"
                        f"🍀 {luck_description(prize['chance'])}\n"
                        f"🔢 Found **{disc.get('total_found', 0)}x** by server"
                    ),
                    inline=True,
                )
            else:
                embed.add_field(
                    name="❓ ???",
                    value="_Not yet discovered._",
                    inline=True,
                )
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1; self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1; self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

# ─── Leaderboard View (reworked) ─────────────────────────────────────────────

class LeaderboardView(discord.ui.View):
    def __init__(self, guild: discord.Guild, lb_type: str = "total"):
        super().__init__(timeout=120)
        self.guild   = guild
        self.lb_type = lb_type
        self.add_item(LeaderboardTypeSelect(guild, lb_type))

    def build_embed(self) -> discord.Embed:
        medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7

        if self.lb_type == "total":
            results = db_leaderboard_total()
            embed = discord.Embed(title="🏆 Leaderboard — Most Prizes Found", color=0xFFD700)
            for i, r in enumerate(results):
                member = self.guild.get_member(r["user_id"])
                name   = member.display_name if member else f"User {r['user_id']}"
                embed.add_field(
                    name=f"{medals[i]} #{i+1} — {name}",
                    value=f"**{r['total']}** total  •  **{r['unique_prizes']}** unique",
                    inline=False,
                )

        elif self.lb_type == "unique":
            results = db_leaderboard_unique()
            embed = discord.Embed(title="🎯 Leaderboard — Most Unique Prizes", color=0x00CED1)
            for i, r in enumerate(results):
                member = self.guild.get_member(r["user_id"])
                name   = member.display_name if member else f"User {r['user_id']}"
                embed.add_field(
                    name=f"{medals[i]} #{i+1} — {name}",
                    value=f"**{r['unique_prizes']}** unique  •  **{r['total']}** total",
                    inline=False,
                )

        else:  # rarity
            results = db_leaderboard_rarity()
            embed = discord.Embed(title="💎 Leaderboard — Rarity Score", color=0xFF1493)
            embed.description = "_Score = sum of (quantity / chance) for all prizes owned. Higher = luckier._"
            for i, r in enumerate(results):
                member = self.guild.get_member(r["user_id"])
                name   = member.display_name if member else f"User {r['user_id']}"
                score  = round(float(r["rarity_score"]), 6)
                embed.add_field(
                    name=f"{medals[i]} #{i+1} — {name}",
                    value=f"Score: **{score}**  •  **{r['total']}** prizes",
                    inline=False,
                )

        embed.set_footer(text="Use the dropdown to switch leaderboard type")
        return embed

# ─── Profile View ─────────────────────────────────────────────────────────────

class ProfileView(discord.ui.View):
    def __init__(self, target: discord.Member, is_self: bool):
        super().__init__(timeout=60)
        if not is_self:
            self.edit_bio_btn.disabled = True
            self.set_showcase_btn.disabled = True

    @discord.ui.button(label="✏️ Edit Bio", style=discord.ButtonStyle.primary)
    async def edit_bio_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BioModal())

    @discord.ui.button(label="🌟 Set Showcase", style=discord.ButtonStyle.secondary)
    async def set_showcase_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        entries = db_get_inventory(interaction.user.id)
        if not entries:
            await interaction.response.send_message("You have no prizes to showcase!", ephemeral=True); return
        await interaction.response.send_message("Choose your showcase prize:", view=ShowcaseSelectView(list(entries)), ephemeral=True)

# ─── on_message Rolling ───────────────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    prizes = db_load_prizes()
    for prize in prizes:
        if random.randint(1, prize["chance"]) == 1:
            now = datetime.now(timezone.utc)
            db_record_roll(prize["name"], message.author.id, message.author.display_name, now)
            await do_animated_roll(message.channel, prize, message.author)
            break
    else:
        # Still increment message counter even with no win
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO user_profiles (user_id, total_messages)
                        VALUES (%s, 1)
                        ON CONFLICT (user_id)
                        DO UPDATE SET total_messages = user_profiles.total_messages + 1
                    """, (message.author.id,))
                conn.commit()
        except Exception:
            pass

    await bot.process_commands(message)

# ─── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    init_db()
    total = len(db_load_prizes())
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   {total} prize(s) in database")

# ─── Commands ─────────────────────────────────────────────────────────────────

@bot.command(name="prizemaker", aliases=["pm"])
async def prizemaker(ctx: commands.Context):
    if not is_admin(ctx.author):
        await ctx.send("❌ You don't have permission."); return
    embed = discord.Embed(
        title="🎁 Prize Maker",
        description=(
            "Manage all prizes from the buttons below.\n\n"
            "**Rarity Tiers**\n"
            "· Common    — 1/1,000+\n"
            "○ Rare      — 1/10,000+\n"
            "● Rare+     — 1/100,000+\n"
            "◇ Epic      — 1/1,000,000+\n"
            "◆ Epic+     — 1/10,000,000+\n"
            "✦ Legendary — 1/100,000,000+  *(pings @everyone)*\n"
            "★ Mythic    — 1/1,000,000,000+  *(pings @everyone + Announcement)*\n\n"
            "Prizes persist in **Supabase** across all deploys."
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="Admin-only panel")
    await ctx.send(embed=embed, view=PrizeMakerView())


@bot.command(name="collection", aliases=["col", "c"])
async def collection_cmd(ctx: commands.Context):
    prizes = db_load_prizes()
    if not prizes:
        await ctx.send("No prizes exist yet!"); return
    disc_docs = {d["prize_name"]: d for d in db_get_collection()}
    view = CollectionView(list(prizes), disc_docs)
    await ctx.send(embed=view.build_embed(), view=view)


@bot.command(name="inventory", aliases=["inv", "i"])
async def inventory_cmd(ctx: commands.Context, member: discord.Member = None, sort: str = "rarity"):
    """Show inventory. Optional: @user and sort (rarity/quantity/name/recent/oldest)"""
    target = member or ctx.author
    if sort not in INV_SORT_OPTIONS:
        sort = "rarity"
    entries = db_get_inventory_sorted(target.id, sort)
    if not entries:
        name = "You have" if target == ctx.author else f"{target.display_name} has"
        await ctx.send(f"{name} no prizes yet!"); return
    view = InventoryView(list(entries), target, sort=sort)
    await ctx.send(embed=view.build_embed(), view=view)


@bot.command(name="search", aliases=["find", "s"])
async def search_cmd(ctx: commands.Context, *, query: str):
    results = db_search_inventory(ctx.author.id, query)
    if not results:
        await ctx.send(f"❌ No prizes matching `{query}` in your inventory."); return
    embed = discord.Embed(title=f"🔍 Search: '{query}'", color=0x5865F2)
    for entry in list(results)[:10]:
        chance = entry.get("chance")
        label  = get_rarity_info(chance)[0] if chance else "Unknown"
        disc   = db_get_disc(entry["prize_name"])
        embed.add_field(
            name=entry["prize_name"],
            value=(
                f"{label}  •  Found **{entry['quantity']}x**\n"
                f"First: {format_dt(entry.get('first_found_at'))}\n"
                f"Server total: **{disc.get('total_found', 0) if disc else 0}x**"
            ),
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb", "top"])
async def leaderboard_cmd(ctx: commands.Context, lb_type: str = "total"):
    """Show leaderboard. Types: total, unique, rarity"""
    if lb_type not in ("total", "unique", "rarity"):
        lb_type = "total"
    view = LeaderboardView(ctx.guild, lb_type=lb_type)
    await ctx.send(embed=view.build_embed(), view=view)


@bot.command(name="rarest", aliases=["rare"])
async def rarest_cmd(ctx: commands.Context):
    results = db_rarest()
    if not results:
        await ctx.send("No prizes discovered yet!"); return
    embed = discord.Embed(title="💎 Rarest Prizes Found", color=0x5865F2)
    for r in results:
        label, _, _, _, effect = get_rarity_info(r["chance"])
        emoji = RARITY_EMOJIS.get(effect, "⚪")
        embed.add_field(
            name=f"{emoji} {r['name']}",
            value=(
                f"{label}  •  1/{r['chance']:,}\n"
                f"First by **{r.get('first_user', 'Unknown')}**\n"
                f"📅 {format_dt(r.get('first_at'))}\n"
                f"Server: **{r.get('total_found', 0)}x**"
            ),
            inline=True,
        )
    await ctx.send(embed=embed)


@bot.command(name="profile", aliases=["p", "prof"])
async def profile_cmd(ctx: commands.Context, member: discord.Member = None):
    """View your profile or another user's profile."""
    target  = member or ctx.author
    is_self = (target == ctx.author)
    profile = db_get_profile(target.id)
    summary = db_user_inventory_summary(target.id)

    all_prizes   = db_load_prizes()
    total_prizes = len(all_prizes)
    unique       = summary.get("unique_prizes", 0) or 0
    total_found  = summary.get("total_found",   0) or 0
    completion   = round(unique / total_prizes * 100, 1) if total_prizes else 0
    bar_filled   = int(completion / 10)
    progress_bar = "█" * bar_filled + "░" * (10 - bar_filled)

    embed = discord.Embed(
        title=f"👤 {target.display_name}'s Profile",
        color=0x5865F2,
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    bio = (profile.get("bio") if profile else None) or "_No bio set. Use -setbio to add one!_"
    embed.add_field(name="📝 Bio", value=bio, inline=False)

    # Showcase prize
    showcase_name = profile.get("showcase_prize") if profile else None
    if showcase_name:
        prize = db_get_prize(showcase_name)
        if prize:
            label, _, _, _, effect = get_rarity_info(prize["chance"])
            emoji = RARITY_EMOJIS.get(effect, "⚪")
            embed.add_field(name="🌟 Showcase Prize", value=f"{emoji} **{showcase_name}**\n{label}  •  1/{prize['chance']:,}", inline=False)
            if prize.get("image"):
                embed.set_image(url=prize["image"])

    # Stats
    best = summary.get("best_prize")
    best_str = "None yet"
    if best:
        bl, _, _, _, be = get_rarity_info(best["chance"])
        best_str = f"{RARITY_EMOJIS.get(be,'⚪')} **{best['prize_name']}** ({bl})"

    embed.add_field(name="🎁 Unique Prizes",  value=f"**{unique}**",            inline=True)
    embed.add_field(name="🔢 Total Found",    value=f"**{total_found}**",        inline=True)
    embed.add_field(name="💬 Messages Sent",  value=f"**{profile.get('total_messages', 0) if profile else 0}**", inline=True)
    embed.add_field(name="📈 Completion",     value=f"`{progress_bar}` {completion}%", inline=False)
    embed.add_field(name="💎 Best Prize",     value=best_str,                   inline=False)

    first_find = summary.get("first_find")
    if first_find:
        embed.set_footer(text=f"First prize found: {format_dt(first_find)}")

    view = ProfileView(target, is_self)
    await ctx.send(embed=embed, view=view)


@bot.command(name="setbio")
async def setbio_cmd(ctx: commands.Context, *, bio: str = ""):
    """Set your profile bio. Leave blank to clear it."""
    db_upsert_profile(ctx.author.id, bio=bio.strip() or None)
    if bio.strip():
        await ctx.send(f"✅ Bio updated!", delete_after=5)
    else:
        await ctx.send(f"✅ Bio cleared!", delete_after=5)


@bot.command(name="showcase")
async def showcase_cmd(ctx: commands.Context):
    """Set your showcase prize from your inventory."""
    entries = db_get_inventory(ctx.author.id)
    if not entries:
        await ctx.send("You have no prizes to showcase!"); return
    await ctx.send("Choose your showcase prize:", view=ShowcaseSelectView(list(entries)))


@bot.command(name="compare", aliases=["vs"])
async def compare_cmd(ctx: commands.Context, member: discord.Member):
    """Compare your inventory with another user's."""
    if member == ctx.author:
        await ctx.send("You can't compare with yourself!"); return
    data = db_compare_inventories(ctx.author.id, member.id)

    embed = discord.Embed(
        title=f"⚔️ {ctx.author.display_name} vs {member.display_name}",
        color=0x5865F2,
    )
    shared_str   = ", ".join(sorted(data["shared"]))[:1000]   or "_None_"
    only_u1_str  = ", ".join(sorted(data["only_u1"]))[:1000]  or "_None_"
    only_u2_str  = ", ".join(sorted(data["only_u2"]))[:1000]  or "_None_"

    embed.add_field(name=f"🤝 Shared ({len(data['shared'])})",                  value=shared_str,  inline=False)
    embed.add_field(name=f"✅ Only {ctx.author.display_name} ({len(data['only_u1'])})", value=only_u1_str, inline=False)
    embed.add_field(name=f"✅ Only {member.display_name} ({len(data['only_u2'])})",     value=only_u2_str, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="prizeinfo", aliases=["pi"])
async def prizeinfo_cmd(ctx: commands.Context, *, name: str):
    """Look up detailed info on a specific prize."""
    prize = db_get_prize(name)
    if not prize:
        # Try partial match
        prizes = db_load_prizes()
        matches = [p for p in prizes if name.lower() in p["name"].lower()]
        if not matches:
            await ctx.send(f"❌ No prize found matching `{name}`."); return
        if len(matches) > 1:
            names = ", ".join(p["name"] for p in matches[:10])
            await ctx.send(f"Multiple matches: {names}\nBe more specific!"); return
        prize = matches[0]

    disc  = db_get_disc(prize["name"])
    label, color, _, _, effect = get_rarity_info(prize["chance"])
    emoji = RARITY_EMOJIS.get(effect, "⚪")

    embed = discord.Embed(title=f"{emoji} {prize['name']}", color=color)
    embed.add_field(name="✨ Rarity",      value=f"{label}  •  1/{prize['chance']:,}", inline=True)
    embed.add_field(name="🍀 Luck",        value=luck_description(prize["chance"]),    inline=True)
    embed.add_field(name="⭐ Tier",        value=rarity_bar(prize["chance"]),          inline=True)
    embed.add_field(name="📝 Description", value=prize.get("description") or "_None_", inline=False)

    if disc:
        if disc.get("discovered"):
            embed.add_field(name="🥇 First Found By", value=disc.get("first_user", "Unknown"),    inline=True)
            embed.add_field(name="📅 First Found At",  value=format_dt(disc.get("first_at")),      inline=True)
            embed.add_field(name="🔢 Server Total",    value=f"**{disc.get('total_found', 0)}x**", inline=True)
        else:
            embed.add_field(name="🔍 Status", value="_Not yet discovered by the server!_", inline=False)

    if prize.get("image"):
        embed.set_image(url=prize["image"])
    await ctx.send(embed=embed)


@bot.command(name="recent", aliases=["feed"])
async def recent_cmd(ctx: commands.Context):
    """Show the most recently found prizes across the server."""
    results = db_recent_finds(10)
    if not results:
        await ctx.send("No prizes found yet!"); return
    embed = discord.Embed(title="📰 Recent Prize Finds", color=0x5865F2)
    for r in results:
        member = ctx.guild.get_member(r["user_id"])
        name   = member.display_name if member else f"User {r['user_id']}"
        label, _, _, _, effect = get_rarity_info(r["chance"])
        emoji  = RARITY_EMOJIS.get(effect, "⚪")
        embed.add_field(
            name=f"{emoji} {r['prize_name']}",
            value=f"By **{name}** • {format_dt(r['last_found_at'])}",
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    await ctx.send(f"🏓 Pong! Latency: `{round(bot.latency * 1000)}ms`")


@bot.command(name="help", aliases=["h"])
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(title="📖 Commands  (prefix: `-`)", color=0x5865F2)

    embed.add_field(name="━━━ Collection ━━━",             value="\u200b",                                           inline=False)
    embed.add_field(name="-collection  (-col, -c)",         value="Server prize collection",                          inline=False)
    embed.add_field(name="-rarest  (-rare)",                value="Rarest discovered prizes",                         inline=False)
    embed.add_field(name="-recent  (-feed)",                value="Latest prize finds across the server",             inline=False)
    embed.add_field(name="-prizeinfo <name>  (-pi)",        value="Detailed info on a specific prize",                inline=False)

    embed.add_field(name="━━━ Inventory ━━━",              value="\u200b",                                           inline=False)
    embed.add_field(name="-inventory [@user] [sort]  (-inv, -i)", value="Your prizes. Sort: `rarity` `quantity` `name` `recent` `oldest`", inline=False)
    embed.add_field(name="-search <query>  (-find, -s)",   value="Search your inventory",                            inline=False)
    embed.add_field(name="-compare @user  (-vs)",          value="Compare your collection with another user",        inline=False)

    embed.add_field(name="━━━ Profile ━━━",                value="\u200b",                                           inline=False)
    embed.add_field(name="-profile [@user]  (-p, -prof)",  value="View your profile or another user's",              inline=False)
    embed.add_field(name="-setbio [text]",                 value="Set your profile bio (leave blank to clear)",      inline=False)
    embed.add_field(name="-showcase",                      value="Set your showcase prize",                          inline=False)

    embed.add_field(name="━━━ Leaderboard ━━━",            value="\u200b",                                           inline=False)
    embed.add_field(name="-leaderboard [type]  (-lb, -top)", value="Top finders. Types: `total` `unique` `rarity`", inline=False)

    embed.add_field(name="━━━ Other ━━━",                  value="\u200b",                                           inline=False)
    embed.add_field(name="-ping",                          value="Check bot latency",                                inline=False)

    if is_admin(ctx.author):
        embed.add_field(name="━━━ Admin ━━━",              value="\u200b",                                           inline=False)
        embed.add_field(name="-prizemaker  (-pm)",         value="Prize management panel",                           inline=False)

    await ctx.send(embed=embed)


# ─── Run ──────────────────────────────────────────────────────────────────────

bot.run(TOKEN)
