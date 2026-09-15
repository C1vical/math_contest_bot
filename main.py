import io
import logging
import os
import threading

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask

from constants import get_contests, display_contest_info
from database import get_problem, get_random_problem
from rendering.aops_renderer import get_image_data

# FLASK HEALTH-CHECK SERVER (Render Keep-Alive)
app = Flask(__name__)

@app.route("/")
def health_check():
    return "Bot is alive!", 200

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_http_server, daemon=True).start()

# BOT CONFIGURATION & INITIALIZATION
load_dotenv()
token = os.getenv("DISCORD_TOKEN")
channel_id = int(os.getenv("CHANNEL_ID"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)  # 'command_prefix' retained for Bot instance initialization

def is_allowed_channel(interaction: discord.Interaction) -> bool:
    """Enforces that slash commands are only run in the designated channel."""
    return interaction.channel_id == channel_id

@bot.event
async def on_ready():
    # Sync slash commands with Discord API upon login
    try:
        synced = await bot.tree.sync()
        logging.info(f"Successfully synced {len(synced)} slash command(s).")
    except Exception as e:
        logging.error(f"Failed to sync slash commands: {e}")

    channel = bot.get_channel(channel_id)
    if channel:
        await channel.send("**Math Contest Bot is online!** Type `/` to see available commands.")

# SLASH COMMANDS
@bot.tree.command(name="help", description="Display help information for bot commands")
@app_commands.check(is_allowed_channel)
async def help_command(interaction: discord.Interaction, command_name: str = None):
    embed = discord.Embed(color=discord.Color.blue())

    if command_name is None:
        embed.title = "Math Contest Bot — Help Menu"
        embed.description = "Use `/help <command_name>` to view detailed information about a specific command."
        embed.add_field(
            name="Available Slash Commands",
            value=(
                "• `/contests` — View all loaded contest series in the database\n"
                "• `/info` — View details and available years for a specific contest\n"
                "• `/gimme` — Fetch a specific problem by year, contest, and problem number\n"
                "• `/random` — Fetch a random problem with optional filters\n"
                "• `/help` — Display this help menu"
            ),
            inline=False
        )
        await interaction.response.send_message(embed=embed)
        return

    cmd = command_name.lower()
    if cmd == "contests":
        embed.title = "Help: `/contests`"
        embed.description = "Displays a list of all competition series currently stored in the database."
    elif cmd == "info":
        embed.title = "Help: `/info`"
        embed.description = "Displays details about a specific contest. Requires 'contest'"
    elif cmd == "gimme":
        embed.title = "Help: `/gimme`"
        embed.description = "Fetches a specific problem image. Requires `year`, `contest`, and `question_number`."
    elif cmd == "random":
        embed.title = "Help: `/random`"
        embed.description = "Retrieves a random problem. You can optionally filter by `year`, `contest`, or `question_number`."
    else:
        await interaction.response.send_message(f"Unknown command `{command_name}`.", ephemeral=True)
        return

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="contests", description="View all available contest series loaded in the database")
@app_commands.check(is_allowed_channel)
async def contests(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Available Contests",
        description="Here are the contest series currently loaded in the database:",
        color=discord.Color.blue()
    )
    contest_list = get_contests()
    embed.add_field(name="", value=contest_list, inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="info", description="View details and available years for a specific contest")
@app_commands.describe(contest="The contest family name (e.g., AMC10A, AIME, IMO)")
@app_commands.check(is_allowed_channel)
async def info(interaction: discord.Interaction, contest: str):
    embed = discord.Embed(
        title=f"{contest.upper()} Contest Info",
        description=f"Information for **{contest.upper()}**:",
        color=discord.Color.blue()
    )
    contest_info = display_contest_info(contest)
    embed.add_field(name="", value=contest_info, inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="gimme", description="Fetch a specific problem")
@app_commands.describe(
    year="Competition year (e.g. 2024)",
    contest="Contest name (e.g. AMC10A)",
    question_number="Problem number (e.g. 5)"
)
@app_commands.check(is_allowed_channel)
async def gimme(interaction: discord.Interaction, year: int, contest: str, question_number: int):
    # Defer response to allow image generation/fetching without hitting Discord's 3-second timeout
    await interaction.response.defer()
    problem = await get_problem(year, contest, question_number)
    await send_problem_image(interaction, problem)

@bot.tree.command(name="random", description="Fetch a random problem with optional filters")
@app_commands.describe(
    year="Filter by competition year (optional)",
    contest="Filter by contest name (optional)",
    question_number="Filter by problem number (optional)"
)
@app_commands.check(is_allowed_channel)
async def random_cmd(interaction: discord.Interaction, year: int = None, contest: str = None, question_number: int = None):
    # Defer response to handle database and R2 fetch overhead
    await interaction.response.defer()
    random_problem = await get_random_problem(year, contest, question_number)
    await send_problem_image(interaction, random_problem)

# GLOBAL SLASH COMMAND ERROR HANDLER
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        # Silently reject commands run outside the allowed channel using an ephemeral message
        await interaction.response.send_message("Slash commands cannot be used in this channel.", ephemeral=True)
    else:
        logging.error(f"Unhandled interaction error: {error}")

async def send_problem_image(interaction: discord.Interaction, problem: tuple):
    if not problem:
        await interaction.followup.send("Problem not found in database!")
        return

    problem_id = problem[0]
    image_data = await get_image_data(problem_id)

    if not image_data:
        await interaction.followup.send("Found a problem, but couldn't load the render from R2.")
        return

    problem_render = discord.File(io.BytesIO(image_data), filename=f"{problem_id}.png")
    await interaction.followup.send(file=problem_render)

if __name__ == "__main__":
    bot.run(token)