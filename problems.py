import random

class Problem:
    operations = ("+", "-", "*")

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








