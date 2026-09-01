import sqlite3
from problems import Problem

conn = sqlite3.connect(":memory:")

c=conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS math_problems (
    contest TEXT,
    year INTEGER,
    question_number INTEGER,
    question_statement TEXT,
    answer INTEGER
)
""")

for i in range (1,11):
    problem = Problem()
    c.execute(
        """INSERT INTO math_problems VALUES (?, ?, ?, ?, ?)""",
        (
            "JON'S MATH BONANZA",
            2026,
            i,
            problem.generate_problem(),
            problem.answer(),
        ),
    )

c.execute("SELECT * FROM math_problems WHERE year=2026")

print(c.fetchall())

conn.commit()

conn.close()