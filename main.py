import os
import discord
import random
from discord import app_commands
from discord.ext import commands
from flask import Flask
from threading import Thread

# --- Flask Web Server (Keeps the bot awake) ---
app = Flask('')
@app.route('/')
def home(): return "Bot is alive!"
def run_flask(): app.run(host='0.0.0.0', port=8080)
Thread(target=run_flask).start()

# --- Bot Setup ---
TOKEN = os.getenv('DISCORD_TOKEN')
ADMIN_ROLE_ID = 920309927375933490
ANNOUNCE_CHANNEL_ID = 900015775069401128

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# In-memory storage for prizes
prizes = []

# --- Rarity Logic ---
def get_rarity_settings(chance):
    # Returns: (Color, PingEveryone, SpecialChannel)
    if chance >= 1000000000: 
        return discord.Color.from_str("#000000"), True, True
    if chance >= 100000000: 
        return discord.Color.from_str("#FF69B4"), True, False
    if chance >= 10000000: 
        return discord.Color.blue(), False, False
    if chance >= 1000000: 
        return discord.Color.from_str("#FFC0CB"), False, False
    if chance >= 100000: 
        return discord.Color.teal(), False, False
    if chance >= 10000: 
        return discord.Color.orange(), False, False
    return discord.Color.light_gray(), False, False

# --- Admin Prize Modal ---
class PrizeModal(discord.ui.Modal, title='Create New Prize'):
    name = discord.ui.TextInput(label='Prize Name', style=discord.TextStyle.short)
    image_url = discord.ui.TextInput(label='Image URL', style=discord.TextStyle.short)
    chance = discord.ui.TextInput(label='Chance (e.g. 1000)', style=discord.TextStyle.short)
    instruction = discord.ui.TextInput(label='Instructions (Use {user} and {prize})', style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            c = int(self.chance.value)
            prizes.append({
                "name": self.name.value,
                "image": self.image_url.value,
                "chance": c,
                "instruction": self.instruction.value
            })
            await interaction.response.send_message(f"✅ Prize '{self.name.value}' created (1/{c})!", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Chance must be a number!", ephemeral=True)

# --- Commands ---
@bot.event
async def on_ready():
    await bot.tree.sync() # Syncs slash commands
    print(f'Logged in as {bot.user.name} and commands synced!')

@bot.tree.command(name="createprize", description="Admin only: Create a prize")
async def createprize(interaction: discord.Interaction):
    # Check Admin Role
    if any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_modal(PrizeModal())
    else:
        await interaction.response.send_message("❌ You don't have permission!", ephemeral=True)

@bot.tree.command(name="ping", description="Check bot status")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

# --- RNG Logic ---
@bot.event
async def on_message(message):
    if message.author.bot: return

    for prize in prizes:
        if random.randint(1, prize['chance']) == 1:
            color, ping, special = get_rarity_settings(prize['chance'])
            
            embed = discord.Embed(
                title="🎉 Prize Rolled!",
                description=prize['instruction'].format(user=message.author.mention, prize=prize['name']),
                color=color
            )
            embed.set_image(url=prize['image'])
            
            # Determine channel and content
            target_channel = bot.get_channel(ANNOUNCE_CHANNEL_ID) if (special and bot.get_channel(ANNOUNCE_CHANNEL_ID)) else message.channel
            content = "@everyone" if ping else None
            
            await target_channel.send(content=content, embed=embed)
            break 

bot.run(TOKEN)
