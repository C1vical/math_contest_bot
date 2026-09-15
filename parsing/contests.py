import json

# Path to the JSON file where contest data will be stored
FILE_PATH = "contests.json"

# base template with currently supported contests
# layer 1: contest (i.e. AMC10, CMO, etc.)
# layer 2: variant (i.e. A, B, Individual, Guts, etc.)
# layer 3: years (i.e. 2025)
# layer 4: problems (i.e. Problem 1)
# layer 5: HTML for rendering
contests = {
    # USA
    "AJHSME": {},
    "AHSME": {},
    "AMC8": {},
    "AMC10": {}, # 2000 - 2001
    "AMC10 A": {},
    "AMC10 B": {},
    "AMC10 FALLA": {}, # 2001
    "AMC10 FALLB": {}, # 2001
    "AMC12": {}, # 2000 - 2001
    "AMC12 A": {},
    "AMC12 B": {},
    "AMC12 FALLA": {}, # 2001
    "AMC12 FALLB": {}, # 2001
    "AIME": {}, # 1983 - 1999
    "AIME I": {},
    "AIME II": {},
    "USAJMO": {},
    "USAMO": {},

    # CANADA
    "CMO": {},
    "CJMO": {},
    "CMOQR": {},

    # International
    "IMO": {}
}

def add_problems_for_contest(contest_year: str, contest_name: str, problems: list):
    """ Adds problems for a specific contest and year to the contests.json file """
    with open(FILE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # ensure contest exists
    data.setdefault(contest_name, {})

    # ensure not overwriting existing year
    if contest_year in data:
        print(f"{contest_year} {contest_name} already exists")
        return

    # initialize year dictionary
    data[contest_name][contest_year] = dict()

    # add problems to year dictionary
    for index, problem in enumerate(problems, start=1):
        data[contest_name][contest_year][index] = problem

    with open(FILE_PATH, "w") as f:
        json.dump(data, f, indent=2)

if __name__ == "__main__":
    # Initialize the contests.json file with the base template if it doesn't exist
    with open(FILE_PATH, 'w') as f:
        json.dump(contests, f, indent=2)