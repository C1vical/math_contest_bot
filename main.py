import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os

import asyncio

import sqlite3

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
async def problem(ctx, contest, year, question_number):
    conn = sqlite3.connect("math_problems.db")

    c = conn.cursor()

    c.execute("SELECT * FROM math_problems"
              " WHERE contest=? AND year=? AND question_number=?", (contest, year, question_number))

    question = c.fetchone()
    print(question)

    question_statement = question[3]
    answer = question[4]
    print(question_statement)
    print(answer)

    await ctx.send(question_statement)

    def check(message):
        return message.author == ctx.author and message.channel == ctx.channel

    try:
        response = await bot.wait_for("message", timeout=5.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send(f"Ran out of time! The answer was {answer}")
    else:
        if response.content == str(answer):
            await ctx.send("Correct!")
        else:
            await ctx.send(f"Incorrect! The answer was {answer}")


bot.run(token, log_handler=handler, log_level=logging.DEBUG)
