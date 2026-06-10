import os
import discord
import random
import asyncio
import time
from datetime import datetime, timezone
from discord.ext import commands, tasks
from flask import Flask
from threading import Thread
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

# ─── Flask Keep-Alive ─────────────────────────────────────────────────────────

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── Connection Pool ──────────────────────────────────────────────────────────

DB_URL = os.getenv('DATABASE_URL')
if not DB_URL:
    print("ERROR: DATABASE_URL environment variable is missing!")
    exit(1)

_pool: ThreadedConnectionPool = None

def get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(
            minconn=2, maxconn=10,
            dsn=DB_URL,
            cursor_factory=RealDictCursor,
            sslmode='require',
            connect_timeout=10,
        )
    return _pool

class _Conn:
    def __enter__(self):
        self.conn = get_pool().getconn()
        return self.conn
    def __exit__(self, exc_type, *_):
        if exc_type:
            self.conn.rollback()
        get_pool().putconn(self.conn)

def get_conn():
    return _Conn()

# ─── Prize Cache ──────────────────────────────────────────────────────────────

_prize_cache: list = []
_prize_cache_ts: float = 0.0
PRIZE_CACHE_TTL = 60

def get_prizes_cached() -> list:
    global _prize_cache, _prize_cache_ts
    if time.time() - _prize_cache_ts > PRIZE_CACHE_TTL:
        _prize_cache = db_load_prizes()
        _prize_cache_ts = time.time()
    return _prize_cache

def invalidate_prize_cache():
    global _prize_cache_ts
    _prize_cache_ts = 0.0

# ─── DB Init ──────────────────────────────────────────────────────────────────

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
                    equipped_prize TEXT,
                    total_messages BIGINT DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            for col_sql in [
                "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS last_found_at TIMESTAMPTZ DEFAULT NOW();",
                "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS equipped_prize TEXT;",
            ]:
                cur.execute(col_sql)
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
                    VALUES (%s, FALSE, 0) ON CONFLICT (prize_name) DO NOTHING
                """, (data['name'],))
                action = "Created"
        conn.commit()
    invalidate_prize_cache()
    return action

def db_delete_prize(name: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prizes WHERE name = %s", (name,))
            cur.execute("DELETE FROM server_collection WHERE prize_name = %s", (name,))
        conn.commit()
    invalidate_prize_cache()

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
            cur.execute("SELECT discovered FROM server_collection WHERE prize_name = %s", (prize_name,))
            disc = cur.fetchone()
            if disc and not disc['discovered']:
                cur.execute("""
                    UPDATE server_collection
                    SET discovered=TRUE, first_user_id=%s, first_user=%s, first_at=%s, total_found=total_found+1
                    WHERE prize_name=%s
                """, (user_id, user_name, now, prize_name))
            else:
                cur.execute("UPDATE server_collection SET total_found=total_found+1 WHERE prize_name=%s", (prize_name,))

            cur.execute("""
                INSERT INTO inventory (user_id, prize_name, quantity, first_found_at, last_found_at)
                VALUES (%s, %s, 1, %s, %s)
                ON CONFLICT (user_id, prize_name)
                DO UPDATE SET quantity = inventory.quantity + 1, last_found_at = %s
            """, (user_id, prize_name, now, now, now))

            cur.execute("""
                INSERT INTO user_profiles (user_id, total_messages) VALUES (%s, 1)
                ON CONFLICT (user_id) DO UPDATE SET total_messages = user_profiles.total_messages + 1
            """, (user_id,))
        conn.commit()

def db_increment_messages(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_profiles (user_id, total_messages) VALUES (%s, 1)
                ON CONFLICT (user_id) DO UPDATE SET total_messages = user_profiles.total_messages + 1
            """, (user_id,))
        conn.commit()

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
                SELECT user_id, SUM(quantity) as total, COUNT(DISTINCT prize_name) as unique_prizes
                FROM inventory GROUP BY user_id ORDER BY total DESC LIMIT 10
            """)
            return cur.fetchall()

def db_leaderboard_unique() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, COUNT(DISTINCT prize_name) as unique_prizes, SUM(quantity) as total
                FROM inventory GROUP BY user_id ORDER BY unique_prizes DESC LIMIT 10
            """)
            return cur.fetchall()

def db_leaderboard_rarity() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.user_id,
                       SUM(CAST(i.quantity AS FLOAT) / p.chance) as rarity_score,
                       SUM(i.quantity) as total
                FROM inventory i JOIN prizes p ON i.prize_name = p.name
                GROUP BY i.user_id ORDER BY rarity_score DESC LIMIT 10
            """)
            return cur.fetchall()

def db_rarest() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.*, s.first_user, s.first_at, s.total_found
                FROM prizes p JOIN server_collection s ON p.name = s.prize_name
                WHERE s.discovered = TRUE ORDER BY p.chance DESC LIMIT 8
            """)
            return cur.fetchall()

def db_stats() -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM prizes) AS total_prizes,
                    (SELECT COUNT(*) FROM server_collection WHERE discovered=TRUE) AS discovered,
                    (SELECT COUNT(*) FROM server_collection WHERE discovered=FALSE) AS undiscovered,
                    (SELECT COALESCE(SUM(total_found),0) FROM server_collection) AS total_found,
                    (SELECT COUNT(*) FROM inventory) AS inv_entries,
                    (SELECT COUNT(DISTINCT user_id) FROM inventory) AS unique_users,
                    (SELECT COALESCE(SUM(total_messages),0) FROM user_profiles) AS total_messages,
                    (SELECT name FROM prizes ORDER BY chance DESC LIMIT 1) AS rarest_prize,
                    (SELECT name FROM prizes ORDER BY chance ASC LIMIT 1) AS commonest_prize,
                    (SELECT prize_name FROM server_collection WHERE discovered=TRUE ORDER BY total_found DESC LIMIT 1) AS most_found_prize,
                    (SELECT COUNT(DISTINCT user_id) FROM inventory WHERE last_found_at >= NOW() - INTERVAL '24 hours') AS active_today,
                    (SELECT COUNT(*) FROM inventory WHERE last_found_at >= NOW() - INTERVAL '24 hours') AS rolls_today
            """)
            return dict(cur.fetchone())

def db_get_profile(user_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_profiles WHERE user_id=%s", (user_id,))
            return cur.fetchone()

def db_upsert_profile(user_id: int, **kwargs):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_profiles (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING
            """, (user_id,))
            for key, val in kwargs.items():
                cur.execute(f"UPDATE user_profiles SET {key}=%s WHERE user_id=%s", (val, user_id))
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
            cur.execute("SELECT prize_name FROM inventory WHERE user_id=%s", (user1_id,))
            u1 = {r["prize_name"] for r in cur.fetchall()}
            cur.execute("SELECT prize_name FROM inventory WHERE user_id=%s", (user2_id,))
            u2 = {r["prize_name"] for r in cur.fetchall()}
    return {"shared": u1 & u2, "only_u1": u1 - u2, "only_u2": u2 - u1}

def db_recent_finds(limit: int = 10) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.user_id, i.prize_name, i.last_found_at, p.chance
                FROM inventory i JOIN prizes p ON i.prize_name = p.name
                ORDER BY i.last_found_at DESC LIMIT %s
            """, (limit,))
            return cur.fetchall()

def db_get_inventory(user_id: int) -> list:
    return db_get_inventory_sorted(user_id, "rarity")

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

ADMIN_ROLE_ID           = 920309927375933490
ANNOUNCEMENT_CHANNEL_ID = 900015775069401128

# ─── Rarity System ────────────────────────────────────────────────────────────

def get_rarity_info(chance: int) -> tuple:
    # Returns: (label, color, ping_everyone, send_announcement, tier_key)
    if chance >= 1_000_000_000: return ("MYTHIC",     0x2B0F3D, True,  True,  "mythic")
    if chance >= 100_000_000:   return ("LEGENDARY",  0xFF6B6B, True,  False, "legendary")
    if chance >= 10_000_000:    return ("EPIC+",       0x4A90D9, False, False, "epic_plus")
    if chance >= 1_000_000:     return ("EPIC",        0xC97FD4, False, False, "epic")
    if chance >= 100_000:       return ("RARE+",       0x5EC4B8, False, False, "rare_plus")
    if chance >= 10_000:        return ("RARE",        0xF4A23C, False, False, "rare")
    return                             ("COMMON",      0x8B9099, False, False, "common")

# Tier accent colors (left border / badge bg) — used in embeds
TIER_ACCENT = {
    "mythic":    0x6B21A8,
    "legendary": 0xDC2626,
    "epic_plus": 0x1D4ED8,
    "epic":      0x7C3AED,
    "rare_plus": 0x0D9488,
    "rare":      0xD97706,
    "common":    0x6B7280,
}

# Short decorative tier badges shown inline in embeds
TIER_BADGE = {
    "mythic":    "◈ MYTHIC",
    "legendary": "◆ LEGENDARY",
    "epic_plus": "◇ EPIC+",
    "epic":      "◇ EPIC",
    "rare_plus": "○ RARE+",
    "rare":      "○ RARE",
    "common":    "· COMMON",
}

# Per-tier header lines for roll result embeds
ROLL_HEADER = {
    "mythic":    "✦  A mythic prize has emerged  ✦",
    "legendary": "✦  A legendary prize appears  ✦",
    "epic_plus": "A powerful prize revealed",
    "epic":      "An epic prize revealed",
    "rare_plus": "A rare prize discovered",
    "rare":      "A rare prize discovered",
    "common":    "You found a prize",
}

RARITY_EMOJI = {
    "mythic":    "🌑",
    "legendary": "💎",
    "epic_plus": "💙",
    "epic":      "💜",
    "rare_plus": "🩵",
    "rare":      "🟠",
    "common":    "⚪",
}

ROLL_FRAMES = [
    "❔  ·  ❔  ·  ❔",
    "🌀  ·  ❔  ·  ❔",
    "🌀  ·  🎯  ·  ❔",
    "🌀  ·  🎯  ·  ✨",
]

INV_SORT_OPTIONS = {
    "rarity":   ("Rarity (rarest first)", "p.chance DESC NULLS LAST"),
    "quantity": ("Quantity (most first)",  "i.quantity DESC"),
    "name":     ("Name (A–Z)",             "i.prize_name ASC"),
    "recent":   ("Recently found",         "i.last_found_at DESC NULLS LAST"),
    "oldest":   ("First found",            "i.first_found_at ASC"),
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or any(r.id == ADMIN_ROLE_ID for r in member.roles)

def format_dt(dt) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d %b %Y, %H:%M UTC") if hasattr(dt, 'strftime') else str(dt)

def format_dt_short(dt) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d %b %Y") if hasattr(dt, 'strftime') else str(dt)

def rarity_tier_bar(chance: int) -> str:
    """Seven-segment bar showing how far along the rarity scale a prize sits."""
    thresholds = [10_000, 100_000, 1_000_000, 10_000_000, 100_000_000, 1_000_000_000]
    filled = sum(1 for t in thresholds if chance >= t)
    return "▰" * filled + "▱" * (6 - filled)

def owner_check(interaction: discord.Interaction, original_user_id: int) -> bool:
    return interaction.user.id == original_user_id

# ─── Build Embeds ─────────────────────────────────────────────────────────────

def build_spinning_embed(frame: str, tier: str, color: int) -> discord.Embed:
    embed = discord.Embed(
        description=f"```\n{frame}\n```",
        color=color,
    )
    embed.set_footer(text="Rolling…")
    return embed


def build_roll_embed(prize: dict, user: discord.Member) -> discord.Embed:
    chance = prize["chance"]
    label, color, _, _, tier = get_rarity_info(chance)
    badge  = TIER_BADGE[tier]
    emoji  = RARITY_EMOJI[tier]
    header = ROLL_HEADER[tier]

    description = (
        prize["roll_message"]
        .replace("{user}", user.mention)
        .replace("{prize}", f"**{prize['name']}**")
    )

    embed = discord.Embed(
        title=f"{emoji}  {header}",
        description=f"{description}\n\u200b",
        color=color,
    )

    # Prize name + tier on one row
    embed.add_field(
        name="Prize",
        value=f"**{prize['name']}**",
        inline=True,
    )
    embed.add_field(
        name="Tier",
        value=f"`{badge}`",
        inline=True,
    )
    embed.add_field(
        name="Odds",
        value=f"1 in {chance:,}",
        inline=True,
    )

    # Rarity spectrum bar
    embed.add_field(
        name="Rarity spectrum",
        value=f"`{rarity_tier_bar(chance)}`",
        inline=False,
    )

    if prize.get("description"):
        embed.add_field(name="About", value=f"*{prize['description']}*", inline=False)

    embed.set_footer(
        text=f"Rolled by {user.display_name}  ·  {datetime.now(timezone.utc).strftime('%d %b %Y, %H:%M UTC')}",
        icon_url=user.display_avatar.url,
    )
    if prize.get("image"):
        embed.set_image(url=prize["image"])
    return embed


# ─── Stats Embed ──────────────────────────────────────────────────────────────

def _build_stats_embed(s: dict) -> discord.Embed:
    total  = s.get("total_prizes", 0)
    disc   = s.get("discovered", 0)
    pct    = round(disc / total * 100, 1) if total else 0
    filled = int(pct / 10)
    bar    = "▰" * filled + "▱" * (10 - filled)

    embed = discord.Embed(
        title="Server statistics",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.description = f"`{bar}`  **{disc}/{total}** prizes discovered  ({pct}%)"

    embed.add_field(
        name="Rolls",
        value=f"All-time  **{s.get('total_found', 0):,}**\nToday  **{s.get('rolls_today', 0):,}**",
        inline=True,
    )
    embed.add_field(
        name="Players",
        value=f"Ever played  **{s.get('unique_users', 0):,}**\nActive today  **{s.get('active_today', 0):,}**",
        inline=True,
    )
    embed.add_field(
        name="Messages tracked",
        value=f"**{s.get('total_messages', 0):,}**",
        inline=True,
    )

    if s.get("rarest_prize"):
        p = db_get_prize(s["rarest_prize"])
        if p:
            label, _, _, _, tier = get_rarity_info(p["chance"])
            embed.add_field(
                name="Rarest prize",
                value=f"{RARITY_EMOJI[tier]} **{p['name']}**\n`{TIER_BADGE[tier]}`  ·  1/{p['chance']:,}",
                inline=True,
            )
    if s.get("commonest_prize"):
        p = db_get_prize(s["commonest_prize"])
        if p:
            label, _, _, _, tier = get_rarity_info(p["chance"])
            embed.add_field(
                name="Most common prize",
                value=f"{RARITY_EMOJI[tier]} **{p['name']}**\n`{TIER_BADGE[tier]}`  ·  1/{p['chance']:,}",
                inline=True,
            )
    if s.get("most_found_prize"):
        disc_row = db_get_disc(s["most_found_prize"])
        embed.add_field(
            name="Most-found prize",
            value=f"**{s['most_found_prize']}**  ·  {disc_row.get('total_found', '?')}× rolled",
            inline=True,
        )

    embed.set_footer(text="Stats refresh in real time")
    return embed


# ─── Animated Roll ────────────────────────────────────────────────────────────

async def do_animated_roll(channel, prize: dict, user: discord.Member):
    chance = prize["chance"]
    label, color, ping_everyone, send_announcement, tier = get_rarity_info(chance)

    msg = await channel.send(embed=build_spinning_embed(ROLL_FRAMES[0], tier, color))
    for i, frame in enumerate(ROLL_FRAMES[1:], 1):
        await asyncio.sleep(0.5 if i < 3 else 0.8)
        await msg.edit(embed=build_spinning_embed(frame, tier, color))

    await asyncio.sleep(1.0)
    final_embed = build_roll_embed(prize, user)
    content = "@everyone" if ping_everyone else ""
    await msg.edit(content=content, embed=final_embed)

    if send_announcement:
        ann_ch = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if ann_ch:
            await ann_ch.send(
                content="@everyone  🌑  **A mythic prize has just been rolled!**",
                embed=final_embed,
            )


# ─── Prize Maker Modal ────────────────────────────────────────────────────────

class PrizeMakerModal(discord.ui.Modal, title="Create / Edit Prize"):
    prize_name = discord.ui.TextInput(label="Prize name (unique)", placeholder="Golden Crown", max_length=100)
    image_url  = discord.ui.TextInput(label="Image URL (optional)", placeholder="https://i.imgur.com/example.png", required=False, max_length=500)
    chance     = discord.ui.TextInput(label="Chance — 1 in X  (e.g. 10000 = 1/10,000)", placeholder="10000", max_length=12)
    roll_msg   = discord.ui.TextInput(label="Roll message  •  use {user} and {prize}", placeholder="{user} rolled {prize}!", max_length=300, style=discord.TextStyle.paragraph)
    desc       = discord.ui.TextInput(label="Description (shown in collection)", placeholder="A rare golden crown...", max_length=200, required=False, style=discord.TextStyle.paragraph)

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
            "name": name,
            "image": self.image_url.value.strip() or None,
            "chance": chance_val,
            "roll_message": self.roll_msg.value.strip(),
            "description": self.desc.value.strip() or None,
        }

        action = db_upsert_prize(data)
        label, color, ping_e, announce, tier = get_rarity_info(chance_val)
        badge = TIER_BADGE[tier]

        embed = discord.Embed(
            title=f"Prize {action.lower()}",
            color=color,
        )
        embed.add_field(name="Name",     value=name,                           inline=True)
        embed.add_field(name="Tier",     value=f"`{badge}`",                   inline=True)
        embed.add_field(name="Odds",     value=f"1 in {chance_val:,}",         inline=True)
        embed.add_field(name="Spectrum", value=f"`{rarity_tier_bar(chance_val)}`", inline=False)

        preview = data["roll_message"].replace("{user}", interaction.user.mention).replace("{prize}", f"**{name}**")
        embed.add_field(name="Message preview", value=preview, inline=False)

        flags = []
        if ping_e:   flags.append("Pings @everyone")
        if announce: flags.append("Posts to announcement channel")
        if flags:
            embed.add_field(name="⚠️ Effects", value="  ·  ".join(flags), inline=False)
        if data["image"]:
            embed.set_thumbnail(url=data["image"])
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── Bio Modal ────────────────────────────────────────────────────────────────

class BioModal(discord.ui.Modal, title="Edit your bio"):
    bio = discord.ui.TextInput(
        label="Bio",
        placeholder="Tell the server about yourself…",
        max_length=200,
        required=False,
        style=discord.TextStyle.paragraph,
    )
    async def on_submit(self, interaction: discord.Interaction):
        db_upsert_profile(interaction.user.id, bio=self.bio.value.strip() or None)
        await interaction.response.send_message("✅ Bio updated.", ephemeral=True)


# ─── Selects ──────────────────────────────────────────────────────────────────

def prize_options(prizes) -> list:
    return [
        discord.SelectOption(
            label=p["name"][:100],
            description=f"{TIER_BADGE.get(get_rarity_info(p['chance'])[4], '')}  ·  1/{p['chance']:,}",
            value=p["name"],
        )
        for p in list(prizes)[:25]
    ]

class DeleteSelect(discord.ui.Select):
    def __init__(self, prizes):
        super().__init__(placeholder="Select a prize to delete…", options=prize_options(prizes))
    async def callback(self, interaction: discord.Interaction):
        db_delete_prize(self.values[0])
        await interaction.response.send_message(f"🗑️ **{self.values[0]}** deleted.", ephemeral=True)

class DeleteSelectView(discord.ui.View):
    def __init__(self, prizes):
        super().__init__(timeout=60)
        self.add_item(DeleteSelect(prizes))

class PreviewSelect(discord.ui.Select):
    def __init__(self, prizes):
        super().__init__(placeholder="Select a prize to preview…", options=prize_options(prizes))
    async def callback(self, interaction: discord.Interaction):
        prize = db_get_prize(self.values[0])
        if not prize:
            await interaction.response.send_message("❌ Prize not found.", ephemeral=True)
            return
        embed = build_roll_embed(prize, interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class PreviewSelectView(discord.ui.View):
    def __init__(self, prizes):
        super().__init__(timeout=60)
        self.add_item(PreviewSelect(prizes))

class EditSelect(discord.ui.Select):
    def __init__(self, prizes):
        super().__init__(placeholder="Select a prize to edit…", options=prize_options(prizes))
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

class ShowcaseSelect(discord.ui.Select):
    def __init__(self, entries):
        options = [
            discord.SelectOption(
                label=e["prize_name"][:100],
                description=f"Owned {e['quantity']}×",
                value=e["prize_name"],
            )
            for e in list(entries)[:25]
        ]
        super().__init__(placeholder="Choose your showcase prize…", options=options)
    async def callback(self, interaction: discord.Interaction):
        db_upsert_profile(interaction.user.id, showcase_prize=self.values[0])
        await interaction.response.send_message(f"✅ Showcase set to **{self.values[0]}**.", ephemeral=True)

class ShowcaseSelectView(discord.ui.View):
    def __init__(self, entries):
        super().__init__(timeout=60)
        self.add_item(ShowcaseSelect(entries))


# ─── Inventory Sort Select ────────────────────────────────────────────────────

class InventorySortSelect(discord.ui.Select):
    def __init__(self, user: discord.Member, current_sort: str, owner_id: int):
        options = [
            discord.SelectOption(label=label, value=key, default=(key == current_sort))
            for key, (label, _) in INV_SORT_OPTIONS.items()
        ]
        super().__init__(placeholder="Sort by…", options=options, row=0)
        self.inv_user = user
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction):
        if not owner_check(interaction, self.owner_id):
            await interaction.response.send_message("This isn't your inventory panel.", ephemeral=True)
            return
        entries = db_get_inventory_sorted(self.inv_user.id, self.values[0])
        view = InventoryView(list(entries), self.inv_user, sort=self.values[0], owner_id=self.owner_id)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


# ─── Leaderboard Type Select ──────────────────────────────────────────────────

class LeaderboardTypeSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild, current: str, owner_id: int):
        options = [
            discord.SelectOption(label="Most prizes found",  value="total",  default=(current == "total")),
            discord.SelectOption(label="Most unique prizes", value="unique", default=(current == "unique")),
            discord.SelectOption(label="Rarity score",       value="rarity", default=(current == "rarity")),
        ]
        super().__init__(placeholder="Switch leaderboard…", options=options)
        self.guild    = guild
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction):
        if not owner_check(interaction, self.owner_id):
            await interaction.response.send_message("This isn't your leaderboard panel.", ephemeral=True)
            return
        view = LeaderboardView(self.guild, lb_type=self.values[0], owner_id=self.owner_id)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


# ─── Prize Maker Panel ────────────────────────────────────────────────────────

class PrizeMakerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="New prize",     style=discord.ButtonStyle.success,   row=0)
    async def add_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        await interaction.response.send_modal(PrizeMakerModal())

    @discord.ui.button(label="Edit prize",    style=discord.ButtonStyle.primary,   row=0)
    async def edit_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        prizes = db_load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes yet.", ephemeral=True); return
        await interaction.response.send_message("Select a prize to edit:", view=EditSelectView(prizes), ephemeral=True)

    @discord.ui.button(label="Delete prize",  style=discord.ButtonStyle.danger,    row=0)
    async def delete_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        prizes = db_load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes to delete.", ephemeral=True); return
        await interaction.response.send_message("Select a prize to delete:", view=DeleteSelectView(prizes), ephemeral=True)

    @discord.ui.button(label="List all",      style=discord.ButtonStyle.secondary, row=1)
    async def list_prizes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        prizes = db_load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes yet.", ephemeral=True); return
        embed = discord.Embed(title="All prizes", color=0x5865F2)
        embed.description = f"{len(prizes)} prizes in the pool\n\u200b"
        for p in prizes:
            _, _, _, _, tier = get_rarity_info(p["chance"])
            badge = TIER_BADGE[tier]
            embed.add_field(
                name=p["name"],
                value=f"`{badge}`  ·  1/{p['chance']:,}\n*{p.get('description') or 'No description'}*",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Preview roll",  style=discord.ButtonStyle.secondary, row=1)
    async def preview_roll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        prizes = db_load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes to preview.", ephemeral=True); return
        await interaction.response.send_message("Select a prize to preview:", view=PreviewSelectView(prizes), ephemeral=True)

    @discord.ui.button(label="Stats",         style=discord.ButtonStyle.secondary, row=1)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True); return
        embed = _build_stats_embed(db_stats())
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── Inventory Pagination ─────────────────────────────────────────────────────

class InventoryView(discord.ui.View):
    def __init__(self, entries: list, user: discord.Member, page: int = 0, sort: str = "rarity", owner_id: int = None):
        super().__init__(timeout=120)
        self.entries  = entries
        self.user     = user
        self.page     = page
        self.sort     = sort
        self.owner_id = owner_id or user.id
        self.per      = 6
        self.pages    = max(1, (len(entries) + self.per - 1) // self.per)
        self.add_item(InventorySortSelect(user, sort, self.owner_id))
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def build_embed(self) -> discord.Embed:
        chunk        = self.entries[self.page * self.per:(self.page + 1) * self.per]
        summary      = db_user_inventory_summary(self.user.id)
        total        = int(summary.get("total_found", 0) or 0)
        unique       = int(summary.get("unique_prizes", 0) or 0)
        all_prizes   = get_prizes_cached()
        total_prizes = len(all_prizes)
        completion   = round(unique / total_prizes * 100, 1) if total_prizes else 0
        bar_filled   = int(completion / 10)
        progress_bar = "▰" * bar_filled + "▱" * (10 - bar_filled)
        sort_label   = INV_SORT_OPTIONS.get(self.sort, ("",))[0]

        embed = discord.Embed(
            title=f"{self.user.display_name}'s collection",
            color=0x5865F2,
        )
        embed.description = (
            f"`{progress_bar}`  **{completion}%** complete  ({unique}/{total_prizes} unique · {total} total)\n"
            f"Sorted by *{sort_label}*  ·  page {self.page+1}/{self.pages}"
        )

        for entry in chunk:
            chance = entry.get("chance")
            _, _, _, _, tier = get_rarity_info(chance) if chance else (None, None, None, None, "common")
            badge  = TIER_BADGE[tier]
            emoji  = RARITY_EMOJI[tier]
            embed.add_field(
                name=f"{emoji}  {entry['prize_name']}",
                value=(
                    f"`{badge}`\n"
                    f"Owned  **{entry['quantity']}×**\n"
                    f"First found  {format_dt_short(entry.get('first_found_at'))}\n"
                    f"Last found  {format_dt_short(entry.get('last_found_at'))}"
                ),
                inline=True,
            )

        embed.set_thumbnail(url=self.user.display_avatar.url)
        return embed

    @discord.ui.button(label="◀  Prev", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not owner_check(interaction, self.owner_id):
            await interaction.response.send_message("This isn't your inventory panel.", ephemeral=True); return
        self.page -= 1; self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next  ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not owner_check(interaction, self.owner_id):
            await interaction.response.send_message("This isn't your inventory panel.", ephemeral=True); return
        self.page += 1; self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


# ─── Collection Pagination ────────────────────────────────────────────────────

class CollectionView(discord.ui.View):
    def __init__(self, all_prizes: list, discoveries: dict, page: int = 0, owner_id: int = None):
        super().__init__(timeout=120)
        self.all_prizes  = all_prizes
        self.discoveries = discoveries
        self.page        = page
        self.owner_id    = owner_id
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
        pct        = round(discovered / total * 100, 1) if total else 0
        bar_filled = int(pct / 10)
        progress   = "▰" * bar_filled + "▱" * (10 - bar_filled)

        embed = discord.Embed(
            title="Server collection",
            color=0x5865F2,
        )
        embed.description = (
            f"`{progress}`  **{pct}%**  ({discovered}/{total} discovered)\n"
            f"Page {self.page+1}/{self.pages}"
        )

        for prize in chunk:
            disc = self.discoveries.get(prize["name"], {})
            if disc.get("discovered"):
                _, _, _, _, tier = get_rarity_info(prize["chance"])
                badge = TIER_BADGE[tier]
                emoji = RARITY_EMOJI[tier]
                embed.add_field(
                    name=f"{emoji}  {prize['name']}",
                    value=(
                        f"`{badge}`  ·  1/{prize['chance']:,}\n"
                        f"*{prize.get('description') or 'No description'}*\n"
                        f"First found by **{disc.get('first_user', '—')}**\n"
                        f"{format_dt_short(disc.get('first_at'))}  ·  {disc.get('total_found', 0)}× total"
                    ),
                    inline=True,
                )
            else:
                embed.add_field(name="❔  Unknown", value="*Not yet discovered.*", inline=True)

        return embed

    @discord.ui.button(label="◀  Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.owner_id and not owner_check(interaction, self.owner_id):
            await interaction.response.send_message("This isn't your collection panel.", ephemeral=True); return
        self.page -= 1; self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next  ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.owner_id and not owner_check(interaction, self.owner_id):
            await interaction.response.send_message("This isn't your collection panel.", ephemeral=True); return
        self.page += 1; self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


# ─── Leaderboard View ─────────────────────────────────────────────────────────

class LeaderboardView(discord.ui.View):
    def __init__(self, guild: discord.Guild, lb_type: str = "total", owner_id: int = None):
        super().__init__(timeout=120)
        self.guild    = guild
        self.lb_type  = lb_type
        self.owner_id = owner_id
        self.add_item(LeaderboardTypeSelect(guild, lb_type, owner_id))

    def build_embed(self) -> discord.Embed:
        podium  = ["🥇", "🥈", "🥉"]
        others  = ["4", "5", "6", "7", "8", "9", "10"]
        medals  = podium + [f"`{n}`" for n in others]

        if self.lb_type == "total":
            results = db_leaderboard_total()
            embed = discord.Embed(title="Most prizes found", color=0xF4A23C)
            embed.description = "Ranked by total prizes collected of all time.\n\u200b"
            for i, r in enumerate(results):
                m    = self.guild.get_member(r["user_id"])
                name = m.display_name if m else f"User {r['user_id']}"
                embed.add_field(
                    name=f"{medals[i]}  {name}",
                    value=f"**{r['total']:,}** prizes  ·  {r['unique_prizes']} unique",
                    inline=False,
                )

        elif self.lb_type == "unique":
            results = db_leaderboard_unique()
            embed = discord.Embed(title="Most unique prizes", color=0x5EC4B8)
            embed.description = "Ranked by number of distinct prizes ever found.\n\u200b"
            for i, r in enumerate(results):
                m    = self.guild.get_member(r["user_id"])
                name = m.display_name if m else f"User {r['user_id']}"
                embed.add_field(
                    name=f"{medals[i]}  {name}",
                    value=f"**{r['unique_prizes']}** unique  ·  {r['total']:,} total",
                    inline=False,
                )

        else:
            results = db_leaderboard_rarity()
            embed = discord.Embed(title="Rarity score", color=0xC97FD4)
            embed.description = "Score = Σ(quantity ÷ chance).  A higher score means luckier rolls.\n\u200b"
            for i, r in enumerate(results):
                m     = self.guild.get_member(r["user_id"])
                name  = m.display_name if m else f"User {r['user_id']}"
                score = round(float(r["rarity_score"]), 6)
                embed.add_field(
                    name=f"{medals[i]}  {name}",
                    value=f"Score  **{score}**  ·  {r['total']:,} prizes",
                    inline=False,
                )

        embed.set_footer(text="Use the dropdown to switch view")
        return embed


# ─── Profile View ─────────────────────────────────────────────────────────────

class ProfileView(discord.ui.View):
    def __init__(self, target: discord.Member, is_self: bool, owner_id: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        if not is_self:
            self.edit_bio_btn.disabled     = True
            self.set_showcase_btn.disabled = True

    @discord.ui.button(label="Edit bio", style=discord.ButtonStyle.primary)
    async def edit_bio_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not owner_check(interaction, self.owner_id):
            await interaction.response.send_message("This isn't your profile.", ephemeral=True); return
        await interaction.response.send_modal(BioModal())

    @discord.ui.button(label="Set showcase", style=discord.ButtonStyle.secondary)
    async def set_showcase_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not owner_check(interaction, self.owner_id):
            await interaction.response.send_message("This isn't your profile.", ephemeral=True); return
        entries = db_get_inventory(interaction.user.id)
        if not entries:
            await interaction.response.send_message("You have no prizes to showcase yet.", ephemeral=True); return
        await interaction.response.send_message("Choose your showcase prize:", view=ShowcaseSelectView(list(entries)), ephemeral=True)


# ─── on_message Rolling ───────────────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    prizes = get_prizes_cached()
    won = False
    for prize in prizes:
        if random.randint(1, prize["chance"]) == 1:
            now = datetime.now(timezone.utc)
            db_record_roll(prize["name"], message.author.id, message.author.display_name, now)
            await do_animated_roll(message.channel, prize, message.author)
            won = True
            break

    if not won:
        try:
            db_increment_messages(message.author.id)
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
    """[Admin] Open the prize management panel."""
    if not is_admin(ctx.author):
        await ctx.send("❌ You don't have permission to use this."); return

    embed = discord.Embed(
        title="Prize Manager",
        color=0x5865F2,
    )
    embed.description = (
        "Manage prizes from the buttons below.\n\u200b"
    )
    embed.add_field(
        name="Rarity tiers",
        value=(
            "⚪  `· COMMON`     — odds up to 1/9,999\n"
            "🟠  `○ RARE`       — 1/10,000+\n"
            "🩵  `○ RARE+`      — 1/100,000+\n"
            "💜  `◇ EPIC`       — 1/1,000,000+\n"
            "💙  `◇ EPIC+`      — 1/10,000,000+\n"
            "💎  `◆ LEGENDARY`  — 1/100,000,000+  *(pings @everyone)*\n"
            "🌑  `◈ MYTHIC`     — 1/1,000,000,000+  *(pings @everyone + announcement)*"
        ),
        inline=False,
    )
    embed.set_footer(text="Admin only  ·  All data persists in Supabase")
    await ctx.send(embed=embed, view=PrizeMakerView())


@bot.command(name="collection", aliases=["col", "c"])
async def collection_cmd(ctx: commands.Context):
    """Browse all prizes the server has — discovered and hidden."""
    prizes = db_load_prizes()
    if not prizes:
        await ctx.send("No prizes exist yet. Admins can add some with `-prizemaker`."); return
    disc_docs = {d["prize_name"]: d for d in db_get_collection()}
    view = CollectionView(list(prizes), disc_docs, owner_id=ctx.author.id)
    await ctx.send(embed=view.build_embed(), view=view)


@bot.command(name="inventory", aliases=["inv", "i"])
async def inventory_cmd(ctx: commands.Context, member: discord.Member = None, sort: str = "rarity"):
    """
    Show your prize inventory, or another user's.
    Sort options: rarity · quantity · name · recent · oldest
    """
    target = member or ctx.author
    if sort not in INV_SORT_OPTIONS:
        sort = "rarity"
    entries = db_get_inventory_sorted(target.id, sort)
    if not entries:
        noun = "You have" if target == ctx.author else f"**{target.display_name}** has"
        await ctx.send(f"{noun} no prizes yet — keep chatting to earn one!"); return
    view = InventoryView(list(entries), target, sort=sort, owner_id=ctx.author.id)
    await ctx.send(embed=view.build_embed(), view=view)


@bot.command(name="search", aliases=["find", "s"])
async def search_cmd(ctx: commands.Context, *, query: str):
    """Search your inventory by prize name."""
    results = db_search_inventory(ctx.author.id, query)
    if not results:
        await ctx.send(f"No prizes matching `{query}` in your inventory."); return

    embed = discord.Embed(title=f"Search: {query}", color=0x5865F2)
    embed.description = f"{len(results[:10])} result(s)\n\u200b"

    for entry in list(results)[:10]:
        chance = entry.get("chance")
        _, _, _, _, tier = get_rarity_info(chance) if chance else (None, None, None, None, "common")
        badge = TIER_BADGE[tier]
        emoji = RARITY_EMOJI[tier]
        disc  = db_get_disc(entry["prize_name"])
        embed.add_field(
            name=f"{emoji}  {entry['prize_name']}",
            value=(
                f"`{badge}`  ·  1/{chance:,}\n"
                f"You own  **{entry['quantity']}×**\n"
                f"First found  {format_dt_short(entry.get('first_found_at'))}\n"
                f"Server total  {disc.get('total_found', 0) if disc else 0}×"
            ),
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb", "top"])
async def leaderboard_cmd(ctx: commands.Context, lb_type: str = "total"):
    """
    Show the server leaderboard.
    Types: total · unique · rarity
    """
    if lb_type not in ("total", "unique", "rarity"):
        lb_type = "total"
    view = LeaderboardView(ctx.guild, lb_type=lb_type, owner_id=ctx.author.id)
    await ctx.send(embed=view.build_embed(), view=view)


@bot.command(name="rarest", aliases=["rare"])
async def rarest_cmd(ctx: commands.Context):
    """Show the rarest prizes ever found on this server."""
    results = db_rarest()
    if not results:
        await ctx.send("No prizes have been discovered yet."); return

    embed = discord.Embed(title="Rarest finds", color=0x5865F2)
    embed.description = "The hardest prizes to roll that have been found here.\n\u200b"

    for r in results:
        _, _, _, _, tier = get_rarity_info(r["chance"])
        badge = TIER_BADGE[tier]
        emoji = RARITY_EMOJI[tier]
        embed.add_field(
            name=f"{emoji}  {r['name']}",
            value=(
                f"`{badge}`  ·  1/{r['chance']:,}\n"
                f"First by **{r.get('first_user', '—')}**  ·  {format_dt_short(r.get('first_at'))}\n"
                f"{r.get('total_found', 0)}× found total"
            ),
            inline=True,
        )
    await ctx.send(embed=embed)


@bot.command(name="profile", aliases=["p", "prof"])
async def profile_cmd(ctx: commands.Context, member: discord.Member = None):
    """View your profile card, or another user's."""
    target  = member or ctx.author
    is_self = (target == ctx.author)
    profile = db_get_profile(target.id)
    summary = db_user_inventory_summary(target.id)

    all_prizes   = get_prizes_cached()
    total_prizes = len(all_prizes)
    unique       = int(summary.get("unique_prizes", 0) or 0)
    total_found  = int(summary.get("total_found",   0) or 0)
    completion   = round(unique / total_prizes * 100, 1) if total_prizes else 0
    bar_filled   = int(completion / 10)
    progress_bar = "▰" * bar_filled + "▱" * (10 - bar_filled)

    embed = discord.Embed(
        title=target.display_name,
        color=0x5865F2,
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    # Bio
    bio = (profile.get("bio") if profile else None) or "*No bio — use `-setbio` to add one.*"
    embed.add_field(name="Bio", value=bio, inline=False)

    # Equipped prize
    equipped_name = profile.get("equipped_prize") if profile else None
    if equipped_name:
        ep = db_get_prize(equipped_name)
        if ep:
            _, _, _, _, tier = get_rarity_info(ep["chance"])
            embed.add_field(
                name="Equipped prize",
                value=f"{RARITY_EMOJI[tier]}  **{equipped_name}**  `{TIER_BADGE[tier]}`",
                inline=False,
            )

    # Showcase prize
    showcase_name = profile.get("showcase_prize") if profile else None
    if showcase_name:
        sp = db_get_prize(showcase_name)
        if sp:
            _, _, _, _, tier = get_rarity_info(sp["chance"])
            embed.add_field(
                name="Showcase prize",
                value=f"{RARITY_EMOJI[tier]}  **{showcase_name}**\n`{TIER_BADGE[tier]}`  ·  1/{sp['chance']:,}",
                inline=False,
            )
            if sp.get("image"):
                embed.set_image(url=sp["image"])

    # Stats row
    embed.add_field(name="Unique",     value=f"**{unique}**",              inline=True)
    embed.add_field(name="Total",      value=f"**{total_found}**",         inline=True)
    embed.add_field(name="Messages",   value=f"**{profile.get('total_messages', 0) if profile else 0}**", inline=True)

    # Completion bar
    embed.add_field(
        name="Collection progress",
        value=f"`{progress_bar}`  {completion}%  ({unique}/{total_prizes})",
        inline=False,
    )

    # Best prize
    best = summary.get("best_prize")
    if best:
        _, _, _, _, tier = get_rarity_info(best["chance"])
        embed.add_field(
            name="Best prize",
            value=f"{RARITY_EMOJI[tier]}  **{best['prize_name']}**  `{TIER_BADGE[tier]}`",
            inline=False,
        )

    first_find = summary.get("first_find")
    if first_find:
        embed.set_footer(text=f"First prize found  {format_dt(first_find)}")

    view = ProfileView(target, is_self, owner_id=ctx.author.id)
    await ctx.send(embed=embed, view=view)


@bot.command(name="setbio")
async def setbio_cmd(ctx: commands.Context, *, bio: str = ""):
    """Set your profile bio. Leave blank to clear it."""
    db_upsert_profile(ctx.author.id, bio=bio.strip() or None)
    msg = "✅ Bio updated." if bio.strip() else "✅ Bio cleared."
    await ctx.send(msg, delete_after=5)


@bot.command(name="showcase")
async def showcase_cmd(ctx: commands.Context):
    """Pick a prize from your inventory to display on your profile."""
    entries = db_get_inventory(ctx.author.id)
    if not entries:
        await ctx.send("You have no prizes to showcase yet."); return
    await ctx.send("Choose your showcase prize:", view=ShowcaseSelectView(list(entries)))


@bot.command(name="equip")
async def equip_cmd(ctx: commands.Context, *, prize_name: str):
    """
    Equip a prize as a nickname tag: [PrizeName] YourName
    You must own the prize first.
    """
    results = db_search_inventory(ctx.author.id, prize_name)
    exact   = next((r for r in results if r["prize_name"].lower() == prize_name.lower()), None)
    if not exact:
        exact = results[0] if results else None
    if not exact:
        await ctx.send(f"You don't own a prize matching `{prize_name}`.\nUse `-inv` to see your prizes."); return

    actual_name = exact["prize_name"]
    chance      = exact.get("chance", 1)
    _, color, _, _, tier = get_rarity_info(chance)

    tag      = f"[{actual_name}]"
    new_nick = f"{tag} {ctx.author.name}"[:32]

    try:
        await ctx.author.edit(nick=new_nick)
        db_upsert_profile(ctx.author.id, equipped_prize=actual_name)

        embed = discord.Embed(
            title="Prize equipped",
            description=f"Your nickname is now **{new_nick}**",
            color=color,
        )
        embed.add_field(name="Prize",  value=f"{RARITY_EMOJI[tier]}  **{actual_name}**", inline=True)
        embed.add_field(name="Tier",   value=f"`{TIER_BADGE[tier]}`",                    inline=True)
        embed.set_footer(text="Use -unequip to remove it")
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send("I don't have permission to change your nickname. Make sure my role is above yours in the role list.")
    except discord.HTTPException as e:
        await ctx.send(f"Failed to set nickname: {e}")


@bot.command(name="unequip")
async def unequip_cmd(ctx: commands.Context):
    """Remove your equipped prize and restore your nickname."""
    try:
        await ctx.author.edit(nick=None)
        db_upsert_profile(ctx.author.id, equipped_prize=None)
        await ctx.send("✅ Prize unequipped — nickname restored.")
    except discord.Forbidden:
        await ctx.send("I don't have permission to change your nickname.")
    except discord.HTTPException as e:
        await ctx.send(f"Failed: {e}")


@bot.command(name="compare", aliases=["vs"])
async def compare_cmd(ctx: commands.Context, member: discord.Member):
    """Compare your prize collection with another user's."""
    if member == ctx.author:
        await ctx.send("You can't compare with yourself."); return

    data = db_compare_inventories(ctx.author.id, member.id)

    embed = discord.Embed(
        title=f"{ctx.author.display_name}  vs  {member.display_name}",
        color=0x5865F2,
    )

    shared_str  = ", ".join(sorted(data["shared"]))[:1000]  or "*None*"
    only_u1_str = ", ".join(sorted(data["only_u1"]))[:1000] or "*None*"
    only_u2_str = ", ".join(sorted(data["only_u2"]))[:1000] or "*None*"

    embed.add_field(
        name=f"Both have  ({len(data['shared'])})",
        value=shared_str,
        inline=False,
    )
    embed.add_field(
        name=f"Only {ctx.author.display_name}  ({len(data['only_u1'])})",
        value=only_u1_str,
        inline=False,
    )
    embed.add_field(
        name=f"Only {member.display_name}  ({len(data['only_u2'])})",
        value=only_u2_str,
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="prizeinfo", aliases=["pi"])
async def prizeinfo_cmd(ctx: commands.Context, *, name: str):
    """Look up details on a specific prize. Partial names work."""
    prize = db_get_prize(name)
    if not prize:
        prizes  = db_load_prizes()
        matches = [p for p in prizes if name.lower() in p["name"].lower()]
        if not matches:
            await ctx.send(f"No prize found matching `{name}`."); return
        if len(matches) > 1:
            names = "  ·  ".join(f"`{p['name']}`" for p in matches[:10])
            await ctx.send(f"Multiple matches: {names}\nBe more specific."); return
        prize = matches[0]

    disc = db_get_disc(prize["name"])
    _, color, _, _, tier = get_rarity_info(prize["chance"])
    badge = TIER_BADGE[tier]
    emoji = RARITY_EMOJI[tier]

    embed = discord.Embed(
        title=f"{emoji}  {prize['name']}",
        color=color,
    )
    embed.add_field(name="Tier",     value=f"`{badge}`",                    inline=True)
    embed.add_field(name="Odds",     value=f"1 in {prize['chance']:,}",     inline=True)
    embed.add_field(name="Spectrum", value=f"`{rarity_tier_bar(prize['chance'])}`", inline=True)

    if prize.get("description"):
        embed.add_field(name="About", value=f"*{prize['description']}*", inline=False)

    if disc:
        if disc.get("discovered"):
            embed.add_field(
                name="Discovery",
                value=(
                    f"First found by **{disc.get('first_user', '—')}**\n"
                    f"{format_dt(disc.get('first_at'))}\n"
                    f"Found **{disc.get('total_found', 0)}×** total"
                ),
                inline=False,
            )
        else:
            embed.add_field(name="Status", value="*Not yet discovered by anyone.*", inline=False)

    if prize.get("image"):
        embed.set_image(url=prize["image"])
    await ctx.send(embed=embed)


@bot.command(name="recent", aliases=["feed"])
async def recent_cmd(ctx: commands.Context):
    """Live feed of the latest prize finds across the server."""
    results = db_recent_finds(10)
    if not results:
        await ctx.send("No prizes found yet."); return

    embed = discord.Embed(title="Recent finds", color=0x5865F2)
    embed.description = "The last 10 prizes rolled on this server.\n\u200b"

    for r in results:
        member = ctx.guild.get_member(r["user_id"])
        name   = member.display_name if member else f"User {r['user_id']}"
        _, _, _, _, tier = get_rarity_info(r["chance"])
        badge  = TIER_BADGE[tier]
        emoji  = RARITY_EMOJI[tier]
        embed.add_field(
            name=f"{emoji}  {r['prize_name']}",
            value=f"`{badge}`  ·  {name}  ·  {format_dt_short(r['last_found_at'])}",
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command(name="stats")
async def stats_cmd(ctx: commands.Context):
    """Server-wide statistics dashboard."""
    await ctx.send(embed=_build_stats_embed(db_stats()))


@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    """Check bot latency."""
    await ctx.send(f"Pong!  `{round(bot.latency * 1000)} ms`")


@bot.command(name="help", aliases=["h"])
async def help_cmd(ctx: commands.Context):
    """Show all available commands."""
    embed = discord.Embed(
        title="Commands",
        color=0x5865F2,
    )
    embed.description = "Prefix: `-`  ·  `[optional]`  `<required>`\n\u200b"

    # ── Collection ──
    embed.add_field(name="Collection", value="\u200b", inline=False)
    embed.add_field(
        name="`-collection`  (`-col`, `-c`)",
        value="Browse all server prizes and their discovery status.",
        inline=False,
    )
    embed.add_field(
        name="`-rarest`  (`-rare`)",
        value="Show the rarest prizes ever found here.",
        inline=False,
    )
    embed.add_field(
        name="`-recent`  (`-feed`)",
        value="Live feed of the last 10 prize finds.",
        inline=False,
    )
    embed.add_field(
        name="`-prizeinfo <name>`  (`-pi`)",
        value="Detailed info on a prize. Partial names work.",
        inline=False,
    )
    embed.add_field(
        name="`-stats`",
        value="Full server statistics dashboard.",
        inline=False,
    )

    embed.add_field(name="\u200b", value="**Inventory**", inline=False)
    embed.add_field(
        name="`-inventory [sort]`  (`-inv`, `-i`)",
        value="Your prize collection. Add `@user` to view someone else's.\nSort options: `rarity` · `quantity` · `name` · `recent` · `oldest`",
        inline=False,
    )
    embed.add_field(
        name="`-search <name>`  (`-find`, `-s`)",
        value="Search your inventory by prize name.",
        inline=False,
    )
    embed.add_field(
        name="`-compare <@user>`  (`-vs`)",
        value="See which prizes you share or don't with another user.",
        inline=False,
    )

    embed.add_field(name="\u200b", value="**Profile**", inline=False)
    embed.add_field(
        name="`-profile [@user]`  (`-p`, `-prof`)",
        value="View your profile card, or someone else's.",
        inline=False,
    )
    embed.add_field(
        name="`-setbio [text]`",
        value="Set a bio on your profile. Leave blank to clear it.",
        inline=False,
    )
    embed.add_field(
        name="`-showcase`",
        value="Pin a prize to the front of your profile.",
        inline=False,
    )
    embed.add_field(
        name="`-equip <prize name>`",
        value="Wear a prize as a nickname tag: `[PrizeName] YourName`",
        inline=False,
    )
    embed.add_field(
        name="`-unequip`",
        value="Remove your equipped prize and restore your nickname.",
        inline=False,
    )

    embed.add_field(name="\u200b", value="**Leaderboard**", inline=False)
    embed.add_field(
        name="`-leaderboard [type]`  (`-lb`, `-top`)",
        value="Server rankings. Types: `total` · `unique` · `rarity`",
        inline=False,
    )

    embed.add_field(name="\u200b", value="**Misc**", inline=False)
    embed.add_field(name="`-ping`", value="Check bot latency.", inline=False)

    if is_admin(ctx.author):
        embed.add_field(name="\u200b", value="**Admin**", inline=False)
        embed.add_field(
            name="`-prizemaker`  (`-pm`)",
            value="Open the prize management panel.",
            inline=False,
        )

    embed.set_footer(text="Every message gives you a chance to win a prize — just keep chatting!")
    await ctx.send(embed=embed)


# ─── Run ──────────────────────────────────────────────────────────────────────

bot.run(TOKEN)
