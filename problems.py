import random
import sqlite3

class Problem:
    operations = ("+", "-", "*")

    # sqlite database
    statement_index = 3
    answer_index = 4

    def __init__(self):
        self.num1 = random.randint(1, 10)
        self.num2 = random.randint(1, 10)
        self.operation = random.choice(Problem.operations)

    def generate_problem(self):
        return f"What is {self.num1} {self.operation} {self.num2}?"

    def answer(self):
        if self.operation == "+":
            return self.num1 + self.num2
        elif self.operation == "-":
            return self.num1 - self.num2
        else:
            return self.num1 * self.num2

    @staticmethod
    def fetch_problem(contest, year, question_number):
        conn = sqlite3.connect("math_problems.db")

        c = conn.cursor()

        c.execute("SELECT * FROM math_problems"
                  " WHERE contest=? AND year=? AND question_number=?", (contest, year, question_number))

        question = c.fetchone()

        conn.close()

        return question

    @staticmethod
    def fetch_problem_statement(question):
        return question[Problem.statement_index]

    @staticmethod
    def fetch_problem_answer(question):
        return question[Problem.answer_index]










