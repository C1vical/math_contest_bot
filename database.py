import os
import sqlite3
import time
import random
import asyncio
import logging

# Set up logger for the current module
logger = logging.getLogger(__name__)

# Basic logging configuration (run once in main.py entry point, or configured globally)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

from aops_parser import fetch_aops_problem_set, render_all_problems
from constants import (
    DATABASE,
    CONTEST_REGISTRY,
    generate_problem_id,
    get_contest_info,
)

def create_database():
    """Create the SQLite database and table schema if it does not already exist."""
    with sqlite3.connect(DATABASE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS math_problems (
                id TEXT PRIMARY KEY,
                year INTEGER NOT NULL,
                contest TEXT NOT NULL,
                question_number INTEGER NOT NULL,
                question_statement TEXT NOT NULL,
                answer TEXT NOT NULL,
                image_path TEXT NOT NULL
            )
        """)

def get_problem(year: int, contest: str, question_number: int):
    """Fetch a single problem record from the SQLite database."""
    problem_id = generate_problem_id(year, contest, question_number)

    with sqlite3.connect(DATABASE) as conn:
        return conn.execute("SELECT * FROM math_problems WHERE id = ?", (problem_id,)).fetchone()

def add_problem(year: int, contest: str, q_num: int, statement: str, answer: str = "0"):
    """Insert or update a math problem into the SQLite database."""
    # Generate primary shortcode ID and local path for image rendering
    problem_id = generate_problem_id(year, contest, q_num)
    image_path = f"renders/{problem_id}.png"

    # Upsert problem details into the math_problems table
    with sqlite3.connect(DATABASE) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO math_problems (
                id, year, contest, question_number, question_statement, answer, image_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (problem_id, year, contest, q_num, statement, str(answer), image_path),
        )

def is_edge_case_skipped(year: int, contest: str) -> bool:
    """Check if a given contest year is a known historical skip/gap year."""
    # AMC 8 moved from November to January in 2022, resulting in no 2021 contest, and IMO was cancelled in 1980
    if (year == 2021 and contest == "AMC8") or (year == 1980 and contest == "IMO"):
        return True

    return False

def is_contest_loaded(year: int, contest: str) -> bool:
    """Check whether a contest is already stored in SQLite."""
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM math_problems WHERE year = ? AND contest = ?)",
            (year, contest),
        )
        return bool(cursor.fetchone()[0])

def get_loaded_contests() -> set:
    """Return a set of all (year, contest) tuples that are completely stored or skipped."""
    loaded_contests = set()

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute("SELECT year, contest, COUNT(*) FROM math_problems GROUP BY year, contest")
        counts = {(row[0], row[1]): row[2] for row in cursor.fetchall()}

    for key, info in CONTEST_REGISTRY.items():
        _, min_year, max_year, max_probs = info
        contest = key.split("_")[0]

        for year in range(min_year, max_year + 1):
            if is_edge_case_skipped(year, contest):
                loaded_contests.add((year, contest))
            elif counts.get((year, contest), 0) >= max_probs:
                loaded_contests.add((year, contest))

    return loaded_contests

async def load_contest(year: int, contest: str):
    """Fetch a full problem set for a contest year from AoPS and store in SQLite."""
    info = get_contest_info(year, contest)
    wiki_title, _, _, max_probs = info
    problems = await fetch_aops_problem_set(year, wiki_title)

    for q_num, statement in enumerate(problems[:max_probs], start=1):
        add_problem(year, contest, q_num, statement)

    print(f"Successfully loaded {year} {contest} ({len(problems[:max_probs])} problems)")

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
            await asyncio.sleep(random.uniform(2.5, 4.0))

async def prepare_database_and_renders():
    """Creates database and runs scraper, renderer concurrently."""
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
    create_database()
    asyncio.run(prepare_database_and_renders())