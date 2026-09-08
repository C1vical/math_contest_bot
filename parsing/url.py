import re

import requests
from bs4 import BeautifulSoup

def extract_urls():
    base_url = "https://cemc.uwaterloo.ca/resources/past-contests"
    contests = ["Pascal", "Cayley", "Fermat"]
    academic_years = {
        "2026": "86",
        "2025": "83",
        "2024": "57",
        "2023": "31",
        "2022": "32",
        "2021": "33",
        "2020": "34",
        "2019": "35",
        "2018": "36",
        "2017": "37",
        "2016": "38",
    }
    urls = {}

    for academic_year in academic_years.values():
        query_params = {
            "view_query": "block_config_key%3Dpast_contest%3A1srMgUG8ZWnN5_Mx6CmP5HeP-aleHwL0jgxyVUuVYE4",
            "grade": "All",
            "academic_year": academic_year,
            "contest_category": 14,
        }

        response = requests.get(base_url, params=query_params)
        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup.find_all("a", href=re.compile("Contest.html")):
            link = "https://cemc.uwaterloo.ca" + tag["href"]
            id = tag["href"].split("/")[-1]

            for contest in contests:
                if contest in id:
                    c = contest
            for year in academic_years.keys():
                if year in id:
                    y = year

            urls[(c,y)] = link
    return urls
if __name__ == "__main__":
    dict = extract_urls()
    print(len(dict))
    print(dict)