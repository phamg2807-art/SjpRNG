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
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor, sslmode='require')

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
                    UNIQUE(user_id, prize_name)
                );
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
            disc = None
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
                INSERT INTO inventory (user_id, prize_name, quantity, first_found_at)
                VALUES (%s, %s, 1, %s)
                ON CONFLICT (user_id, prize_name)
                DO UPDATE SET quantity = inventory.quantity + 1
            """, (user_id, prize_name, now))
        conn.commit()

def db_get_inventory(user_id: int) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM inventory WHERE user_id=%s ORDER BY quantity DESC
            """, (user_id,))
            return cur.fetchall()

def db_search_inventory(user_id: int, query: str) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM inventory
                WHERE user_id=%s AND prize_name ILIKE %s
                ORDER BY quantity DESC
            """, (user_id, f'%{query}%'))
            return cur.fetchall()

def db_leaderboard() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, SUM(quantity) as total
                FROM inventory
                GROUP BY user_id
                ORDER BY total DESC
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
    return {
        "total_prizes": total_prizes,
        "discovered": discovered,
        "undiscovered": undiscovered,
        "total_found": total_found,
        "inv_entries": inv_entries,
    }

# ─── Bot Setup ────────────────────────────────────────────────────────────────

TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable is missing!")
    exit(1)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

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
    embed.add_field(name="🎁 Prize",  value=prize["name"],            inline=True)
    embed.add_field(name="✨ Rarity", value=f"{label}\n1/{chance:,}", inline=True)
    embed.add_field(name="🍀 Luck",   value=luck_description(chance), inline=True)
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
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ─── Inventory Pagination ─────────────────────────────────────────────────────

class InventoryView(discord.ui.View):
    def __init__(self, entries: list, user: discord.Member, page: int = 0):
        super().__init__(timeout=120)
        self.entries = entries
        self.user    = user
        self.page    = page
        self.per     = 6
        self.pages   = max(1, (len(entries) + self.per - 1) // self.per)
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def build_embed(self) -> discord.Embed:
        chunk = self.entries[self.page * self.per:(self.page + 1) * self.per]
        embed = discord.Embed(
            title=f"🎒 {self.user.display_name}'s Inventory",
            color=0x5865F2,
            description=f"Page {self.page+1}/{self.pages}  •  {len(self.entries)} unique prize(s)",
        )
        for entry in chunk:
            prize = db_get_prize(entry["prize_name"])
            label = get_rarity_info(prize["chance"])[0] if prize else "Unknown"
            embed.add_field(
                name=entry["prize_name"],
                value=(
                    f"{label}  •  Found **{entry['quantity']}x**\n"
                    f"First: {format_dt(entry.get('first_found_at'))}"
                ),
                inline=True,
            )
        embed.set_thumbnail(url=self.user.display_avatar.url)
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1; self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
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
        embed = discord.Embed(
            title="🗂️ Server Collection",
            description=f"**{discovered}/{total}** prizes discovered  •  Page {self.page+1}/{self.pages}",
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
    disc_docs  = {d["prize_name"]: d for d in db_get_collection()}
    view = CollectionView(list(prizes), disc_docs)
    await ctx.send(embed=view.build_embed(), view=view)


@bot.command(name="inventory", aliases=["inv", "i"])
async def inventory_cmd(ctx: commands.Context, member: discord.Member = None):
    target  = member or ctx.author
    entries = db_get_inventory(target.id)
    if not entries:
        name = "You have" if target == ctx.author else f"{target.display_name} has"
        await ctx.send(f"{name} no prizes yet!"); return
    view = InventoryView(list(entries), target)
    await ctx.send(embed=view.build_embed(), view=view)


@bot.command(name="search", aliases=["find", "s"])
async def search_cmd(ctx: commands.Context, *, query: str):
    results = db_search_inventory(ctx.author.id, query)
    if not results:
        await ctx.send(f"❌ No prizes matching `{query}` in your inventory."); return
    embed = discord.Embed(title=f"🔍 Search: '{query}'", color=0x5865F2)
    for entry in list(results)[:10]:
        prize = db_get_prize(entry["prize_name"])
        label = get_rarity_info(prize["chance"])[0] if prize else "Unknown"
        disc  = db_get_disc(entry["prize_name"])
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
async def leaderboard_cmd(ctx: commands.Context):
    results = db_leaderboard()
    if not results:
        await ctx.send("No one has found any prizes yet!"); return
    embed  = discord.Embed(title="🏆 Prize Leaderboard", color=0xFFD700)
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    for i, r in enumerate(results):
        member = ctx.guild.get_member(r["user_id"])
        name   = member.display_name if member else f"User {r['user_id']}"
        embed.add_field(
            name=f"{medals[i]} #{i+1} — {name}",
            value=f"**{r['total']}** prize(s) found",
            inline=False,
        )
    await ctx.send(embed=embed)


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


@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    await ctx.send(f"🏓 Pong! Latency: `{round(bot.latency * 1000)}ms`")


@bot.command(name="help", aliases=["h"])
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(title="📖 SjpRNG Commands", color=0x5865F2)
    embed.add_field(name="!collection  (or !col, !c)",  value="Server prize collection — ??? for undiscovered", inline=False)
    embed.add_field(name="!inventory   (or !inv, !i)",  value="Your prizes — add @user to see theirs",          inline=False)
    embed.add_field(name="!search <query>  (or !find)", value="Search your inventory",                          inline=False)
    embed.add_field(name="!leaderboard (or !lb, !top)", value="Top prize finders",                              inline=False)
    embed.add_field(name="!rarest      (or !rare)",     value="Rarest discovered prizes",                       inline=False)
    embed.add_field(name="!ping",                       value="Check bot latency",                              inline=False)
    if is_admin(ctx.author):
        embed.add_field(name="━━━ Admin ━━━",           value="\u200b",                                         inline=False)
        embed.add_field(name="!prizemaker  (or !pm)",   value="Prize management panel",                         inline=False)
    await ctx.send(embed=embed)


# ─── Run ──────────────────────────────────────────────────────────────────────

bot.run(TOKEN)
