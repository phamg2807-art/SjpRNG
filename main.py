import os
import discord
import random
import json
from discord.ext import commands
from flask import Flask
from threading import Thread

# ─── Flask Keep-Alive ────────────────────────────────────────────────────────

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_flask, daemon=True).start()

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

ADMIN_ROLE_ID          = 920309927375933490
ANNOUNCEMENT_CHANNEL_ID = 900015775069401128
PRIZES_FILE            = "prizes.json"

# ─── Prize Storage ────────────────────────────────────────────────────────────

def load_prizes() -> list:
    try:
        with open(PRIZES_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_prizes(prizes: list) -> None:
    with open(PRIZES_FILE, "w") as f:
        json.dump(prizes, f, indent=2)

# ─── Rarity System ───────────────────────────────────────────────────────────
# Returns (label, embed_color, ping_everyone, send_announcement, effect_key)

def get_rarity_info(chance: int) -> tuple:
    if chance >= 1_000_000_000:
        return ("★ MYTHIC",    0x111111, True,  True,  "mythic")
    if chance >= 100_000_000:
        return ("✦ LEGENDARY", 0xFF1493, True,  False, "legendary")
    if chance >= 10_000_000:
        return ("◆ EPIC+",     0x00008B, False, False, "dark_blue")
    if chance >= 1_000_000:
        return ("◇ EPIC",      0xFF69B4, False, False, "pink")
    if chance >= 100_000:
        return ("● RARE+",     0x00CED1, False, False, "cyan")
    if chance >= 10_000:
        return ("○ RARE",      0xFFA500, False, False, "orange")
    return     ("· COMMON",    0xAAAAAA, False, False, "normal")

EFFECT_TITLES = {
    "mythic":    "🌑 ══════ MYTHIC ROLL ══════ 🌑",
    "legendary": "🌸 ════ LEGENDARY ROLL ════ 🌸",
    "dark_blue": "💙 ══════ EPIC+ ROLL ══════ 💙",
    "pink":      "💗 ══════ EPIC ROLL ═══════ 💗",
    "cyan":      "🩵 ══════ RARE+ ROLL ══════ 🩵",
    "orange":    "🟠 ══════ RARE ROLL ═══════ 🟠",
    "normal":    "🎲 You got a roll!",
}

# ─── Admin Check ─────────────────────────────────────────────────────────────

def is_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.id == ADMIN_ROLE_ID for role in member.roles)

# ─── Build Roll Embed ─────────────────────────────────────────────────────────

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
    embed.add_field(name="🎁 Prize",  value=prize["name"],          inline=True)
    embed.add_field(name="✨ Rarity", value=f"{label}\n1/{chance:,}", inline=True)
    embed.set_footer(
        text=f"Rolled by {user.display_name}",
        icon_url=user.display_avatar.url,
    )
    if prize.get("image"):
        embed.set_image(url=prize["image"])
    return embed

# ─── Prize Maker Modal ───────────────────────────────────────────────────────

class PrizeMakerModal(discord.ui.Modal, title="🎁 Create a New Prize"):
    prize_name = discord.ui.TextInput(
        label="Prize Name",
        placeholder="e.g. Golden Crown",
        max_length=100,
    )
    image_url = discord.ui.TextInput(
        label="Image URL (optional)",
        placeholder="https://i.imgur.com/example.png",
        required=False,
        max_length=500,
    )
    chance = discord.ui.TextInput(
        label="Chance (1 in X)  —  e.g. 1000 means 1/1000",
        placeholder="1000",
        max_length=12,
    )
    roll_message = discord.ui.TextInput(
        label="Roll Message  ({user} and {prize} supported)",
        placeholder="{user} has rolled {prize}! 🎉",
        max_length=300,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Validate chance
        try:
            chance_val = int(self.chance.value.strip())
            if chance_val < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Chance must be a positive whole number (e.g. `1000`).",
                ephemeral=True,
            )
            return

        prize_data = {
            "name":         self.prize_name.value.strip(),
            "image":        self.image_url.value.strip() or None,
            "chance":       chance_val,
            "roll_message": self.roll_message.value.strip(),
        }

        prizes = load_prizes()
        prizes.append(prize_data)
        save_prizes(prizes)

        label, color, ping_e, announce, _ = get_rarity_info(chance_val)

        embed = discord.Embed(title="✅ Prize Created!", color=color)
        embed.add_field(name="Name",   value=prize_data["name"],            inline=True)
        embed.add_field(name="Rarity", value=f"{label}  (1/{chance_val:,})", inline=True)
        embed.add_field(name="Roll Message", value=prize_data["roll_message"], inline=False)

        preview = (
            prize_data["roll_message"]
            .replace("{user}", interaction.user.mention)
            .replace("{prize}", f"**{prize_data['name']}**")
        )
        embed.add_field(name="📋 Preview", value=preview, inline=False)

        flags = []
        if ping_e:   flags.append("Pings @everyone")
        if announce: flags.append("Sends to Announcement channel")
        if flags:
            embed.add_field(name="⚠️ Special Effects", value=" • ".join(flags), inline=False)

        if prize_data["image"]:
            embed.set_thumbnail(url=prize_data["image"])

        await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── Delete Select ───────────────────────────────────────────────────────────

class DeleteSelect(discord.ui.Select):
    def __init__(self, prizes: list):
        options = [
            discord.SelectOption(
                label=f"#{i+1} — {p['name']}",
                description=f"{get_rarity_info(p['chance'])[0]}  (1/{p['chance']:,})",
                value=str(i),
            )
            for i, p in enumerate(prizes[:25])
        ]
        super().__init__(placeholder="Choose a prize to delete…", options=options)

    async def callback(self, interaction: discord.Interaction):
        idx    = int(self.values[0])
        prizes = load_prizes()
        if idx >= len(prizes):
            await interaction.response.send_message("❌ Prize not found.", ephemeral=True)
            return
        removed = prizes.pop(idx)
        save_prizes(prizes)
        await interaction.response.send_message(
            f"🗑️ Deleted **{removed['name']}**.", ephemeral=True
        )


class DeleteSelectView(discord.ui.View):
    def __init__(self, prizes: list):
        super().__init__(timeout=60)
        self.add_item(DeleteSelect(prizes))


# ─── Preview Select ───────────────────────────────────────────────────────────

class PreviewSelect(discord.ui.Select):
    def __init__(self, prizes: list):
        options = [
            discord.SelectOption(
                label=f"#{i+1} — {p['name']}",
                description=f"{get_rarity_info(p['chance'])[0]}  (1/{p['chance']:,})",
                value=str(i),
            )
            for i, p in enumerate(prizes[:25])
        ]
        super().__init__(placeholder="Choose a prize to preview…", options=options)

    async def callback(self, interaction: discord.Interaction):
        idx    = int(self.values[0])
        prizes = load_prizes()
        if idx >= len(prizes):
            await interaction.response.send_message("❌ Prize not found.", ephemeral=True)
            return
        embed = build_roll_embed(prizes[idx], interaction.user)
        await interaction.response.send_message(content="**🔍 Preview:**", embed=embed, ephemeral=True)


class PreviewSelectView(discord.ui.View):
    def __init__(self, prizes: list):
        super().__init__(timeout=60)
        self.add_item(PreviewSelect(prizes))


# ─── Prize Maker Panel View ───────────────────────────────────────────────────

class PrizeMakerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # ── Add ──────────────────────────────────────────────────────────────────
    @discord.ui.button(label="➕ Add Prize", style=discord.ButtonStyle.success, row=0)
    async def add_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return
        await interaction.response.send_modal(PrizeMakerModal())

    # ── List ─────────────────────────────────────────────────────────────────
    @discord.ui.button(label="📋 List Prizes", style=discord.ButtonStyle.primary, row=0)
    async def list_prizes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return
        prizes = load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes yet — add some first!", ephemeral=True)
            return
        embed = discord.Embed(title="🎁 All Prizes", color=0x7289DA)
        for i, p in enumerate(prizes):
            label = get_rarity_info(p["chance"])[0]
            embed.add_field(
                name=f"#{i+1}  {p['name']}",
                value=f"{label}  •  1/{p['chance']:,}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Delete ────────────────────────────────────────────────────────────────
    @discord.ui.button(label="🗑️ Delete Prize", style=discord.ButtonStyle.danger, row=0)
    async def delete_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return
        prizes = load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes to delete.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select a prize to delete:", view=DeleteSelectView(prizes), ephemeral=True
        )

    # ── Preview ───────────────────────────────────────────────────────────────
    @discord.ui.button(label="👁️ Preview Roll", style=discord.ButtonStyle.secondary, row=0)
    async def preview_roll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return
        prizes = load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes to preview.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select a prize to preview:", view=PreviewSelectView(prizes), ephemeral=True
        )


# ─── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   Loaded {len(load_prizes())} prize(s) from {PRIZES_FILE}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Roll every prize independently each message
    prizes = load_prizes()
    for prize in prizes:
        if random.randint(1, prize["chance"]) == 1:
            _, _, ping_everyone, send_announcement, _ = get_rarity_info(prize["chance"])
            embed   = build_roll_embed(prize, message.author)
            content = "@everyone" if ping_everyone else ""

            await message.channel.send(content=content, embed=embed)

            if send_announcement:
                ann_ch = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
                if ann_ch:
                    await ann_ch.send(
                        content="@everyone 🌑 **A MYTHIC prize has just been rolled!**",
                        embed=embed,
                    )
            break  # Only one prize fires per message

    await bot.process_commands(message)


# ─── Commands ─────────────────────────────────────────────────────────────────

@bot.command()
async def prizemaker(ctx: commands.Context):
    """Open the Prize Maker panel (admin only)."""
    if not is_admin(ctx.author):
        await ctx.send("❌ You don't have permission to use this command.")
        return

    embed = discord.Embed(
        title="🎁 Prize Maker",
        description=(
            "Manage all prizes from the buttons below.\n\n"
            "**Rarity Tiers**\n"
            "· Common   — 1/1,000+\n"
            "○ Rare     — 1/10,000+\n"
            "● Rare+    — 1/100,000+\n"
            "◇ Epic     — 1/1,000,000+\n"
            "◆ Epic+    — 1/10,000,000+\n"
            "✦ Legendary — 1/100,000,000+  *(pings @everyone)*\n"
            "★ Mythic    — 1/1,000,000,000+  *(pings @everyone + Announcement)*"
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="Admin-only panel")
    await ctx.send(embed=embed, view=PrizeMakerView())


@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send(f"🏓 Pong! Latency: `{round(bot.latency * 1000)}ms`")


# ─── Run ──────────────────────────────────────────────────────────────────────

bot.run(TOKEN)
