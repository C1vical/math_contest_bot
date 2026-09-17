from curl_cffi import requests
from bs4 import BeautifulSoup
import re
import json

JSON_PATH = "cemc_urls.json"

def fetch_raw_html(contest: str, year: str) -> str:
    with open(JSON_PATH, "r") as f:
        urls = json.load(f)

    key = f"{year}_{contest}"
    url = urls[key]

    response = requests.get(url, impersonate="chrome")
    response.raise_for_status()
    print(response.status_code)
    return response.text

def extract_problems(raw_html: str) -> list[str]:
    """ Extracts problems from the raw HTML of a CEMC contest page."""
    soup = BeautifulSoup(raw_html, "html5lib") # using html5lib will automatically fix broken tags, unlike normal html.parser
    problems = []

    # get rid of all buttons (don't want them when rendering)
    for button in soup.find_all("button"):
        button.decompose()

    p = 1  # problem numbers

    # for PCF
    headers = soup.find_all("h2", string=re.compile(r"(Part [A-C]|^Questions)"))

    if not headers:
        headers = [soup.find(class_="infobox")]

    for header in headers:
        while header.name != "ol":
            header = header.find_next_sibling()
        for child in header.children:
            if not child.name: # newline
                continue
            elif child.name == "h2": # next section
                break
            elif child.name == "li": # problem
                problems.append(f"<h2>Problem {p}</h2>" + str(child).removeprefix("<li>").removesuffix("</li>"))
                p += 1
            elif child.name == "ol": # other part of previous problem, don't append, instead add to previous
                problems[-1] = problems[-1] + str(child).removeprefix("<ol>").removesuffix("</ol>")
    return problems