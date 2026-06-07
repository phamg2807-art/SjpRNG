import os
import discord
import random
import json
import asyncio
from discord.ext import commands
from flask import Flask
from threading import Thread

# --- Flask Web Server ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

t = Thread(target=run_flask)
t.start()

# --- Discord Bot Setup ---
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    print("ERROR: DISCORD_TOKEN is missing!")
    exit(1)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=True)

# --- Constants ---
ADMIN_ROLE_ID = 920309927375933490
ANNOUNCEMENT_CHANNEL_ID = 900015775069401128
PRIZES_FILE = "prizes.json"

# --- Prize Storage ---
def load_prizes():
    try:
        with open(PRIZES_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_prizes(prizes):
    with open(PRIZES_FILE, "w") as f:
        json.dump(prizes, f, indent=2)

# --- Rarity Config ---
def get_rarity_info(chance: int):
    """Returns (label, embed_color, ping_everyone, send_announcement, special_effect)"""
    if chance >= 1_000_000_000:
        return ("★ MYTHIC", 0x000000, True, True, "mythic")
    elif chance >= 100_000_000:
        return ("✦ LEGENDARY", 0xFF1493, True, False, "legendary")
    elif chance >= 10_000_000:
        return ("◆ EPIC+", 0x00008B, False, False, "dark_blue")
    elif chance >= 1_000_000:
        return ("◇ EPIC", 0xFF69B4, False, False, "pink")
    elif chance >= 100_000:
        return ("● RARE+", 0x00FFFF, False, False, "cyan")
    elif chance >= 10_000:
        return ("○ RARE", 0xFFA500, False, False, "orange")
    else:
        return ("· COMMON", 0xAAAAAA, False, False, "normal")

def get_rarity_color_hex(chance: int) -> int:
    return get_rarity_info(chance)[1]

def get_rarity_label(chance: int) -> str:
    return get_rarity_info(chance)[0]

# --- Admin Check ---
def is_admin(member: discord.Member) -> bool:
    return any(r.id == ADMIN_ROLE_ID for r in member.roles) or member.guild_permissions.administrator

# --- Prize Maker View ---
class PrizeMakerModal(discord.ui.Modal, title="🎁 Create a New Prize"):
    prize_name = discord.ui.TextInput(
        label="Prize Name",
        placeholder="e.g. Golden Crown",
        max_length=100
    )
    image_url = discord.ui.TextInput(
        label="Image URL (optional)",
        placeholder="https://i.imgur.com/example.png",
        required=False,
        max_length=500
    )
    chance = discord.ui.TextInput(
        label="Chance (1 in X) — e.g. 1000 for 1/1000",
        placeholder="1000",
        max_length=12
    )
    roll_message = discord.ui.TextInput(
        label="Roll Message",
        placeholder="{user} has rolled {prize}! 🎉",
        max_length=300,
        style=discord.TextStyle.paragraph
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            chance_val = int(self.chance.value.strip())
            if chance_val < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Chance must be a positive integer (e.g. 1000).", ephemeral=True)
            return

        prize_data = {
            "name": self.prize_name.value.strip(),
            "image": self.image_url.value.strip() if self.image_url.value else None,
            "chance": chance_val,
            "roll_message": self.roll_message.value.strip()
        }

        prizes = load_prizes()
        prizes.append(prize_data)
        save_prizes(prizes)

        rarity_label, embed_color, _, _, _ = get_rarity_info(chance_val)

        embed = discord.Embed(
            title="✅ Prize Created!",
            color=embed_color
        )
        embed.add_field(name="Prize Name", value=prize_data["name"], inline=True)
        embed.add_field(name="Rarity", value=f"{rarity_label} (1/{chance_val:,})", inline=True)
        embed.add_field(name="Roll Message", value=prize_data["roll_message"], inline=False)

        if prize_data["image"]:
            embed.set_thumbnail(url=prize_data["image"])

        # Preview
        preview_msg = prize_data["roll_message"].replace("{user}", interaction.user.mention).replace("{prize}", f"**{prize_data['name']}**")
        embed.add_field(name="📋 Message Preview", value=preview_msg, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)


class PrizeMakerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Add Prize", style=discord.ButtonStyle.success)
    async def add_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
            return
        await interaction.response.send_modal(PrizeMakerModal())

    @discord.ui.button(label="📋 List Prizes", style=discord.ButtonStyle.primary)
    async def list_prizes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
            return
        prizes = load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes yet! Add some first.", ephemeral=True)
            return

        embed = discord.Embed(title="🎁 Prize List", color=0x7289DA)
        for i, p in enumerate(prizes):
            rarity_label = get_rarity_label(p["chance"])
            embed.add_field(
                name=f"#{i+1} — {p['name']}",
                value=f"Rarity: {rarity_label} (1/{p['chance']:,})",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🗑️ Delete Prize", style=discord.ButtonStyle.danger)
    async def delete_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
            return
        prizes = load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes to delete.", ephemeral=True)
            return

        options = [
            discord.SelectOption(
                label=f"#{i+1} — {p['name']} (1/{p['chance']:,})",
                value=str(i),
                description=get_rarity_label(p["chance"])
            )
            for i, p in enumerate(prizes[:25])
        ]

        view = DeleteSelectView(options)
        await interaction.response.send_message("Select a prize to delete:", view=view, ephemeral=True)

    @discord.ui.button(label="👁️ Preview Roll", style=discord.ButtonStyle.secondary)
    async def preview_roll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
            return
        prizes = load_prizes()
        if not prizes:
            await interaction.response.send_message("No prizes to preview.", ephemeral=True)
            return

        options = [
            discord.SelectOption(
                label=f"#{i+1} — {p['name']} (1/{p['chance']:,})",
                value=str(i),
                description=get_rarity_label(p["chance"])
            )
            for i, p in enumerate(prizes[:25])
        ]

        view = PreviewSelectView(options)
        await interaction.response.send_message("Select a prize to preview:", view=view, ephemeral=True)


class DeleteSelectView(discord.ui.View):
    def __init__(self, options):
        super().__init__(timeout=60)
        self.select = discord.ui.Select(placeholder="Choose prize to delete...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        idx = int(self.select.values[0])
        prizes = load_prizes()
        if idx >= len(prizes):
            await interaction.response.send_message("Prize not found.", ephemeral=True)
            return
        removed = prizes.pop(idx)
        save_prizes(prizes)
        await interaction.response.send_message(f"🗑️ Deleted prize: **{removed['name']}**", ephemeral=True)


class PreviewSelectView(discord.ui.View):
    def __init__(self, options):
        super().__init__(timeout=60)
        self.select = discord.ui.Select(placeholder="Choose prize to preview...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        idx = int(self.select.values[0])
        prizes = load_prizes()
        if idx >= len(prizes):
            await interaction.response.send_message("Prize not found.", ephemeral=True)
            return
        prize = prizes[idx]
        embed = build_roll_embed(prize, interaction.user)
        await interaction.response.send_message(content="**Preview:**", embed=embed, ephemeral=True)


# --- Build Roll Embed ---
def build_roll_embed(prize: dict, user: discord.Member) -> discord.Embed:
    chance = prize["chance"]
    rarity_label, embed_color, _, _, effect = get_rarity_info(chance)

    roll_msg = prize["roll_message"].replace("{user}", user.mention).replace("{prize}", f"**{prize['name']}**")

    # Title effect based on rarity
    if effect == "mythic":
        title = f"🌑 ═══ MYTHIC ROLL ═══ 🌑"
    elif effect == "legendary":
        title = f"🌸 ═══ LEGENDARY ROLL ═══ 🌸"
    elif effect == "dark_blue":
        title = f"💙 ═══ EPIC+ ROLL ═══ 💙"
    elif effect == "pink":
        title = f"💗 ═══ EPIC ROLL ═══ 💗"
    elif effect == "cyan":
        title = f"🩵 ═══ RARE+ ROLL ═══ 🩵"
    elif effect == "orange":
        title = f"🟠 ═══ RARE ROLL ═══ 🟠"
    else:
        title = f"🎲 You rolled!"

    embed = discord.Embed(
        title=title,
        description=roll_msg,
        color=embed_color
    )
    embed.add_field(name="Prize", value=prize["name"], inline=True)
    embed.add_field(name="Rarity", value=f"{rarity_label}\n1/{chance:,}", inline=True)
    embed.set_footer(text=f"Rolled by {user.display_name}", icon_url=user.display_avatar.url)

    if prize.get("image"):
        embed.set_image(url=prize["image"])

    return embed


# --- on_message Event: Rolling ---
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    prizes = load_prizes()
    if prizes:
        for prize in prizes:
            roll = random.randint(1, prize["chance"])
            if roll == 1:
                embed = build_roll_embed(prize, message.author)
                _, _, ping_everyone, send_announcement, _ = get_rarity_info(prize["chance"])

                content = ""
                if ping_everyone:
                    content = "@everyone"

                await message.channel.send(content=content, embed=embed)

                # Send to announcement channel for mythic
                if send_announcement:
                    ann_channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
                    if ann_channel:
                        await ann_channel.send(content="@everyone 🌑 **A MYTHIC prize has been rolled!**", embed=embed)

                break  # Only one prize per message

    await bot.process_commands(message)


# --- Commands ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')


@bot.command()
async def prizemaker(ctx):
    """Open the prize maker panel (admin only)"""
    if not is_admin(ctx.author):
        await ctx.send("❌ You don't have permission to use this command.")
        return

    embed = discord.Embed(
        title="🎁 Prize Maker Panel",
        description=(
            "Welcome to the Prize Maker! Use the buttons below to manage prizes.\n\n"
            "**Rarity Tiers:**\n"
            "· Common: 1/1,000+\n"
            "○ Rare: 1/10,000+\n"
            "● Rare+: 1/100,000+\n"
            "◇ Epic: 1/1,000,000+\n"
            "◆ Epic+: 1/10,000,000+\n"
            "✦ Legendary: 1/100,000,000+ *(pings @everyone)*\n"
            "★ Mythic: 1/1,000,000,000+ *(pings @everyone + announcement)*"
        ),
        color=0x7289DA
    )
    embed.set_footer(text="Only admins can manage prizes.")

    view = PrizeMakerView()
    await ctx.send(embed=embed, view=view)


@bot.command()
async def ping(ctx):
    await ctx.send('Pong!')


bot.run(TOKEN)
