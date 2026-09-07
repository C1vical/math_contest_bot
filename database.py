import sqlite3
from constants import DATABASE, CONTEST_REGISTRY, generate_problem_id

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
    problem_id = generate_problem_id(year, contest, q_num)
    image_path = f"renders/{problem_id}.png"

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
    return (year == 2021 and contest == "AMC8") or (year == 1980 and contest == "IMO")

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
            if is_edge_case_skipped(year, contest) or counts.get((year, contest), 0) >= max_probs:
                loaded_contests.add((year, contest))

    return loaded_contests