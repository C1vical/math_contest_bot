import sqlite3
import time
import random

from aops_parser import fetch_aops_problem_set
from constants import (
    CONTEST_REGISTRY,
    is_valid_request,
    generate_problem_id,
    get_contest_info
)

DATABASE = "math_problems.db"

def create_database():
    """Create the database/table if it doesn't already exist."""
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


def add_problem(year: int, contest: str, q_num: int, statement: str, answer: str = "0"):
    # 1. Validate request bounds against registry
    if not is_valid_request(contest, year, q_num):
        raise ValueError(f"Invalid problem request: {year} {contest} #{q_num}")

    # 2. Build the primary key and image path
    clean_contest = contest.upper().replace(" ", "")
    problem_id = generate_problem_id(year, clean_contest, q_num)
    image_path = f"renders/{problem_id}.png"

    # 3. Store in SQLite
    with sqlite3.connect(DATABASE) as conn:
        conn.execute(
            """
            INSERT INTO math_problems (
                id, year, contest, question_number, question_statement, answer, image_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                question_statement = excluded.question_statement,
                answer = excluded.answer;
            """,
            (problem_id, year, clean_contest, q_num, statement, str(answer), image_path),
        )


def get_problem(year: int, contest: str, question_number: int):
    """Constructs shortcode ID from inputs and fetches the problem record."""
    problem_id = generate_problem_id(year, contest, question_number)

    with sqlite3.connect(DATABASE) as conn:
        return conn.execute("SELECT * FROM math_problems WHERE id = ?", (problem_id,)).fetchone()

def load_contest_problems(year: int, contest: str):
    """Load the contest problems into the database."""
    info = get_contest_info(contest, year)
    if not info:
        print(f"[-] Invalid contest/year: {contest} {year}")
        return

    _, wiki_title, _, _, max_probs = info
    wiki_name = wiki_title.replace("_", " ")

    try:
        problems = fetch_aops_problem_set(year, wiki_name)
        if not problems:
            print(f"[-] No data: {year} {contest}")
            return

        for q_num, statement in enumerate(problems[:max_probs], start=1):
            add_problem(year, contest, q_num, statement)

        print(f"[+] Loaded {year} {contest} ({len(problems[:max_probs])} problems)")

    except Exception as e:
        print(f"[!] Error {year} {contest}: {e}")

def load_all_problems():
    """Load all problems from AoPS into the database."""
    seen = set()

    for key, info in CONTEST_REGISTRY.items():
        base_contest = key.split("_")[0]
        _, wiki_title, min_year, max_year, _ = info
        group = (base_contest, wiki_title, min_year, max_year)

        if group in seen:
            continue
        seen.add(group)

        for year in range(min_year, max_year + 1):
            load_contest_problems(year, base_contest)
            time.sleep(random.uniform(0.5, 1.2))

if __name__ == "__main__":
    create_database()

    load_contest_problems(2025, "AMC10A")


    # for i in range(2020, 2026):
    #     load_contest_problems(i, "AMC", "10A", 25)
    #     time.sleep(random.uniform(1, 3))