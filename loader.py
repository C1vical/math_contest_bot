from database import create_database, add_problem

def load_cemc():
    from cemc.cemc_urls import contests, academic_years
    from cemc.cemc_parser import fetch_raw_html, extract_problems

    for contest in contests:
        for year in academic_years.keys():
            raw_html = fetch_raw_html(year, contest)

            try:
                problems = extract_problems(raw_html)
            except Exception as e:
                print(f"Error extracting problems for {year} {contest}: {e}")
                continue
            for i, problem in enumerate(problems, start=1):
                add_problem(year, contest, i, problem)
            print(f"Loaded {len(problems)} problems for {year} {contest}")

def load_aops():
    from constants import CONTEST_REGISTRY

    for key, info in CONTEST_REGISTRY.items():
        contest = key.split("_")[0]
        _, min_year, max_year, _ = info

        for year in range(min_year, max_year + 1):
            load_contest(year, contest)

def load_contest(year: int, contest: str):
    from constants import get_contest_info
    from aops.aops_parser import fetch_problem_statement

    info = get_contest_info(contest)
    wiki_title, _, _, max_probs = info

    for problem_num in range(1, max_probs + 1):
        question_statement = fetch_problem_statement(year, wiki_title, problem_num)
        add_problem(year, contest, problem_num, question_statement)

def load_aops_community():
    from aops.community_parser import run_session, extract_problems
    run_session(extract_problems)
    print("Done loading problems from AoPS community!")

def load_all_contests():
    # load_cemc()
    # load_aops()
    load_aops_community()

if __name__ == "__main__":
    create_database()
    load_all_contests()