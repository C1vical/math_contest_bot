import json
from curl_cffi import requests
from bs4 import BeautifulSoup

def extract_urls():
    base_url = "https://cemc.uwaterloo.ca/resources/past-contests"
    view_query = "block_config_key%3Dpast_contest%3A1srMgUG8ZWnN5_Mx6CmP5HeP-aleHwL0jgxyVUuVYE4"
    contests = ["Pascal", "Cayley", "Fermat", "Euclid", "Hypatia", "Galois", "Fryer", "Gauss7", "Gauss8"]
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

    contest_categories = {
        "PCF": "14",
        "Euclid": "24",
        "FGH": "25",
        "Gauss": "13"
    }

    urls = {}

    for academic_year in academic_years.values():
        for contest_category in contest_categories.values():
            query_params = {
                "view_query": view_query,
                "grade": "All",
                "academic_year": academic_year,
                "contest_category": contest_category,
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

                urls[f"{y}_{c}"] = link
    return urls

if __name__ == "__main__":
    # Extract URLs and save to JSON file
    dict = extract_urls()
    with open("cemc_urls.json", "w") as json_file:
        json.dump(dict, json_file, indent=4)
    print("JSON data is saved.")