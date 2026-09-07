import asyncio
import random
from aops_parser import fetch_aops_problem_set
from aops_renderer import render_all_problems
from database import create_database, add_problem, get_loaded_contests
from constants import CONTEST_REGISTRY, get_contest_info
from logger_config import get_file_logger

logger = get_file_logger("parsing", "logs/parsing.log")

async def load_contest(year: int, contest: str):
    """Fetch a full problem set for a contest year from AoPS and store in SQLite."""
    info = get_contest_info(year, contest)
    wiki_title, _, _, max_probs = info
    problems = await fetch_aops_problem_set(year, wiki_title)

    for q_num, statement in enumerate(problems[:max_probs], start=1):
        add_problem(year, contest, q_num, statement)

    logger.info(f"Successfully loaded {year} {contest} ({len(problems[:max_probs])} problems)")

async def load_all_contests():
    """Iterate through all registered contests and fetch missing problem sets into SQLite."""
    loaded_set = get_loaded_contests()

    for key, info in CONTEST_REGISTRY.items():
        contest = key.split("_")[0]
        _, min_year, max_year, _ = info

        for year in range(min_year, max_year + 1):
            if (year, contest) in loaded_set:
                continue

            await load_contest(year, contest)
            await asyncio.sleep(random.uniform(4.0, 7.0))

async def prepare_database_and_renders():
    """Creates database and runs scraper and renderer concurrently."""
    create_database()
    scraper_done_event = asyncio.Event()

    async def run_scraper():
        print("Starting problem scraper...")
        await load_all_contests()
        print("Scraper completed all contests.")
        scraper_done_event.set()

    await asyncio.gather(
        run_scraper(),
        render_all_problems(scraper_finished_event=scraper_done_event),
    )
    print("Done preparing database and rendering images.")

if __name__ == "__main__":
    asyncio.run(prepare_database_and_renders())