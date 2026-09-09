import asyncio
import random
from parsing.aops_parser import fetch_problem_statement
from rendering.aops_renderer import render_all_problems
from database import create_database, add_problem, get_loaded_problems
from constants import CONTEST_REGISTRY, get_contest_info
from logger_config import get_file_logger

logger = get_file_logger("parsing", "logs/parsing.log")

async def load_all_contests():
    """Iterate through all registered contests and fetch missing problem sets into SQLite."""
    loaded_problems = get_loaded_problems()

    for key, info in CONTEST_REGISTRY.items():
        contest = key.split("_")[0]
        _, min_year, max_year, _ = info

        for year in range(min_year, max_year + 1):
            await load_contest(year, contest, loaded_problems)

async def load_contest(year: int, contest: str, loaded_problems: set):
    """Fetch missing problems for a specific contest year."""
    info = get_contest_info(year, contest)
    wiki_title, _, _, max_probs = info

    missing_problems = [
        p for p in range(1, max_probs + 1)
        if (year, contest, p) not in loaded_problems
    ]

    if not missing_problems:
        logger.info(f"Already loaded {year} {contest} ({max_probs} problems)")
        return

    for problem_num in missing_problems:
        await load_problem(year, contest, wiki_title, problem_num, loaded_problems)

    logger.info(f"Finished loading {len(missing_problems)} missing problems for {year} {contest}")

async def load_problem(year: int, contest: str, wiki_title: str, problem_num: int, loaded_problems: set):
    """Fetch a single problem statement and save it to SQLite."""
    question_statement = await fetch_problem_statement(year, wiki_title, problem_num)

    if not question_statement:
        logger.warning(f"Failed to load {year} {contest} Problem {problem_num}")
        return

    add_problem(year, contest, problem_num, question_statement)
    loaded_problems.add((year, contest, problem_num))
    logger.info(f"Successfully loaded {year} {contest} Problem {problem_num}")

    await asyncio.sleep(random.uniform(1.8, 3.2))

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