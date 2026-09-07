import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database import get_problem
from scraper import prepare_database_and_renders

load_dotenv()
token = os.getenv("DISCORD_TOKEN")
channel_id = int(os.getenv("CHANNEL_ID"))

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

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

    image_path = problem[6]

    file = discord.File(image_path, filename=os.path.basename(image_path))
    await ctx.send(file=file)

if __name__ == "__main__":
    # asyncio.run(prepare_database_and_renders())
    bot.run(token, log_handler=handler, log_level=logging.DEBUG)