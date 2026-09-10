import asyncio
import re
from bs4 import BeautifulSoup
from curl_cffi import requests
from logger_config import get_file_logger
import random

logger = get_file_logger("parsing", "logging/parsing.log")

async def fetch_problem_html(page_title: str, max_retries: int = 4) -> str:
    """Fetch raw HTML content from an AoPS wiki page using curl_cffi with exponential jitter backoff."""
    url = "https://artofproblemsolving.com/wiki/api.php"

    params = {
        "action": "parse",
        "page": page_title,
        "format": "json",
        "prop": "text",
        "disablelimitreport": "",
        "redirects": ""
    }

    for attempt in range(1, max_retries + 1):
        try:
            async with requests.AsyncSession() as session:
                response = await session.get(url=url, params=params, impersonate="chrome", timeout=12)

            # Handle 429
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")

                if retry_after and retry_after.isdigit():
                    backoff = int(retry_after) + random.uniform(0.5, 1.5)
                else:
                    # Exponential backoff + jitter
                    backoff = (2 ** attempt) + random.uniform(0.5, 2.0)

                await asyncio.sleep(backoff)
                continue

            response.raise_for_status()

            data = response.json()
            return data.get("parse", {}).get("text", {}).get("*", "")

        except Exception as e:
            backoff = (2 ** attempt) + random.uniform(0.5, 2.0)
            await asyncio.sleep(backoff)

    await asyncio.sleep(60)

    # Final attempt
    try:
        async with requests.AsyncSession() as session:
            response = await session.get(url=url, params=params, impersonate="chrome", timeout=12)
        response.raise_for_status()
        data = response.json()
        return data.get("parse", {}).get("text", {}).get("*", "")
    except Exception as e:
        return ""

def extract_problem_statement(raw_wikitext: str, year: int, contest: str, q_num: int) -> str:
    """Extract individual problem snippets from raw wikitext HTML."""
    soup = BeautifulSoup(raw_wikitext, "html.parser")

    span_tag = soup.find('span', id=re.compile(r'^Problem', re.IGNORECASE))

    if not span_tag:
        return ""

    h2_tag = span_tag.parent

    curr = h2_tag.next_sibling

    contest_name = contest.replace("_", " ")

    custom_header = f"<h2>{year} {contest_name} Problem {q_num}</h2>"
    problem_statement = [custom_header]

    while curr:
        if curr.name == "h2":
            break
        problem_statement.append(str(curr))
        curr = curr.next_sibling

    raw_statement = "".join(problem_statement)

    # 1. Convert protocol-relative URLs (src="//...) to https://
    raw_statement = re.sub(r'src=(["\'])//', r'src=\1https://', raw_statement)

    # 2. Convert single-slash domain-relative URLs (src="/...) to absolute
    raw_statement = re.sub(
        r'src=(["\'])/(?=[^/])',
        r'src=\1https://artofproblemsolving.com/',
        raw_statement
    )

    # 3. Convert single-slash href links (href="/...) to absolute
    raw_statement = re.sub(
        r'href=(["\'])/(?=[^/])',
        r'href=\1https://artofproblemsolving.com/',
        raw_statement
    )

    return raw_statement

async def fetch_problem_statement(year: int, wiki_title: str, q_num: int) -> str:
    """Fetch problem statement HTML and parse into a string."""
    page_title = f"{year}_{wiki_title}_Problems/Problem_{q_num}"
    raw_html = await fetch_problem_html(page_title)
    return extract_problem_statement(raw_html, year, wiki_title, q_num)