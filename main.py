import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os

import asyncio

from database import get_problem, prepare_database_and_renders

load_dotenv()
token = os.getenv("DISCORD_TOKEN")

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

channel_id = int(os.getenv("CHANNEL_ID"))

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

    problem = get_problem(int(year), contest, int(question_number))

    if not problem:
        await ctx.send("Problem not found in database!")
        return

    problem_statement = problem[4]
    answer = problem[5]
    image_path = problem[6]

    # Send rendered PNG image if present, fallback to raw LaTeX codeblock
    if image_path and os.path.exists(image_path):
        file = discord.File(image_path, filename=os.path.basename(image_path))
        await ctx.send(file=file)
    else:
        await ctx.send(f"```latex\n{problem_statement}\n```")

    # def check(message):
    #     return message.author == ctx.author and message.channel == ctx.channel
    #
    # try:
    #     response = await bot.wait_for("message", timeout=5.0, check=check)
    # except asyncio.TimeoutError:
    #     await ctx.send(f"Ran out of time! The answer was {answer}")
    # else:
    #     if response.content == str(answer):
    #         await ctx.send("Correct!")
    #     else:
    #         await ctx.send(f"Incorrect! The answer was {answer}")

async def main():
    # 1. Wait until all scraping and rendering finishes completely
    print("Initializing database and rendering images...")
    await prepare_database_and_renders()

    # 2. Start the Discord bot using bot.start()
    print("Starting Discord bot...")
    async with bot:
        await bot.start(token, log_handler=handler, log_level=logging.DEBUG)

if __name__ == "__main__":
    asyncio.run(main())
    # bot.run(token, log_handler=handler, log_level=logging.DEBUG)
