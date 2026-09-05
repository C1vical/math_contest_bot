import playwright
from curl_cffi import requests
from bs4 import BeautifulSoup
import re
from playwright.async_api import async_playwright

def fetch_raw_html() -> str:
    url = "https://cemc.uwaterloo.ca/sites/default/files/documents/2026/2018FermatContest.html"
    response = requests.get(url, impersonate="chrome")

    return response.text

def extract_problems(raw_html: str) -> list[str]:
    soup = BeautifulSoup(raw_html, "html.parser")
    problems = []

    for header in soup.find_all("h2", string=re.compile(r"Part [A-C]")):
        header = header.find_next_sibling()
        for child in header.children:
            if child.name == "li":
                problems.append(str(child).removeprefix("<li>").removesuffix("</li>"))
            elif child.name == "ol":
                problems[-1] = problems[-1] + str(child).removeprefix("<ol>").removesuffix("</ol>")
    return problems

def extract_head(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    return str(soup.find("head"))

def create_html_document(head_html: str, body_html: str):
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    {{head_html}}
    <body>
    {{body_html}}
    </body>
    </html>
    """

if __name__ == "__main__":
    raw = fetch_raw_html()
    problems = extract_problems(raw)

    head = extract_head(raw)
    document = create_html_document(head_html=head, body_html=problems[0])

    # # print(problems)
    # for problem in problems:
    #     print(problem[0])
    # print(len(problems))