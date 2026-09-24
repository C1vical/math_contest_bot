import sqlite3
from pathlib import Path

from constants import generate_problem_id

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "data" / "problems.db"

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
                answer TEXT NOT NULL
            )
        """)

async def get_problem(year: int, contest: str, question_number: int):
    """Fetch a single problem record from the SQLite database."""
    problem_id = generate_problem_id(year, contest, question_number)
    with sqlite3.connect(DATABASE) as conn:
        return conn.execute("SELECT * FROM math_problems WHERE id = ?", (problem_id,)).fetchone()

async def get_random_problem(year=None, contest=None, question_number=None):
    """Fetch a random problem based on arguments"""
    with sqlite3.connect(DATABASE) as conn:
        query = "SELECT * FROM math_problems WHERE 1=1"
        params = []

        if year is not None:
            query += " AND year = ?"
            params.append(year)

        if contest is not None:
            query += " AND UPPER(contest) = UPPER(?)"
            params.append(contest)

        if question_number is not None:
            query += " AND question_number = ?"  # Adjust column name to match your DB schema
            params.append(question_number)

        query += " ORDER BY RANDOM() LIMIT 1"

        cursor = conn.cursor()
        cursor.execute(query, params)
        return cursor.fetchone()

def add_problem(year: int, contest: str, q_num: int, statement: str, answer: str = "0"):
    """Insert or update a math problem into the SQLite database."""
    problem_id = generate_problem_id(year, contest, q_num)

    with sqlite3.connect(DATABASE) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO math_problems (
                id, year, contest, question_number, question_statement, answer
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (problem_id, year, contest, q_num, statement, str(answer)),
        )

def get_contest_count():
    num_problems = 0
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute(
            "SELECT contest, COUNT(*) FROM math_problems GROUP BY contest ORDER BY contest"
        )
        for contest, count in cursor.fetchall():
            print(f"{contest}: {count}")
            num_problems += count
    print(f"Total problems: {num_problems}")

if __name__ == "__main__":
    get_contest_count()