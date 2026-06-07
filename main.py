import os
import discord
from discord.ext import commands
from flask import Flask
from threading import Thread

# --- Flask Web Server (To keep Render alive) ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

# Start the web server in a separate thread
t = Thread(target=run_flask)
t.start()

# --- Discord Bot Setup ---
# Fetch the token from Render's Environment Variables
TOKEN = os.getenv('DISCORD_TOKEN')

# Safety check for missing token
if not TOKEN:
    print("ERROR: DISCORD_TOKEN is missing! Check your Render Environment Variables.")
    exit(1)

# Set up intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    print('------')

@bot.command()
async def changecolor(ctx, role_name: str, color_hex: str):
    """
    Usage: !changecolor "Role Name" 0xFF0000
    Changes a role's color using a hex code.
    """
    # Convert hex string (e.g., 'FF0000') to discord.Color
    try:
        # Strip '0x' or '#' if user includes it
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
