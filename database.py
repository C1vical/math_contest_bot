import sqlite3
import time
import random

from aops_parser import fetch_aops_problem

statement_index = 4
answer_index = 5

DATABASE = "math_problems.db"

def create_database():
    """Create the database/table if it doesn't already exist."""
    conn = sqlite3.connect(DATABASE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS math_problems (
            year INTEGER NOT NULL,
            contest TEXT NOT NULL,
            edition TEXT,
            question_number INTEGER NOT NULL,
            question_statement TEXT NOT NULL,
            answer TEXT NOT NULL,

            UNIQUE(year, contest, edition, question_number)
        )
    """)

    conn.commit()
    conn.close()

def add_problem(contest, year, edition, question_number, question_statement, answer):
    """Add a problem to the database if it isn't already there."""
    conn = sqlite3.connect(DATABASE)

    conn.execute("INSERT OR IGNORE INTO math_problems VALUES (?, ?, ?, ?, ?, ?)", (year, contest, edition, question_number, question_statement, answer))

    conn.commit()
    conn.close()

def get_problem(year, contest, edition, question_number):
    conn = sqlite3.connect(DATABASE)

    cursor = conn.execute("SELECT * FROM math_problems WHERE year=? AND contest=? AND edition=? AND question_number=?", (year, contest, edition, question_number))

    question = cursor.fetchone()

    conn.close()

    return question

def get_problem_statement(question):
    """Get the question statement from a database row."""
    return question[4]

def get_problem_answer(question):
    """Get the answer from a database row."""
    return question[5]

def load_problems(year, contest, edition, problem_numbers=25):
    """Load the problems into the database."""
    for question_number in range(1, problem_numbers + 1):
        question = get_problem(year, contest, edition, question_number)
        if question is None:
            question_statement = fetch_aops_problem(year, contest, edition, question_number)
            add_problem(contest, year, edition, question_number, question_statement, 0)
            time.sleep(random.uniform(2.0, 5.0))

if __name__ == "__main__":
    create_database()

    # load_problems(2025, "AMC", "10A", 25)

    for i in range(1, 26):
        print(f"Problem {i}: {get_problem_statement(get_problem(2025, "AMC", "10A", i))}")
        print("\n")

