import os
import discord
from discord.ext import commands, tasks
from flask import Flask
from threading import Thread

# --- Flask Web Server (To keep Render alive) ---
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

bot = commands.Bot(command_prefix='!', intents=intents)

# --- Rainbow Role Configuration ---
# List of hex colors for the rainbow
rainbow_colors = [0xFF0000, 0xFF7F00, 0xFFFF00, 0x00FF00, 0x0000FF, 0x4B0082, 0x8F00FF]
color_index = 0
TARGET_ROLE_ID = 920309927375933490

@tasks.loop(minutes=10)
async def rainbow_role_loop():
    global color_index
    # Iterate through all guilds the bot is in
    for guild in bot.guilds:
        role = guild.get_role(TARGET_ROLE_ID)
        
        if role:
            try:
                await role.edit(color=discord.Color(rainbow_colors[color_index]))
                print(f"Changed {role.name} to color {rainbow_colors[color_index]}")
            except discord.Forbidden:
                print(f"Missing permissions to edit role in {guild.name}")
    
    # Move to the next color
    color_index = (color_index + 1) % len(rainbow_colors)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    # Start the loop
    if not rainbow_role_loop.is_running():
        rainbow_role_loop.start()

# --- Commands ---
@bot.command()
async def changecolor(ctx, role_name: str, color_hex: str):
    try:
        clean_hex = color_hex.replace('0x', '').replace('#', '')
        color = discord.Color(int(clean_hex, 16))
    except ValueError:
        await ctx.send("Invalid color format. Please use hex (e.g., 0xFF0000).")
        return

    role = discord.utils.get(ctx.guild.roles, name=role_name)
    
    if role:
        try:
            await role.edit(color=color)
            await ctx.send(f'Successfully changed {role.name} color!')
        except discord.Forbidden:
            await ctx.send("I don't have permission to edit that role. Check my role hierarchy.")
    else:
        await ctx.send(f"Could not find a role named '{role_name}'.")

@bot.command()
async def ping(ctx):
    await ctx.send('Pong!')

# Run the bot
bot.run(TOKEN)
