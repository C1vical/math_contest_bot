import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os

import random
import asyncio

load_dotenv()
token = os.getenv("DISCORD_TOKEN")

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

channel_id = int(os.getenv("CHANNEL_ID")) # josh's channel for testing
def in_channel(ctx):
    return ctx.channel.id == channel_id

@bot.event
async def on_ready():
    print("running")

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
async def problem(ctx):
    operations = ("+", "-", "*")
    num1 = random.randint(1, 10)
    num2 = random.randint(1, 10)
    operation = random.choice(operations)

    if operation == "+":
        answer = num1 + num2
    elif operation == "-":
        answer = num1 - num2
    else:
        answer = num1 * num2

    await ctx.send(f"What is {num1} {operation} {num2}?")

    def check(message):
        return message.author == ctx.author and message.channel == ctx.channel

    try:
        response = await bot.wait_for("message", timeout=30.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send("Ran out of time!")
    else:
        if response.content == str(answer):
            await ctx.send("Correct!")
        else:
            await ctx.send(f"Incorrect! The answer was {answer}")


bot.run(token, log_handler=handler, log_level=logging.DEBUG)
