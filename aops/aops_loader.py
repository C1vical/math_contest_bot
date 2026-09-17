import time
import random
from aops.aops_parser import fetch_problem_statement
from database import create_database, add_problem, get_loaded_problems
from constants import CONTEST_REGISTRY, get_contest_info

def load_all_contests():
    """Iterate through all registered contests and fetch missing problem sets into SQLite."""
    loaded_problems = get_loaded_problems()

    for key, info in CONTEST_REGISTRY.items():
        contest = key.split("_")[0]
        _, min_year, max_year, _ = info

        for year in range(min_year, max_year + 1):
            load_contest(year, contest, loaded_problems)

def load_contest(year: int, contest: str, loaded_problems: set):
    """Fetch missing problems for a specific contest year."""
    info = get_contest_info(contest)
    wiki_title, _, _, max_probs = info

    missing_problems = [p for p in range(1, max_probs + 1) if (year, contest, p) not in loaded_problems]

    if not missing_problems:
        print(f"Already loaded {year} {contest}")
        return

    print(f"Attempting to load {year} {contest} ({len(missing_problems)} missing problems):")

    successful_count = 0
    unsuccessful_problem_num = []

    for problem_num in missing_problems:
        success = load_problem(year, contest, wiki_title, problem_num, loaded_problems)
        if success:
            successful_count += 1
        else:
            unsuccessful_problem_num.append(problem_num)

    if successful_count == len(missing_problems):
        print(f"Finished loading all {successful_count} missing problems for {year} {contest}")
    else:
        print(
            f"{year} {contest}: {successful_count} loaded successfully. "
            f"Failed to load {len(unsuccessful_problem_num)} problem(s): {unsuccessful_problem_num}"
        )

def load_problem(year: int, contest: str, wiki_title: str, problem_num: int, loaded_problems: set) -> bool:
    """Fetch a single problem statement and save it to SQLite. Returns True on success."""
    question_statement = fetch_problem_statement(year, wiki_title, problem_num)

    if not question_statement:
        return False

    add_problem(year, contest, problem_num, question_statement)
    loaded_problems.add((year, contest, problem_num))

    time.sleep(random.uniform(1.8, 3.2))
    return True

if __name__ == "__main__":
    create_database()
    load_all_contests()