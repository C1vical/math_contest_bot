import asyncio
import logging
import os
import threading

import discord
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask

from constants import RENDERS_DIR
from database import get_problem

# Render Keep Active
app = Flask(__name__)

@app.route("/")
def health_check():
    return "Bot is alive!", 200

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Start Flask in a background daemon thread
threading.Thread(target=run_http_server, daemon=True).start()

# Discord Bot Configuration
load_dotenv()
token = os.getenv("DISCORD_TOKEN")
channel_id = int(os.getenv("CHANNEL_ID"))

# Set up logging to stream directly to standard console for Render dashboard
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

def in_channel(ctx):
    return ctx.channel.id == channel_id

@bot.event
async def on_ready():
    channel = bot.get_channel(channel_id)
    await channel.send("I'm ready!")

@bot.command()
@commands.check(in_channel)
async def hi(ctx):
    await ctx.send(f"Hey {ctx.author.mention}! Welcome to MCC!")

@bot.command()
@commands.check(in_channel)
async def repeat(ctx, *, arg):
    await ctx.send(arg)

@bot.command()
@commands.check(in_channel)
async def gimme(ctx, year, contest, question_number):
    problem = await asyncio.to_thread(get_problem, int(year), contest, int(question_number))

    if not problem:
        await ctx.send("Problem not found in database!")
        return

    problem_id = problem[0]
    image_path = os.path.join(RENDERS_DIR, f"{problem_id}.png")

    if not os.path.exists(image_path):
        await ctx.send("Problem found in database, but image render is missing!")
        return

    file = discord.File(image_path, filename=os.path.basename(image_path))
    await ctx.send(file=file)

if __name__ == "__main__":
    bot.run(token)