import os
import discord
import random
from discord.ext import commands
from flask import Flask
from threading import Thread

# --- Flask Server (Keep alive) ---
app = Flask('')
@app.route('/')
def home(): return "Bot is alive!"
def run_flask(): app.run(host='0.0.0.0', port=8080)
Thread(target=run_flask).start()

# --- Setup ---
TOKEN = os.getenv('DISCORD_TOKEN')
ADMIN_ROLE_ID = 920309927375933490
ANNOUNCEMENT_CHANNEL_ID = 900015775069401128

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# In-memory storage (Resets on restart)
prizes = []

# --- Rarity Logic ---
def get_rarity_settings(chance):
    # chance is the denominator (e.g., 1000 for 1/1000)
    if chance >= 1000000000: return {"color": discord.Color.default(), "ping": True, "special": True} # "Black" (Default is dark)
    if chance >= 100000000: return {"color": discord.Color.from_str("#FF69B4"), "ping": True, "special": False} # Dark Pink
    if chance >= 10000000: return {"color": discord.Color.blue(), "ping": True, "special": False}
    if chance >= 1000000: return {"color": discord.Color.from_str("#FFC0CB"), "ping": False, "special": False} # Pink
    if chance >= 100000: return {"color": discord.Color.teal(), "ping": False, "special": False} # Cyan
    if chance >= 10000: return {"color": discord.Color.orange(), "ping": False, "special": False}
    return {"color": discord.Color.light_gray(), "ping": False, "special": False}

# --- Admin Prize Creation UI ---
class PrizeModal(discord.ui.Modal, title='Create New Prize'):
    name = discord.ui.TextInput(label='Prize Name', style=discord.TextStyle.short)
    image_url = discord.ui.TextInput(label='Image URL', style=discord.TextStyle.short)
    chance = discord.ui.TextInput(label='Chance (e.g. 1000 for 1/1000)', style=discord.TextStyle.short)
    instruction = discord.ui.TextInput(label='Instructions (Use {user} and {prize})', style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            c = int(self.chance.value)
            new_prize = {
                "name": self.name.value,
                "image": self.image_url.value,
                "chance": c,
                "instruction": self.instruction.value
            }
            prizes.append(new_prize)
            await interaction.response.send_message(f"Prize '{self.name.value}' created! (Chance: 1/{c})", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Chance must be a number!", ephemeral=True)

# --- Commands ---
@bot.command()
async def createprize(ctx):
    # Check for Admin Role
    if not any(role.id == ADMIN_ROLE_ID for role in ctx.author.roles):
        return await ctx.send("You don't have permission to create prizes.")
    await ctx.send_modal(PrizeModal())

@bot.event
async def on_message(message):
    if message.author.bot: return
    
    # RNG Logic
    for prize in prizes:
        if random.randint(1, prize['chance']) == 1:
            rarity = get_rarity_settings(prize['chance'])
            
            # Build Embed
            embed = discord.Embed(
                title="🎉 Prize Rolled!",
                description=prize['instruction'].format(user=message.author.mention, prize=prize['name']),
                color=rarity['color']
            )
            embed.set_image(url=prize['image'])
            
            # Logic for Pings and Channels
            content = "@everyone" if rarity['ping'] else None
            channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID) if rarity['special'] else message.channel
            
            await channel.send(content=content, embed=embed)
            break # One prize per message max
            
    await bot.process_commands(message)

bot.run(TOKEN)
