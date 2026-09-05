import os
import sqlite3
import time
import random
import asyncio

from aops_parser import fetch_aops_problem_set, render_all_problems_from_db
from constants import (
    DATABASE,
    CONTEST_REGISTRY,
    is_valid_request,
    generate_problem_id,
    get_contest_info,
)

from constants import DATABASE

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
                image_path TEXT NOT NULL,
                rendered BOOLEAN NOT NULL DEFAULT 0
            )
        """)


def is_edge_case_skipped(year: int, contest: str) -> bool:
    """
    Check if a given contest year is a known historical skip/gap year.

    Args:
        year (int): The year of the contest.
        contest (str): The contest identifier name.

    Returns:
        bool: True if the contest was skipped historically, False otherwise.
    """
    clean_contest = contest.upper().replace(" ", "")

    # AMC 8 moved from November to January in 2022, resulting in no 2021 contest
    if year == 2021 and clean_contest == "AMC8":
        return True

    # IMO was cancelled in 1980
    if year == 1980 and clean_contest == "IMO":
        return True

    return False


def add_problem(year: int, contest: str, q_num: int, statement: str, answer: str = "0"):
    """
    Insert or update a single math problem entry in the SQLite database.

    Args:
        year (int): Year of the contest.
        contest (str): Name of the contest.
        q_num (int): Question/Problem number.
        statement (str): Text/LaTeX content of the problem statement.
        answer (str, optional): Verified answer string. Defaults to "0".
    """
    # Verify bounds and contest rules against configured registry limits
    if not is_valid_request(year, contest, q_num):
        raise ValueError(f"Invalid problem request: {year} {contest} #{q_num}")

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


def is_contest_loaded(year: int, contest: str) -> bool:
    """
    Check whether a contest year has already been recorded in SQLite.

    Args:
        year (int): Year of the contest.
        contest (str): Name of the contest.

    Returns:
        bool: True if at least one problem exists for this year and contest, False otherwise.
    """
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM math_problems WHERE year = ? AND contest = ?)",
            (year, contest),
        )
        return bool(cursor.fetchone()[0])


def get_fully_loaded_contests() -> set:
    """
    Query the database once to return a set of all (year, clean_contest) tuples that exist.

    Returns:
        set: A set containing (year, contest) tuples that are already stored or skipped.
    """
    loaded_contests = set()
    if not os.path.exists(DATABASE):
        return loaded_contests

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT year, contest FROM math_problems")
        existing_contests = set(cursor.fetchall())

    for key, info in CONTEST_REGISTRY.items():
        _, _, min_year, max_year, _ = info
        contest = key.split("_")[0]

        for year in range(min_year, max_year + 1):
            if is_edge_case_skipped(year, contest) or (year, contest) in existing_contests:
                loaded_contests.add((year, contest))

    return loaded_contests


def load_contest_problems(year: int, contest: str, loaded_set: set = None):
    """
    Fetch a full problem set for a contest year from the AoPS wiki and store in SQLite.

    Args:
        year (int): Year of the contest.
        contest (str): Name of the contest.
        loaded_set (set, optional): A set of already loaded (year, contest) tuples.
    """

    # Skip known missing years
    if is_edge_case_skipped(year, contest):
        print(f"Skipping {year} {contest}: No contest took place this year.")
        return

    # Check cached set to avoid unnecessary operations
    if loaded_set is not None and (year, contest) in loaded_set:
        print(f"Skipping {year} {contest}: Already fully loaded.")
        return

    # Retrieve system metadata for the requested contest year
    info = get_contest_info(year, contest)
    if not info:
        print(f"Error: Invalid contest/year combination: {contest} {year}")
        return

    _, wiki_title, _, _, max_probs = info
    wiki_name = wiki_title.replace("_", " ")

    # Fallback SQLite check if cache set was not provided
    if loaded_set is None and is_contest_loaded(year, contest):
        print(f"Skipping {year} {contest}: Already fully loaded.")
        return

    # Download from AoPS and process problems
    try:
        problems = fetch_aops_problem_set(year, wiki_name)
        if not problems:
            print(f"Notice: No problem data found for {year} {contest}")
            return

        for q_num, statement in enumerate(problems[:max_probs], start=1):
            add_problem(year, contest, q_num, statement)

        print(f"Successfully loaded {year} {contest} ({len(problems[:max_probs])} problems)")

    except Exception as e:
        print(f"Error fetching {year} {contest}: {e}")


def load_all_problems():
    """Iterate through all registered contests and fetch missing problem sets into SQLite."""
    loaded_set = get_fully_loaded_contests()

    for key, info in CONTEST_REGISTRY.items():
        contest = key.split("_")[0]
        _, _, min_year, max_year, _ = info

        for year in range(min_year, max_year + 1):
            # Skip fully loaded or non-existent contests instantly
            if (year, contest) in loaded_set:
                if is_edge_case_skipped(year, contest):
                    print(f"Skipping {year} {contest}: No contest took place.")
                else:
                    print(f"Skipping {year} {contest}: Already loaded.")
                continue

            load_contest_problems(year, contest, loaded_set=loaded_set)

            # Politeness delay between network requests to avoid rate limits
            time.sleep(random.uniform(2.5, 4.0))


def get_problem(year: int, contest: str, question_number: int):
    """
    Fetch a single problem record from the SQLite database.

    Args:
        year (int): Year of the contest.
        contest (str): Name of the contest.
        question_number (int): Question/Problem number.

    Returns:
        tuple: Database row tuple representing the math problem record.
    """
    problem_id = generate_problem_id(year, contest, question_number)

    with sqlite3.connect(DATABASE) as conn:
        return conn.execute("SELECT * FROM math_problems WHERE id = ?", (problem_id,)).fetchone()


def get_problem_statement(problem_file) -> str:
    """Extract the problem statement text from a fetched database tuple."""
    return problem_file[4]


def get_problem_answer(problem_file) -> str:
    """Extract the problem answer string from a fetched database tuple."""
    return problem_file[5]

async def prepare_database_and_renders():
    """
    Creates DB, runs scraper and Playwright renderer concurrently,
    and returns when all problems are fully scraped and rendered.
    """
    create_database()
    scraper_done_event = asyncio.Event()

    async def run_scraper():
        print("Starting problem scraper...")
        # Offload sync Cloudscraper scraping loop to a background thread
        await asyncio.to_thread(load_all_problems)
        print("Scraper completed all contest years.")
        scraper_done_event.set()

    # Run scraper and renderer in parallel
    await asyncio.gather(
        run_scraper(),
        render_all_problems_from_db(scraper_finished_event=scraper_done_event)
    )
    print("Database build and image rendering complete.")


async def test_single_contest(year: int, contest: str):
    """
    Test helper to scrape and render a single specific contest year.
    """
    print(f"--- Running Test for: {year} {contest} ---")
    create_database()
    scraper_done_event = asyncio.Event()

    async def run_single_scraper():
        print(f"Scraping problem set for {year} {contest}...")
        # Run single contest scraper in a background thread
        await asyncio.to_thread(load_contest_problems, year, contest)
        print(f"Finished scraping {year} {contest}.")
        scraper_done_event.set()

    # Run single-contest scraper and renderer in parallel
    await asyncio.gather(
        run_single_scraper(),
        render_all_problems_from_db(scraper_finished_event=scraper_done_event)
    )
    print(f"--- Test complete for {year} {contest} ---")


if __name__ == "__main__":
    # Change these values to test any contest you want
    TEST_YEAR = 2008
    TEST_CONTEST = "AMC10B"

    asyncio.run(test_single_contest(TEST_YEAR, TEST_CONTEST))