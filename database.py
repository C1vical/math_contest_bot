import os
import sqlite3
from constants import DATABASE, RENDERS_DIR, CONTEST_REGISTRY, generate_problem_id

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

def get_problem(year: int, contest: str, question_number: int):
    """Fetch a single problem record from the SQLite database."""
    problem_id = generate_problem_id(year, contest, question_number)
    with sqlite3.connect(DATABASE) as conn:
        return conn.execute("SELECT * FROM math_problems WHERE id = ?", (problem_id,)).fetchone()

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

def remove_problem(year: int, contest: str, q_num: int):
    """Remove a math problem from the SQLite database."""
    problem_id = generate_problem_id(year, contest, q_num)

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute(
            "DELETE FROM math_problems WHERE id = ?", (problem_id,)
        )

    render_path = os.path.join(RENDERS_DIR, f"{problem_id}.png")
    os.remove(render_path)

    print(f"Removed problem {problem_id} from database.")

def is_edge_case_skipped(year: int, contest: str) -> bool:
    """Check if a given contest year is a known historical skip/gap year."""
    return (year == 2021 and contest == "AMC8") or (year == 1980 and contest == "IMO")

def get_loaded_problems() -> set:
    """Return a set of (year, contest, question_number) tuples stored in SQLite or skipped."""
    loaded_problems = set()

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute("SELECT year, contest, question_number FROM math_problems")
        loaded_problems.update(cursor.fetchall())

    # Include edge case skip years so all of their problem numbers are skipped
    for key, info in CONTEST_REGISTRY.items():
        _, min_year, max_year, max_probs = info
        contest = key.split("_")[0]

        for year in range(min_year, max_year + 1):
            if is_edge_case_skipped(year, contest):
                for q_num in range(1, max_probs + 1):
                    loaded_problems.add((year, contest, q_num))

    return loaded_problems

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
    # get_contest_count()
    remove_problem(2015, "USAJMO", 2)
    remove_problem(2015, "USAJMO", 3)