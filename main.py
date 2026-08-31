import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os

load_dotenv()
token = os.getenv("DISCORD_TOKEN")

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents =discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print("running")

@bot.command()
async def hi(ctx):
    await ctx.send(f"Hey {ctx.author.mention}! Welcome to MCC!")

@bot.command()
async def repeat(ctx, *, arg):
    await ctx.send(arg)

@bot.command()
async def problem(ctx):
    await ctx.send("What is 1+1?")

bot.run(token, log_handler=handler, log_level=logging.DEBUG)
