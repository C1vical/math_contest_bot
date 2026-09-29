from patchright.sync_api import sync_playwright
import json
import re
from pathlib import Path

base_url = "https://artofproblemsolving.com/community/" # base URL for AoPS community
session_id = "21d6f40cfb511982e4424e0e250a9557" # default ID for non-logged in users

# contest and its AOPS id
contests = {
    "CMO": "3277",
    "CJMO": "1231801",
    "CMOQR": "3280"
}

# contest and its AOPS id for contests before 1977
contest_ids = {
    "1976 CMO": "5021",
    "1975 CMO": "5020",
    "1974 CMO": "5019",
    "1973 CMO": "5018",
    "1972 CMO": "5017",
    "1971 CMO": "5016",
    "1970 CMO": "5015",
    "1969 CMO": "5014",
}

BASE_DIR = Path(__file__).resolve().parent
ID_PATH = BASE_DIR / "ids.json" # path to save contest ids

def run_session(func):
    with sync_playwright() as p:
        print("Initializing session...")
        browser = p.chromium.launch(headless=True, channel="chrome")
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        func(page)

        browser.close()

def fetch_category_data(page, id):
    """ Fetches category data from AoPS community for a given category ID """
    return page.evaluate("""
        async ([id, session_id]) => {
        const response = await fetch(
                "https://artofproblemsolving.com/m/community/ajax.php",
                {
                    headers: {
                        "accept": "application/json, text/javascript, */*; q=0.01",
                        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "x-requested-with": "XMLHttpRequest"
                    },
                    body: `category_id=${id}&a=fetch_category_data&aops_logged_in=false&aops_user_id=1&aops_session_id=${session_id}`,
                    method: "POST",
                    credentials: "include"
                }
            );

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        return await response.json();
    }""", [id, session_id])

def save_contest_ids_to_json(page):
    """ Fetches contest IDs from the AoPS community and saves them to a JSON file """
    page.goto(base_url)

    ids = {}
    for contest, id in contests.items():
        print(f"Fetching {contest}...")

        response = fetch_category_data(page, id)
        items = response.get("response", {}).get("category", {}).get("items", [])

        for item in items:
            ids[f"{item['item_score']} {contest}"] = item["item_id"]

    ids.update(contest_ids)
    print(f"Fetched item ids successfully. Now writing to file...")

    # convert to json and write to file
    with open(ID_PATH, "w", encoding="utf-8") as f:
        json.dump(ids, f, indent=2)

def extract_problems(page):
    """ Extracts problems from the AoPS community and saves them to contests.json"""
    from database import add_problem
    page.goto(base_url)

    with open(ID_PATH, "r") as f:
        data = json.load(f)

    # iterate through contests and extract problems
    for contest, id in data.items():
        print(f"Extracting problems for {contest}...")

        response = fetch_category_data(page, id)
        items = response.get("response", {}).get("category", {}).get("items", [])

        # extract problems from items, making sure
        # to only include items that have a numeric item_text (i.e. problem number)
        contest_year = contest.split(" ")[0]
        contest_name = contest.split(" ")[1]
        for item in items:
            if not item.get('item_text', "").isdigit():
                continue
            else:
                problem_number = item.get('item_text')
                title_header = f"<h2>{contest_year} {contest_name} Problem {problem_number}</h2>"
                final_statement = title_header + item.get("post_data", {}).get("post_rendered", {})
                final_statement = re.sub(r'src=(["\'])//', r'src=\1https://', final_statement)

                add_problem(contest_year,
                            contest_name,
                            problem_number,
                            final_statement,)

        print(f"Loaded problems for {contest}!")

        # limit to 5 requests per second, or we get blocked
        page.wait_for_timeout(200)

if __name__ == "__main__":
    # Fetches contest IDs from the AoPS community and save them to a JSON file
    run_session(save_contest_ids_to_json)
    print("Done saving contest IDs!")